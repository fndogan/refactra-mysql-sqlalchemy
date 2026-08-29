"""
Dynamic SQL Converter — Second-pass AI conversion for skipped functions.

Re-processes functions selected for additional review after the first pass.

Usage:
    refactra-mysql convert-dynamic
    refactra-mysql convert-dynamic --file output/admin/customers.py
    refactra-mysql convert-dynamic --report reports/ai_converter_*.json
    refactra-mysql convert-dynamic --apply
    refactra-mysql convert-dynamic --category dynamic_sql
"""
import argparse
import ast
import difflib
import glob
import json
import sys
import time
from pathlib import Path
from typing import Optional

from refactra_mysql.config import (
    AI_API_KEY,
    AI_MODEL,
    AI_PROVIDER,
    DYNAMIC_PROMPT_FILE,
    MODELS_FILE,
    OUTPUT_DIR,
    MAX_RETRIES,
    RATE_LIMIT_INPUT_TPM,
    RATE_LIMIT_OUTPUT_TPM,
    RATE_LIMIT_RPM,
    REPORTS_DIR,
    RETRY_DELAY,
    setup_logging,
)
from refactra_mysql.io_utils import atomic_write_text
from .model_extractor import ModelExtractor
from .prompt_loader import load_prompt
from .rate_limiter import RateLimiter

logger = setup_logging("dynamic_converter")

_DYNAMIC_PROMPT = load_prompt(DYNAMIC_PROMPT_FILE, "dynamic_prompt.example.txt")

# Report directory (same as main converter)
_REPORTS_DIR = REPORTS_DIR / "converter"

# =============================================================================
# Report Parser
# =============================================================================

def find_latest_report() -> Optional[Path]:
    """
    Find the most recent ai_converter JSON report file.

    Returns:
        Path to the latest report, or None if no reports exist.
    """
    pattern = str(_REPORTS_DIR / "ai_converter_*.json")    # same subdir as first-pass
    reports = sorted(glob.glob(pattern))
    if not reports:
        return None
    return Path(reports[-1])


def parse_skipped_functions(
    report_path: Path,
    category_filter: Optional[str] = None,
    file_filter: Optional[str] = None,
) -> list[dict]:
    """
    Parse a first-pass report and extract skipped function details.

    Args:
        report_path: Path to the JSON report file.
        category_filter: If set, only include skipped functions with this category
                         (e.g., 'dynamic_sql', 'ddl').
        file_filter: If set, only include skipped functions from this file path
                     (partial match supported).

    Returns:
        List of dicts with keys: file, function, reason, category.
    """
    with open(report_path, encoding="utf-8") as f:
        data = json.load(f)

    skipped = []
    for file_data in data.get("files", []):
        file_path = file_data.get("file", "")

        # Apply file filter (partial match)
        if file_filter and file_filter not in file_path:
            continue

        for detail in file_data.get("skipped_details", []):
            cat = detail.get("category", "")

            # Apply category filter
            if category_filter and cat != category_filter:
                continue

            skipped.append({
                "file": file_path,
                "function": detail.get("function", ""),
                "reason": detail.get("reason", ""),
                "category": cat,
            })

    return skipped


# =============================================================================
# Function Extractor (from output files)
# =============================================================================

