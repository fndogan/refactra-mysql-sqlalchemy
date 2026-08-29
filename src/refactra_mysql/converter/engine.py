"""
AI-Powered SQL → ORM Converter.

Sends selected Python functions and model context to a configured AI provider
and receives proposed ORM-converted code for review.

Usage:
    refactra-mysql convert --source-dir ./queries --models-file ./models.py --dry-run
"""
import argparse
import ast
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from refactra_mysql.config import (
    AI_API_KEY,
    AI_MODEL,
    AI_PROVIDER,
    AI_PROMPT_CACHING,
    SYSTEM_PROMPT_FILE,
    MODELS_FILE,
    OUTPUT_DIR,
    DRY_RUN,
    MAX_RETRIES,
    RATE_LIMIT_INPUT_TPM,
    RATE_LIMIT_OUTPUT_TPM,
    RATE_LIMIT_RPM,
    RETRY_DELAY,
    setup_logging,
)
from .model_extractor import ModelExtractor
from .prompt_loader import load_prompt
from .rate_limiter import RateLimiter
from refactra_mysql.io_utils import atomic_write_text

logger = setup_logging("ai_converter")

_SYSTEM_PROMPT = load_prompt(SYSTEM_PROMPT_FILE, "system_prompt.example.txt")

# =============================================================================
# AI Provider Abstraction
# =============================================================================