def extract_function_source(filepath: Path, func_name: str) -> Optional[dict]:
    """
    Extract a specific function's source code from a Python file.

    Args:
        filepath: Path to the Python file.
        func_name: Name of the function to extract.

    Returns:
        Dict with 'name', 'start_line', 'end_line', 'source' keys,
        or None if the function is not found.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("File not found: %s", filepath)
        return None

    lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("Cannot parse %s — syntax error", filepath)
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                start = node.lineno - 1
                end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
                func_source = "\n".join(lines[start:end])
                return {
                    "name": func_name,
                    "start_line": node.lineno,
                    "end_line": end,
                    "source": func_source,
                }

    logger.warning("Function %s() not found in %s", func_name, filepath)
    return None


# =============================================================================
# Single Function Converter
# =============================================================================

def convert_dynamic_function(
    func_info: dict,
    provider,
    model_extractor: ModelExtractor,
    rate_limiter: RateLimiter,
    max_retries: int = MAX_RETRIES,
    retry_delay: float = RETRY_DELAY,
) -> Optional[str]:
    """
    Convert a single dynamic SQL function using AI.

    Args:
        func_info: Dict with 'name', 'source' keys.
        provider: AI provider instance (from engine.py).
        model_extractor: ModelExtractor for targeted model context.
        rate_limiter: RateLimiter for API pacing.
        max_retries: Maximum retry attempts.
        retry_delay: Base delay between retries.

    Returns:
        Converted source code string, or None on failure.
    """
    func_name = func_info["name"]
    func_source = func_info["source"]

    # Get targeted model context
    models_context = model_extractor.get_context_for_sql(func_source)
    if not models_context:
        # Fallback: provide all parsed model definitions
        models_context = "\n\n".join(model_extractor.models_by_table.values())

    # Estimate input tokens (~4 chars per token)
    estimated_tokens = (len(models_context) + len(func_source) + len(_DYNAMIC_PROMPT)) // 4

    # Dynamic max_tokens based on function size
    # The output needs to be at least as large as the input function
    func_tokens = len(func_source) // 4
    # Add 50% buffer for imports, refactoring overhead, etc.
    dynamic_max = max(4096, int(func_tokens * 1.5))
    # Cap at provider maximum
    dynamic_max = min(dynamic_max, 16384)
    if dynamic_max > 4096:
        logger.debug("    max_tokens adjusted: 4096 → %d (func ~%d tokens)", dynamic_max, func_tokens)

    for attempt in range(1, max_retries + 1):
        try:
            # Wait for rate limiter
            wait_time = rate_limiter.wait_if_needed(estimated_input_tokens=estimated_tokens)
            if wait_time > 0:
                logger.debug("    Waited %.1fs for rate limit", wait_time)

            # Build the request with the configured reviewed-pass prompt.
            system_content = (
                f"{_DYNAMIC_PROMPT}\n\n"
                f"## AVAILABLE SQLALCHEMY MODELS:\n\n"
                f"```python\n{models_context}\n```"
            )

            user_message = (
                "Convert the following Python function from raw/dynamic SQL to SQLAlchemy ORM.\n"
                "This function requires additional review after the first pass.\n"
                "Follow the configured system prompt and preserve behavior carefully.\n"
                "Return ONLY the converted function code with necessary imports.\n\n"
                f"```python\n{func_source}\n```"
            )

            # Call the provider with the reviewed-pass prompt.
            converted_code, input_tokens, output_tokens = _call_ai(
                provider, system_content, user_message,
                max_tokens=dynamic_max,
            )

            # Record actual usage
            rate_limiter.record_usage(input_tokens, output_tokens)

            # Validate syntax of converted code
            try:
                ast.parse(converted_code)
            except SyntaxError as syn_err:
                logger.warning(
                    "    [WARN] AI output has syntax error at line %d: %s — retrying",
                    syn_err.lineno or 0, syn_err.msg,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay * (2 ** (attempt - 1)))
                    continue
                return None

            logger.info(
                "    [PASS] %s() — %d in / %d out tokens",
                func_name, input_tokens, output_tokens,
            )
            return converted_code

        except Exception as e:
            if attempt < max_retries:
                delay = retry_delay * (2 ** (attempt - 1))
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
                return None

    return None


def _call_ai(provider, system_content: str, user_message: str,
             max_tokens: int = 4096) -> tuple[str, int, int]:
    """
    Call the AI provider with custom system content.

    Supports both Anthropic and OpenAI providers by detecting the provider type.

    Args:
        provider: AI provider instance.
        system_content: Full system prompt (dynamic prompt + models).
        user_message: User message with the function to convert.

    Returns:
        Tuple of (converted_code, input_tokens, output_tokens).
    """
    # Import here to avoid circular dependency
    from .engine import AnthropicProvider, OpenAIProvider

    if isinstance(provider, AnthropicProvider):
        provider._request_count += 1

        if provider.use_caching:
            anthropic_response = provider.client.messages.create(
                model=provider.model,
                max_tokens=max_tokens,
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
            anthropic_response = provider.client.messages.create(
                model=provider.model,
                max_tokens=max_tokens,
                system=system_content,
                messages=[{"role": "user", "content": user_message}],
            )

        anthropic_usage = anthropic_response.usage
        input_tokens = anthropic_usage.input_tokens
        output_tokens = anthropic_usage.output_tokens
        provider._total_input_tokens += input_tokens
        provider._total_output_tokens += output_tokens
        provider._cached_tokens += getattr(anthropic_usage, "cache_read_input_tokens", 0) or 0

        anthropic_content = AnthropicProvider._extract_response_text(anthropic_response)
        return AnthropicProvider._extract_code(anthropic_content), input_tokens, output_tokens

    elif isinstance(provider, OpenAIProvider):
        provider._request_count += 1

        openai_response = provider.client.chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_message},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
        )

        openai_usage = openai_response.usage
        if openai_usage is None:
            raise ValueError("OpenAI response did not include usage data")
        input_tokens = openai_usage.prompt_tokens
        output_tokens = openai_usage.completion_tokens
        provider._total_input_tokens += input_tokens
        provider._total_output_tokens += output_tokens

        openai_content = openai_response.choices[0].message.content
        return AnthropicProvider._extract_code(openai_content or ""), input_tokens, output_tokens

    else:
        raise TypeError(f"Unsupported provider type: {type(provider).__name__}")


# =============================================================================
# File Patcher
# =============================================================================

def patch_function_in_file(
    filepath: Path,
    func_name: str,
    original_info: dict,
    converted_code: str,
    dry_run: bool = False,
) -> bool:
    """
    Replace a function in a file with its converted version.

    Handles:
    - Indent preservation
    - Import collection and injection
    - Removal of TODO: [MANUAL REVIEW REQUIRED] comments
    - Post-patch syntax validation with rollback on failure

    Args:
        filepath: Path to the output file.
        func_name: Function name being replaced.
        original_info: Dict with 'start_line', 'end_line' from extract_function_source.
        converted_code: The AI-generated replacement code.
        dry_run: If True, don't write the file.

    Returns:
        True if patched successfully, False otherwise.
    """
    source = filepath.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)

    start_idx = original_info["start_line"] - 1
    end_idx = original_info["end_line"]

    # ── Remove TODO comment above the function ──
    # The first pass inserts: # TODO: [MANUAL REVIEW REQUIRED] ...
    if start_idx > 0:
        prev_line = lines[start_idx - 1].strip()
        if prev_line.startswith("# TODO: [MANUAL REVIEW REQUIRED]"):
            lines.pop(start_idx - 1)
            start_idx -= 1
            end_idx -= 1

    # ── Strip leading imports from AI output ──
    collected_imports: list[str] = []
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

    # ── Strip duplicate decorators ──
    original_above = lines[start_idx - 1].strip() if start_idx > 0 else ""
    first_converted = converted_code.lstrip("\n").split("\n")[0].strip() if converted_code.strip() else ""
    if first_converted.startswith("@") and first_converted == original_above:
        code_lines2 = converted_code.lstrip("\n").split("\n")
        converted_code = "\n".join(code_lines2[1:])

    # ── Strip hallucinated lines between decorator and def ──
    cleaned_lines = converted_code.lstrip("\n").split("\n")
    def_idx = None
    for k, cl in enumerate(cleaned_lines):
        if cl.strip().startswith("def ") or cl.strip().startswith("async def "):
            def_idx = k
            break
    if def_idx is not None and def_idx > 0:
        junk_before_def = []
        for k in range(def_idx):
            cl_stripped = cleaned_lines[k].strip()
            if cl_stripped.startswith("@") or cl_stripped == "":
                continue
            junk_before_def.append(cl_stripped)
        if junk_before_def:
            kept = [cl for cl in cleaned_lines[:def_idx]
                    if cl.strip().startswith("@") or cl.strip() == ""]
            kept.extend(cleaned_lines[def_idx:])
            converted_code = "\n".join(kept)
            logger.warning("    Stripped %d hallucinated line(s) before def", len(junk_before_def))

    # ── Indent preservation ──
    original_first_line = lines[start_idx] if start_idx < len(lines) else ""
    original_indent = len(original_first_line) - len(original_first_line.lstrip())

    converted_stripped = converted_code.lstrip("\n")
    if converted_stripped:
        ai_first_line = converted_stripped.split("\n")[0]
        ai_indent = len(ai_first_line) - len(ai_first_line.lstrip())
    else:
        ai_indent = 0

    indent_diff = original_indent - ai_indent
    if indent_diff != 0:
        indent_str = " " * indent_diff if indent_diff > 0 else ""
        new_lines = []
        for cline in converted_code.splitlines(keepends=True):
            if cline.strip():
                if indent_diff > 0:
                    new_lines.append(indent_str + cline)
                else:
                    remove = abs(indent_diff)
                    if cline[:remove] == " " * remove:
                        new_lines.append(cline[remove:])
                    else:
                        new_lines.append(cline)
            else:
                new_lines.append(cline)
        converted_code = "".join(new_lines)

    converted_lines = converted_code.splitlines(keepends=True)
    if converted_lines and not converted_lines[-1].endswith("\n"):
        converted_lines[-1] += "\n"

    # ── Syntax validation before patching ──
    lines[start_idx:end_idx] = converted_lines

    try:
        ast.parse("".join(lines))
    except SyntaxError as syn_err:
        logger.warning(
            "    [WARN] ROLLBACK %s() — replacement caused SyntaxError at line %d: %s",
            func_name, syn_err.lineno or 0, syn_err.msg,
        )
        return False

    # ── Inject collected AI imports ──
    if collected_imports:
        mod_source = "".join(lines)
        mod_lines = mod_source.split("\n")
        unique_imports = list(dict.fromkeys(collected_imports))

        # Find the last import line using AST
        last_import_idx = 0
        try:
            tree = ast.parse(mod_source)
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    last_import_idx = max(last_import_idx, node.end_lineno or node.lineno)
        except SyntaxError:
            # Fallback: find by text
            for idx, ml in enumerate(mod_lines):
                s = ml.strip()
                if s.startswith("import ") or s.startswith("from "):
                    last_import_idx = idx + 1

        # Filter out imports already present in the file
        existing_imports = set()
        for ml in mod_lines[:last_import_idx + 5]:
            s = ml.strip()
            if s.startswith("import ") or s.startswith("from "):
                existing_imports.add(s)

        new_imports = [imp for imp in unique_imports if imp not in existing_imports]
        if new_imports:
            import_block = "\n".join(new_imports) + "\n"
            mod_lines.insert(last_import_idx, import_block)
            lines = (("\n".join(mod_lines)) + "\n").splitlines(keepends=True)

    if dry_run:
        logger.info("    [DRY-RUN] Would patch %s::%s()", filepath.name, func_name)
        return True

    # Write the patched file
    atomic_write_text(filepath, "".join(lines))
    return True


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Convert skipped dynamic SQL functions using AI (second pass).",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to the first-pass report JSON. Defaults to the latest report.",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Output directory containing the files to patch.",
    )
    parser.add_argument(
        "--models-file",
        default=MODELS_FILE,
        help="Path to SQLAlchemy models file.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Only convert skipped functions in this specific file (partial match).",
    )
    parser.add_argument(
        "--exclude-file",
        nargs="*",
        default=[],
        help="Exclude files from conversion (partial match, multiple allowed).",
    )
    parser.add_argument(
        "--function",
        default=None,
        help="Only convert this specific function name.",
    )
    parser.add_argument(
        "--category",
        default=None,
        choices=["dynamic_sql", "ddl"],
        help="Only convert functions in this skip category.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the command is a dry run.",
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
    dry_run = not args.apply

    # ── Validate API key ──
    if not AI_API_KEY:
        logger.error("AI_API_KEY is not set. Store it in .env or the environment.")
        sys.exit(1)

    if not args.model:
        logger.error(
            "AI_MODEL is not set. Choose a currently supported model ID from "
            "your provider's official documentation."
        )
        sys.exit(1)

    # ── Validate models file ──
    models_path = Path(args.models_file)
    if not models_path.is_file():
        logger.error("Models file not found: %s", models_path)
        sys.exit(1)

    # ── Find report ──
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = find_latest_report()

    if not report_path or not report_path.is_file():
        logger.error(
            "No first-pass report found. Run 'refactra-mysql convert' first, "
            "or specify --report path."
        )
        sys.exit(1)

    logger.info("Using report: %s", report_path.name)

    # ── Parse skipped functions ──
    skipped = parse_skipped_functions(
        report_path,
        category_filter=args.category,
        file_filter=args.file,
    )

    # Filter by specific function name if requested
    if args.function:
        skipped = [s for s in skipped if s["function"] == args.function]

    # Exclude already-converted files
    if args.exclude_file:
        before_count = len(skipped)
        skipped = [
            s for s in skipped
            if not any(ex in s["file"] for ex in args.exclude_file)
        ]
        excluded = before_count - len(skipped)
        if excluded > 0:
            logger.info("Excluded %d functions from %d file(s)", excluded, len(args.exclude_file))

    if not skipped:
        logger.info("No skipped functions found matching the criteria.")
        sys.exit(0)

    logger.info("Found %d skipped functions to convert", len(skipped))

    # ── Initialize components ──
    model_extractor = ModelExtractor(models_path)
    rate_limiter = RateLimiter(
        rpm=args.rpm,
        input_tpm=args.input_tpm,
        output_tpm=args.output_tpm,
    )

    # Reuse the AI provider factory from engine.py
    from .engine import create_provider
    provider = create_provider(args.provider, AI_API_KEY, args.model)

    # ── Print configuration ──
    summary = model_extractor.get_summary()
    logger.info("=" * 60)
    logger.info("Dynamic SQL Converter (Second Pass)")
    logger.info("=" * 60)
    logger.info("Provider:    %s (%s)", args.provider, args.model)
    logger.info("Models:      %s (%d classes)", models_path.name, summary["total_models"])
    logger.info("Report:      %s", report_path.name)
    logger.info("Targets:     %d skipped functions", len(skipped))
    logger.info("Dry run:     %s", dry_run)
    if args.category:
        logger.info("Category:    %s", args.category)
    if args.file:
        logger.info("File filter: %s", args.file)
    logger.info("-" * 60)

    # ── Group by file for efficient processing ──
    files_map: dict[str, list[dict]] = {}
    for item in skipped:
        files_map.setdefault(item["file"], []).append(item)

    # ── Process each file ──
    output_dir = Path(args.output_dir).resolve()
    stats = {
        "total": len(skipped),
        "converted": 0,
        "failed": 0,
        "not_found": 0,
    }

    results: list[dict] = []

    for file_rel_path, func_list in sorted(files_map.items()):
        # Resolve report paths without flattening nested directories or allowing
        # a tampered report to address files outside the configured output tree.
        report_file = Path(file_rel_path)
        if report_file.is_absolute():
            filepath = report_file.resolve()
        else:
            if report_file.parts and report_file.parts[0] == output_dir.name:
                report_file = Path(*report_file.parts[1:])
            filepath = (output_dir / report_file).resolve()

        try:
            filepath.relative_to(output_dir)
        except ValueError:
            logger.warning("Refusing path outside output directory: %s", file_rel_path)
            stats["not_found"] += len(func_list)
            continue

        if not filepath.is_file():
            logger.warning("File not found: %s", file_rel_path)
            stats["not_found"] += len(func_list)
            continue

        logger.info("\n[FILE] %s (%d functions)", filepath.relative_to(output_dir.parent), len(func_list))

        for func_item in func_list:
            func_name = func_item["function"]
            logger.info("  [%s] Converting: %s() — %s",
                        func_item["category"], func_name, func_item["reason"])

            # Extract current source from the output file
            func_info = extract_function_source(filepath, func_name)
            if not func_info:
                logger.warning("    [FAIL] Function %s() not found in %s", func_name, filepath.name)
                stats["not_found"] += 1
                results.append({
                    "file": file_rel_path,
                    "function": func_name,
                    "status": "not_found",
                })
                continue

            # Save original source BEFORE conversion (for diff reporting)
            original_source = func_info["source"]

            # Convert with AI
            converted_code = convert_dynamic_function(
                func_info, provider, model_extractor, rate_limiter,
            )

            if not converted_code:
                stats["failed"] += 1
                results.append({
                    "file": file_rel_path,
                    "function": func_name,
                    "status": "failed",
                })
                continue

            # Patch the file
            # Re-extract to get fresh line numbers (in case previous patches shifted lines)
            fresh_info = extract_function_source(filepath, func_name)
            if not fresh_info:
                stats["failed"] += 1
                results.append({
                    "file": file_rel_path,
                    "function": func_name,
                    "status": "failed",
                    "reason": "function disappeared after earlier patch",
                })
                continue

            success = patch_function_in_file(
                filepath, func_name, fresh_info, converted_code, dry_run=dry_run,
            )

            if success:
                stats["converted"] += 1
                results.append({
                    "file": file_rel_path,
                    "function": func_name,
                    "status": "converted",
                    "original_source": original_source,
                    "converted_source": converted_code,
                })
            else:
                stats["failed"] += 1
                results.append({
                    "file": file_rel_path,
                    "function": func_name,
                    "status": "failed",
                    "reason": "patch caused syntax error (rolled back)",
                })

    # ── Print summary ──
    logger.info("\n" + "=" * 60)
    logger.info("DYNAMIC CONVERTER REPORT")
    logger.info("=" * 60)
    logger.info("Total targets:  %d", stats["total"])
    logger.info("Converted:      %d [PASS]", stats["converted"])
    logger.info("Failed:         %d [FAIL]", stats["failed"])
    logger.info("Not found:      %d [SKIP]", stats["not_found"])

    if provider:
        usage = provider.get_usage_report()
        logger.info("-" * 60)
        logger.info("API USAGE")
        logger.info("  Requests:     %d", usage.get("requests", 0))
        logger.info("  Input tokens: %d", usage.get("input_tokens", 0))
        logger.info("  Output tokens:%d", usage.get("output_tokens", 0))

    logger.info("=" * 60)

    # ── Save report ──
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "provider": args.provider,
        "model": args.model,
        "dry_run": dry_run,
        "source_report": str(report_path),
        "category_filter": args.category,
        "file_filter": args.file,
        "summary": stats,
        "api_usage": provider.get_usage_report() if provider else {},
        "rate_limiter": rate_limiter.get_stats(),
        "results": results,
    }

    # ── Save JSON report ──
    json_path = _REPORTS_DIR / f"dynamic_converter_{timestamp}.json"
    json_path.write_text(
        json.dumps(report_data, indent=2, default=str), encoding="utf-8",
    )
    logger.info("JSON report saved: %s", json_path)

    # ── Save TXT report ──
    txt_lines = [
        "=" * 70,
        "  DYNAMIC SQL CONVERTER REPORT (Second Pass)",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        "",
        f"  Provider:        {args.provider} ({args.model})",
        f"  Source report:   {report_path.name}",
        f"  Dry run:         {dry_run}",
        f"  Category filter: {args.category or 'all'}",
        f"  File filter:     {args.file or 'all'}",
        "",
        "─" * 70,
        "  SUMMARY",
        "─" * 70,
        f"  Total targets:   {stats['total']}",
        f"  Converted:       {stats['converted']} [PASS]",
        f"  Failed:          {stats['failed']} [FAIL]",
        f"  Not found:       {stats['not_found']} [SKIP]",
    ]

    if provider:
        usage = provider.get_usage_report()
        txt_lines.extend([
            "",
            "─" * 70,
            "  API USAGE",
            "─" * 70,
            f"  Requests:        {usage.get('requests', 0)}",
            f"  Input tokens:    {usage.get('input_tokens', 0)}",
            f"  Output tokens:   {usage.get('output_tokens', 0)}",
            f"  Cached tokens:   {usage.get('cached_tokens', 0)}",
        ])

    txt_lines.extend([
        "",
        "─" * 70,
        "  PER-FUNCTION RESULTS",
        "─" * 70,
    ])
    for r in results:
        icon = "[PASS]" if r["status"] == "converted" else "[FAIL]" if r["status"] == "failed" else "[SKIP]"
        reason = f" — {r['reason']}" if r.get("reason") else ""
        txt_lines.append(f"\n  {icon} {r['file']}::{r['function']}() [{r['status']}]{reason}")

        # Include before/after diff for converted functions (no AI cost — pure system work)
        if r.get("original_source") and r.get("converted_source"):
            txt_lines.append("")
            txt_lines.append("    ┌─── BEFORE (original) ───")
            for line in r["original_source"].splitlines():
                txt_lines.append(f"    │ {line}")
            txt_lines.append("    ├─── AFTER (converted) ───")
            for line in r["converted_source"].splitlines():
                txt_lines.append(f"    │ {line}")
            txt_lines.append("    ├─── DIFF ───")
            diff = difflib.unified_diff(
                r["original_source"].splitlines(),
                r["converted_source"].splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
                n=3,
            )
            for dline in diff:
                txt_lines.append(f"    │ {dline}")
            txt_lines.append("    └" + "─" * 50)

    txt_lines.extend(["", "=" * 70])

    txt_path = _REPORTS_DIR / f"dynamic_converter_{timestamp}.txt"
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")
    logger.info("TXT report saved: %s", txt_path)


if __name__ == "__main__":
    main()