class AIProvider:
    """Abstract base for AI providers."""

    def convert(self, function_code: str, models_context: str) -> tuple[str, int, int]:
        """
        Convert a function from raw SQL to ORM.

        Returns:
            Tuple of (converted_code, input_tokens, output_tokens).
        """
        raise NotImplementedError

    def get_usage_report(self) -> dict:
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    """Claude AI provider with prompt caching support."""

    def __init__(self, api_key: str, model: str, use_caching: bool = True):
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package is required. Install with: pip install anthropic"
            )

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.use_caching = use_caching
        self._request_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cached_tokens = 0

    def convert(self, function_code: str, models_context: str) -> tuple[str, int, int]:
        """Send function to Claude for ORM conversion."""
        self._request_count += 1

        # Build system message with models context
        system_content = (
            f"{_SYSTEM_PROMPT}\n\n"
            f"## AVAILABLE SQLALCHEMY MODELS:\n\n"
            f"```python\n{models_context}\n```"
        )

        user_message = (
            "Convert the following Python function from raw SQL to SQLAlchemy ORM.\n"
            "Return ONLY the converted function code with necessary imports.\n\n"
            f"```python\n{function_code}\n```"
        )

        try:
            if self.use_caching:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": system_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_message}],
                )
            else:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_content,
                    messages=[{"role": "user", "content": user_message}],
                )

            # Track token usage
            usage = response.usage
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens
            self._cached_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            # Extract code from response
            content = self._extract_response_text(response)
            return self._extract_code(content), input_tokens, output_tokens

        except Exception as e:
            logger.error("AI conversion failed: %s", e)
            raise

    def get_usage_report(self) -> dict:
        """Return token usage statistics."""
        return {
            "requests": self._request_count,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "cached_tokens": self._cached_tokens,
        }

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Return the first textual content block from a provider response."""
        for block in response.content:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                return block_text
        raise ValueError("AI provider response did not contain text")

    @staticmethod
    def _extract_code(response_text: str) -> str:
        """Extract Python code from AI response (handles markdown code blocks)."""
        text = response_text.strip()

        # Try ```python ... ``` first
        marker = "```python"
        idx = text.find(marker)
        if idx != -1:
            start = idx + len(marker)
            # Skip optional newline after marker
            if start < len(text) and text[start] == "\n":
                start += 1
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
            # No closing ``` — take everything after marker
            return text[start:].strip()

        # Try generic ``` ... ```
        idx = text.find("```")
        if idx != -1:
            start = idx + 3
            # Skip optional language tag on same line
            nl = text.find("\n", start)
            if nl != -1 and nl - start < 20:
                start = nl + 1
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
            return text[start:].strip()

        return text


class OpenAIProvider(AIProvider):
    """OpenAI GPT provider."""

    def __init__(self, api_key: str, model: str):
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package is required. Install with: pip install openai"
            )

        self.client = openai.OpenAI(api_key=api_key)
        self.model = model
        self._request_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def convert(self, function_code: str, models_context: str) -> tuple[str, int, int]:
        """Send function to GPT for ORM conversion."""
        self._request_count += 1

        system_content = (
            f"{_SYSTEM_PROMPT}\n\n"
            f"## AVAILABLE SQLALCHEMY MODELS:\n\n"
            f"```python\n{models_context}\n```"
        )

        user_message = (
            "Convert the following Python function from raw SQL to SQLAlchemy ORM.\n"
            "Return ONLY the converted function code with necessary imports.\n\n"
            f"```python\n{function_code}\n```"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=4096,
                temperature=0.1,
            )

            usage = response.usage
            if usage is None:
                raise ValueError("OpenAI response did not include usage data")
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens
            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens

            content = response.choices[0].message.content
            return AnthropicProvider._extract_code(content or ""), input_tokens, output_tokens

        except Exception as e:
            logger.error("AI conversion failed: %s", e)
            raise

    def get_usage_report(self) -> dict:
        return {
            "requests": self._request_count,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "cached_tokens": 0,
        }


def create_provider(provider: str, api_key: str, model: str) -> AIProvider:
    """Factory function to create the appropriate AI provider."""
    if provider == "anthropic":
        return AnthropicProvider(api_key, model, use_caching=AI_PROMPT_CACHING)
    elif provider == "openai":
        return OpenAIProvider(api_key, model)
    else:
        raise ValueError(f"Unknown AI provider: {provider}. Use 'anthropic' or 'openai'.")


# =============================================================================
# Function Extractor
# =============================================================================

class FunctionInfo(TypedDict):
    name: str
    start_line: int
    end_line: int
    source: str


def extract_functions(filepath: Path) -> list[FunctionInfo]:
    """
    Extract all functions from a Python file that contain SQL patterns.

    Returns:
        List of dicts with 'name', 'start_line', 'end_line', 'source' keys.
    """
    source = filepath.read_text(encoding="utf-8")
    lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("Cannot parse %s", filepath)
        return []

    # SQL detection patterns — catches all forms of raw SQL usage
    _SQL_INDICATORS = [
        "cursor.execute",
        "db.execute",
        "execute_query",
        "text(",
        "SELECT ",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
    ]

    functions: list[FunctionInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
            func_source = "\n".join(lines[start:end])

            # Check for any SQL indicator (case-insensitive for SQL keywords)
            func_upper = func_source.upper()
            has_sql = any(
                ind.upper() in func_upper for ind in _SQL_INDICATORS
            )

            if has_sql:
                functions.append({
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": end,
                    "source": func_source,
                })

    return functions


# =============================================================================
# File Converter (with smart context + rate limiting)
# =============================================================================

def convert_file(
    filepath: Path,
    output_path: Path,
    provider: AIProvider,
    model_extractor: ModelExtractor,
    rate_limiter: RateLimiter,
    dry_run: bool = False,
    max_retries: int = MAX_RETRIES,
    retry_delay: float = RETRY_DELAY,
) -> dict:
    """
    Convert all SQL functions in a file to ORM equivalents using AI.

    Uses smart model extraction to send only relevant models per function,
    and rate limiting to respect API limits.

    Args:
        filepath: Source file path.
        output_path: Output file path.
        provider: AI provider instance.
        model_extractor: ModelExtractor for targeted model context.
        rate_limiter: RateLimiter for API pacing.
        dry_run: If True, don't write files.
        max_retries: Maximum retry attempts per function.
        retry_delay: Base delay between retries (exponential backoff).

    Returns:
        Result dict with conversion statistics.
    """
    result: dict[str, Any] = {
        "file": str(filepath),
        "functions_total": 0,
        "functions_converted": 0,
        "functions_failed": 0,
        "functions_skipped_unsafe": 0,
        "functions_review": 0,
        "skipped_details": [],
        "review_details": [],
        "status": "ok",
    }

    source = filepath.read_text(encoding="utf-8")
    functions = extract_functions(filepath)
    result["functions_total"] = len(functions)

    if not functions:
        result["status"] = "skipped"
        if not dry_run and filepath.resolve() != output_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(output_path, source)
        return result

    lines = source.splitlines(keepends=True)

    # Collect replacements: (start_line, end_line, function_name, converted_code)
    # We process in forward order for logging but apply in reverse to avoid offset drift
    replacements: list[tuple[int, int, str, str]] = []

    for i, func in enumerate(functions, 1):
        func_name = func["name"]
        logger.info(
            "    [%d/%d] Converting: %s()",
            i, len(functions), func_name,
        )

        # ── Safety Classification ──
        from .safety_classifier import classify_function, RiskLevel
        classification = classify_function(func["source"], func_name)

        if classification.level == RiskLevel.SKIP:
            logger.warning(
                "    [SKIP] SKIP %s() — %s",
                func_name, classification.reason,
            )
            result["functions_skipped_unsafe"] += 1
            result["skipped_details"].append({
                "function": func_name,
                "reason": classification.reason,
                "category": classification.category,
            })
            # Add TODO comment to the function
            todo_comment = f"# TODO: [MANUAL REVIEW REQUIRED] {classification.reason}\n"
            original_line = func["source"].split("\n")[0]
            indent = len(original_line) - len(original_line.lstrip())
            todo_line = " " * indent + todo_comment
            start_idx = func["start_line"] - 1
            lines.insert(start_idx, todo_line)
            # Adjust subsequent function line numbers
            for j in range(i, len(functions)):
                functions[j]["start_line"] += 1
                functions[j]["end_line"] += 1
            continue

        if classification.level == RiskLevel.REVIEW:
            logger.info(
                "    [WARN] REVIEW %s() — %s (converting with flag)",
                func_name, classification.reason,
            )
            result["functions_review"] += 1
            result["review_details"].append({
                "function": func_name,
                "reason": classification.reason,
                "category": classification.category,
            })

        # Get targeted model context for this specific function
        models_context = model_extractor.get_context_for_sql(func["source"])
        if not models_context:
            logger.warning("    No models found for %s(), using file-level context", func_name)
            models_context = model_extractor.get_context_for_file(filepath)

        # Estimate input tokens (~4 chars per token)
        estimated_tokens = (len(models_context) + len(func["source"]) + len(_SYSTEM_PROMPT)) // 4

        # Retry loop with exponential backoff
        for attempt in range(1, max_retries + 1):
            try:
                # Wait for rate limiter before making the request
                wait_time = rate_limiter.wait_if_needed(estimated_input_tokens=estimated_tokens)
                if wait_time > 0:
                    logger.debug("    Waited %.1fs for rate limit", wait_time)

                # Make the AI call
                converted_code, input_tokens, output_tokens = provider.convert(
                    func["source"], models_context
                )

                # Record actual usage with rate limiter
                rate_limiter.record_usage(input_tokens, output_tokens)

                # Store replacement by line numbers (1-indexed from AST)
                replacements.append(
                    (func["start_line"], func["end_line"], func_name, converted_code)
                )
                result["functions_converted"] += 1
                logger.info(
                    "    [PASS] %s() — %d in / %d out tokens",
                    func_name, input_tokens, output_tokens,
                )
                break

            except Exception as e:
                if attempt < max_retries:
                    delay = retry_delay * (2 ** (attempt - 1))  # Exponential backoff
                    logger.warning(
                        "    Attempt %d/%d failed for %s(): %s — retrying in %.0fs",
                        attempt, max_retries, func_name, e, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "    [FAIL] All %d attempts failed for %s(): %s",
                        max_retries, func_name, e,
                    )
                    result["functions_failed"] += 1

    # Apply replacements in REVERSE order (bottom-up) to avoid line offset drift
    collected_imports: list[str] = []  # AI imports to inject at file top

    if replacements:
        # Filter out nested functions (child inside parent range)
        # Sort by start_line ascending to detect containment
        replacements.sort(key=lambda r: r[0])
        filtered: list[tuple[int, int, str, str]] = []
        for i, (start_value, end_value, name, code) in enumerate(replacements):
            is_nested = False
            for j, (parent_start, parent_end, _, _) in enumerate(replacements):
                if i != j and parent_start < start_value and parent_end >= end_value:
                    is_nested = True
                    logger.debug(
                        "    Skipping nested %d-%d (inside %d-%d)",
                        start_value,
                        end_value,
                        parent_start,
                        parent_end,
                    )
                    break
            if not is_nested:
                filtered.append((start_value, end_value, name, code))
        replacements = filtered

        # Apply in reverse order
        replacements.sort(key=lambda r: r[0], reverse=True)
        for start_line, end_line, replacement_name, converted_code in replacements:
            # Convert 1-indexed lines to 0-indexed
            start_idx = start_line - 1
            end_idx = end_line  # end_line is inclusive, slice is exclusive

            # ── Strip leading imports from AI output ──
            # AI prepends "from sqlalchemy import...\n" before the function.
            # We collect these and inject at file top instead.
            code_lines = converted_code.split("\n")
            func_start = 0
            for k, cline in enumerate(code_lines):
                stripped = cline.strip()
                if stripped == "" or stripped.startswith("import ") or stripped.startswith("from "):
                    if stripped:
                        collected_imports.append(stripped)
                    func_start = k + 1
                else:
                    break
            if func_start > 0:
                converted_code = "\n".join(code_lines[func_start:])

            # ── Strip duplicate decorators from AI output ──
            # Original decorator (@staticmethod etc.) is preserved at its line.
            # AI might return @staticmethod again — remove it.
            original_above = lines[start_idx - 1].strip() if start_idx > 0 else ""
            first_converted = converted_code.lstrip("\n").split("\n")[0].strip() if converted_code.strip() else ""
            if first_converted.startswith("@") and first_converted == original_above:
                # Remove duplicate decorator from AI output
                code_lines2 = converted_code.lstrip("\n").split("\n")
                converted_code = "\n".join(code_lines2[1:])

            # ── Strip hallucinated lines between decorator and def ──
            # AI sometimes inserts module-level statements (logger=, constants)
            # between @staticmethod and def. Clean them out.
            cleaned_lines = converted_code.lstrip("\n").split("\n")
            def_idx = None
            for k, cl in enumerate(cleaned_lines):
                if cl.strip().startswith("def ") or cl.strip().startswith("async def "):
                    def_idx = k
                    break
            if def_idx is not None and def_idx > 0:
                # Check if lines before def are NON-decorator junk
                junk_before_def = []
                for k in range(def_idx):
                    cl_stripped = cleaned_lines[k].strip()
                    if cl_stripped.startswith("@"):
                        continue  # keep decorators
                    if cl_stripped == "":
                        continue  # skip blank lines
                    junk_before_def.append(cl_stripped)
                if junk_before_def:
                    # Remove junk lines, keep only decorators and def onwards
                    kept = [cl for cl in cleaned_lines[:def_idx]
                            if cl.strip().startswith("@") or cl.strip() == ""]
                    kept.extend(cleaned_lines[def_idx:])
                    converted_code = "\n".join(kept)
                    logger.warning(f"    Stripped {len(junk_before_def)} hallucinated line(s) before def")

            # ── Indent preservation ──
            # Detect original function's indentation level
            original_first_line = lines[start_idx] if start_idx < len(lines) else ""
            original_indent = len(original_first_line) - len(original_first_line.lstrip())

            # Detect AI output's indentation level (usually 0)
            converted_stripped = converted_code.lstrip("\n")
            if converted_stripped:
                ai_first_line = converted_stripped.split("\n")[0]
                ai_indent = len(ai_first_line) - len(ai_first_line.lstrip())
            else:
                ai_indent = 0

            # Re-indent if there's a mismatch
            indent_diff = original_indent - ai_indent
            if indent_diff != 0:
                indent_str = " " * indent_diff if indent_diff > 0 else ""
                new_lines = []
                for cline in converted_code.splitlines(keepends=True):
                    if cline.strip():  # Non-empty line
                        if indent_diff > 0:
                            new_lines.append(indent_str + cline)
                        else:
                            # Remove leading spaces (dedent)
                            remove = abs(indent_diff)
                            if cline[:remove] == " " * remove:
                                new_lines.append(cline[remove:])
                            else:
                                new_lines.append(cline)
                    else:
                        new_lines.append(cline)  # Keep blank lines as-is
                converted_code = "".join(new_lines)

            converted_lines = converted_code.splitlines(keepends=True)
            # Ensure last line has newline
            if converted_lines and not converted_lines[-1].endswith("\n"):
                converted_lines[-1] += "\n"

            # ── Post-replacement syntax check ──
            # Save a snapshot before replacing so we can rollback
            lines_snapshot = lines[:]
            lines[start_idx:end_idx] = converted_lines

            # Validate that the file is still valid Python after this replacement
            try:
                import ast as _ast_check
                _ast_check.parse("".join(lines))
            except SyntaxError as syn_err:
                logger.warning(
                    f"    [WARN] ROLLBACK {replacement_name}() — replacement caused SyntaxError "
                    f"at line {syn_err.lineno}: {syn_err.msg}. Restoring original."
                )
                lines = lines_snapshot
                # Mark in source as needing manual review
                if start_idx < len(lines):
                    marker = "    # TODO: [MANUAL REVIEW REQUIRED] AI conversion caused syntax error — review manually\n"
                    lines.insert(start_idx + 1, marker)
                result["functions_review"] += 1

        modified_source = "".join(lines)

        # ── Inject collected AI imports at file top ──
        if collected_imports:
            unique_imports = list(dict.fromkeys(collected_imports))  # preserve order, dedup
            import_block = "\n".join(unique_imports) + "\n"
            # Use AST to find the last import line (skips docstrings correctly)
            mod_lines = modified_source.split("\n")
            last_import_idx = 0
            try:
                import ast as _ast
                _tree = _ast.parse(modified_source)
                for _node in _ast.iter_child_nodes(_tree):
                    if isinstance(_node, (_ast.Import, _ast.ImportFrom)):
                        last_import_idx = max(last_import_idx, _node.end_lineno or _node.lineno)
            except SyntaxError:
                # Fallback: find last import line by text (skip docstrings)
                in_docstring = False
                for idx, ml in enumerate(mod_lines):
                    stripped_line = ml.strip()
                    if '"""' in stripped_line or "'''" in stripped_line:
                        # Toggle docstring state (rough heuristic)
                        count = stripped_line.count('"""') + stripped_line.count("'''")
                        if count % 2 == 1:
                            in_docstring = not in_docstring
                        continue
                    if in_docstring:
                        continue
                    if stripped_line.startswith("import ") or stripped_line.startswith("from "):
                        last_import_idx = idx + 1  # 1-indexed like AST
                    elif stripped_line and not stripped_line.startswith("#"):
                        if last_import_idx > 0:
                            break

            # Insert after last import (last_import_idx is 1-indexed from AST)
            insert_at = last_import_idx  # AST lineno is 1-indexed, list is 0-indexed

            # ── Safety: ensure insert point is NOT inside a multi-line import ──
            # e.g. "from utils.foo import (\n   bar,\n   baz\n)"
            # If we insert here, we break the parenthesized import block.
            in_multiline = False
            for check_idx in range(insert_at):
                cl = mod_lines[check_idx].strip()
                if cl.startswith("from ") and cl.endswith("("):
                    in_multiline = True
                if in_multiline and (")" in cl):
                    in_multiline = False
            if in_multiline:
                # We're inside a multi-line import — find the closing paren
                for scan_idx in range(insert_at, len(mod_lines)):
                    if ")" in mod_lines[scan_idx]:
                        insert_at = scan_idx + 1
                        break
                logger.debug("    Import insert adjusted to L%d (was inside multi-line import)", insert_at)

            mod_lines.insert(insert_at, import_block)
            modified_source = "\n".join(mod_lines)
    else:
        # Skipped-only files may still contain manual-review markers.
        modified_source = "".join(lines)

    should_write = (
        filepath.resolve() != output_path.resolve()
        or modified_source != source
    )
    if not dry_run and should_write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, modified_source)

    return result


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Convert raw SQL functions to SQLAlchemy ORM using AI.",
    )
    parser.add_argument(
        "--source-dir",
        default=OUTPUT_DIR,
        help="Directory containing Python files to convert.",
    )
    parser.add_argument(
        "--models-file",
        default=MODELS_FILE,
        help="Path to SQLAlchemy models file.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for converted files.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Explicitly allow modifying source files in-place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=DRY_RUN,
        help="Preview without writing files.",
    )
    parser.add_argument(
        "--provider",
        default=AI_PROVIDER,
        choices=["anthropic", "openai"],
        help="AI provider to use.",
    )
    parser.add_argument(
        "--model",
        default=AI_MODEL,
        help="AI model name.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Convert a single file instead of the entire directory.",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=RATE_LIMIT_RPM,
        help=f"Requests per minute limit (default: {RATE_LIMIT_RPM}).",
    )
    parser.add_argument(
        "--input-tpm",
        type=int,
        default=RATE_LIMIT_INPUT_TPM,
        help=f"Input tokens per minute limit (default: {RATE_LIMIT_INPUT_TPM}).",
    )
    parser.add_argument(
        "--output-tpm",
        type=int,
        default=RATE_LIMIT_OUTPUT_TPM,
        help=f"Output tokens per minute limit (default: {RATE_LIMIT_OUTPUT_TPM}).",
    )
    args = parser.parse_args()

    if args.output_dir and args.in_place:
        parser.error("Use either --output-dir or --in-place, not both.")
    if not args.dry_run and not args.output_dir and not args.in_place:
        parser.error(
            "Refusing to modify source files implicitly. Use --output-dir, "
            "--dry-run, or explicitly pass --in-place."
        )

    # Validate API key
    if not AI_API_KEY:
        logger.error("AI_API_KEY is not set. Store it in .env or the environment.")
        sys.exit(1)

    if not args.model:
        logger.error(
            "AI_MODEL is not set. Choose a currently supported model ID from "
            "your provider's official documentation."
        )
        sys.exit(1)

    # Validate models file
    models_path = Path(args.models_file)
    if not models_path.is_file():
        logger.error("Models file not found: %s", models_path)
        sys.exit(1)

    # Initialize components
    model_extractor = ModelExtractor(models_path)
    rate_limiter = RateLimiter(
        rpm=args.rpm,
        input_tpm=args.input_tpm,
        output_tpm=args.output_tpm,
    )
    ai = create_provider(args.provider, AI_API_KEY, args.model)

    # Print configuration
    summary = model_extractor.get_summary()
    logger.info("=" * 60)
    logger.info("AI-Powered SQL → ORM Converter")
    logger.info("=" * 60)
    logger.info("Provider:  %s (%s)", args.provider, args.model)
    logger.info("Models:    %s (%d classes, %d tables)", models_path.name, summary["total_models"], summary["total_tables"])
    logger.info("Limits:    %d RPM, %dK input TPM, %dK output TPM", args.rpm, args.input_tpm // 1000, args.output_tpm // 1000)
    logger.info("Caching:   %s", AI_PROMPT_CACHING)
    logger.info("Dry run:   %s", args.dry_run)
    logger.info("-" * 60)

    # ── Initialize reporter ──
    from .reporter import AIConverterReporter
    reporter = AIConverterReporter(
        provider=args.provider,
        model=args.model,
        dry_run=args.dry_run,
    )

    # Single file mode
    if args.file:
        filepath = Path(args.file)
        if not filepath.is_file():
            logger.error("File not found: %s", filepath)
            sys.exit(1)

        output_path = Path(args.output_dir or str(filepath.parent)) / filepath.name
        result = convert_file(
            filepath, output_path, ai, model_extractor, rate_limiter, args.dry_run,
        )
        reporter.add_file_result(result)

    else:
        # Directory mode
        source_dir = Path(args.source_dir)
        if not source_dir.is_dir():
            logger.error("Source directory not found: %s", source_dir)
            sys.exit(1)

        output_dir = Path(args.output_dir) if args.output_dir else source_dir
        py_files = sorted(source_dir.rglob("*.py"))
        py_files = [f for f in py_files if "__pycache__" not in str(f)]

        logger.info("Found %d Python files", len(py_files))

        for filepath in py_files:
            rel_path = filepath.relative_to(source_dir)
            output_path = output_dir / rel_path

            logger.info("  Processing: %s", rel_path)
            result = convert_file(
                filepath, output_path, ai, model_extractor, rate_limiter, args.dry_run,
            )
            reporter.add_file_result(result)

    # Finalize reports
    usage = ai.get_usage_report()
    limiter_stats = rate_limiter.get_stats()
    reporter.set_api_usage(usage, limiter_stats)

    reporter.print_console_report()
    reporter.save_report()
    reporter.save_json()


if __name__ == "__main__":
    main()
