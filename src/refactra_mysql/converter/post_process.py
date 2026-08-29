"""
Post-Process Cleanup — Cleans up AI converter output.

Runs after AI conversion to fix common issues:
1. Removes old MySQL imports (get_db_connection, DictCursor, pymysql)
2. Deduplicates imports (AI adds per-function imports)
3. Removes dead code patterns (if 'connection' in locals(), etc.)
4. Removes empty pass-only blocks
5. Ensures consistent import ordering

Uses LibCST for safe AST-based transformations.

Usage (called by the converter or standalone):
    refactra-mysql post-process --source-dir ./output
    refactra-mysql post-process --file ./output/settings.py --apply
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Any

import libcst as cst
import libcst.matchers as m

from refactra_mysql.config import REPORTS_DIR, setup_logging
from refactra_mysql.io_utils import atomic_write_text

logger = setup_logging("post_process")


# =============================================================================
# Old imports that should be removed after migration
# =============================================================================
_OLD_IMPORT_MODULES = {
    "pymysql",
    "pymysql.cursors",
    "MySQLdb",
}

_OLD_IMPORT_NAMES = {
    "get_db_connection",
    "execute_query",
    "DictCursor",
}

_OLD_IMPORT_FROM_MODULES = {
    "pymysql.cursors",
}


# =============================================================================
# LibCST Transformer: Remove old imports
# =============================================================================

class RemoveOldImportsTransformer(cst.CSTTransformer):
    """Remove MySQL/raw-connection related imports."""

    def __init__(self):
        self.changes: list[str] = []

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom | cst.RemovalSentinel:
        """Remove legacy MySQL modules and raw-connection helper names."""

        # Build module name as string (handling relative imports)
        module_name = ""
        if updated_node.module:
            parts = []
            node: cst.BaseExpression = updated_node.module
            while isinstance(node, cst.Attribute):
                parts.append(node.attr.value)
                node = node.value
            if isinstance(node, cst.Name):
                parts.append(node.value)
            module_name = ".".join(reversed(parts))

        # Add relative dots prefix
        if updated_node.relative:
            dots = "." * len(updated_node.relative)
            full_module = dots + module_name
        else:
            full_module = module_name

        # Remove entire imports only for known third-party legacy modules.
        # Project-specific modules are handled name-by-name below so unrelated
        # helpers imported from the same module are preserved.
        is_old_module = module_name in _OLD_IMPORT_FROM_MODULES
        if not is_old_module:
            for old_mod in _OLD_IMPORT_FROM_MODULES:
                if module_name.endswith(f".{old_mod}"):
                    is_old_module = True
                    break

        if is_old_module:
            self.changes.append(f"REMOVE IMPORT │ from {full_module} import ...")
            return cst.RemovalSentinel.REMOVE

        # Remove specific old names from ANY import (e.g. get_db_connection, DictCursor)
        if isinstance(updated_node.names, (list, tuple)):
            new_names = []
            removed = []
            for alias in updated_node.names:
                name = alias.name.value if isinstance(alias.name, cst.Name) else str(alias.name)
                if name in _OLD_IMPORT_NAMES:
                    removed.append(name)
                else:
                    new_names.append(alias)

            if removed:
                self.changes.extend([f"REMOVE IMPORT │ {name} (from {full_module})" for name in removed])
                if not new_names:
                    return cst.RemovalSentinel.REMOVE
                # Clean trailing commas
                cleaned = []
                for i, alias in enumerate(new_names):
                    cleaned.append(alias.with_changes(comma=cst.MaybeSentinel.DEFAULT))
                return updated_node.with_changes(names=cleaned)

        return updated_node

    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import | cst.RemovalSentinel:
        """Remove: import pymysql, etc."""
        if isinstance(updated_node.names, (list, tuple)):
            for alias in updated_node.names:
                name = alias.name.value if isinstance(alias.name, cst.Name) else ""
                if isinstance(alias.name, cst.Attribute):
                    parts = []
                    node: cst.BaseExpression = alias.name
                    while isinstance(node, cst.Attribute):
                        parts.append(node.attr.value)
                        node = node.value
                    if isinstance(node, cst.Name):
                        parts.append(node.value)
                    name = ".".join(reversed(parts))
                if name in _OLD_IMPORT_MODULES:
                    self.changes.append(f"REMOVE IMPORT │ import {name}")
                    return cst.RemovalSentinel.REMOVE

        return updated_node


# =============================================================================
# LibCST Transformer: Remove dead code patterns
# =============================================================================

class RemoveDeadCodeTransformer(cst.CSTTransformer):
    """Remove dead code patterns left over from boilerplate removal."""

    def __init__(self):
        self.changes: list[str] = []

    def leave_If(
        self, original_node: cst.If, updated_node: cst.If
    ) -> cst.If | cst.RemovalSentinel:
        """Remove: if 'connection' in locals(): pass"""

        # Match: if 'connection'/'conn' in locals():
        if m.matches(updated_node.test, m.Comparison()):
            test = updated_node.test
            if isinstance(test, cst.Comparison):
                # Check for string 'connection' or 'conn'
                left = test.left
                if isinstance(left, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString)):
                    left_val = ""
                    if isinstance(left, cst.SimpleString):
                        evaluated = left.evaluated_value
                        left_val = evaluated if isinstance(evaluated, str) else left.value.strip("'\"")
                    if left_val in ("connection", "conn", "cursor"):
                        # Check if body is just pass
                        body = updated_node.body
                        if isinstance(body, cst.IndentedBlock):
                            stmts = body.body
                            if len(stmts) == 1:
                                inner = stmts[0]
                                if isinstance(inner, cst.SimpleStatementLine):
                                    if len(inner.body) == 1 and isinstance(inner.body[0], cst.Pass):
                                        self.changes.append(f"REMOVE DEAD │ if '{left_val}' in locals(): pass")
                                        return cst.RemovalSentinel.REMOVE

        return updated_node

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
        """Remove standalone 'pass' that's not needed, and 'if not connection: return None' patterns."""

        for stmt in updated_node.body:
            # Remove: if not connection: (orphaned check)
            if isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.Call):
                pass  # Keep expressions

        return updated_node


# =============================================================================
# Import deduplication (string-based, reliable for this specific case)
# =============================================================================

def dedup_imports(source: str) -> tuple[str, list[str]]:
    """
    Deduplicate import lines and logger assignments in Python source code.

    Handles:
    - Exact duplicate import lines
    - from X import A vs from X import A, B (keeps the broader one)
    - Duplicate logger = logging.getLogger(__name__) lines
    - Duplicate logging.basicConfig() calls

    Returns (cleaned_source, list_of_changes).
    """
    lines = source.split("\n")
    seen_imports: set[str] = set()
    seen_from_modules: dict[str, set[str]] = {}  # module -> set of imported names
    seen_logger: bool = False
    seen_basicconfig: bool = False
    result_lines: list[str] = []
    changes: list[str] = []

    import_re = re.compile(r"^\s*(from\s+\S+\s+import\s+.+|import\s+\S+)")
    from_import_re = re.compile(r"^\s*from\s+(\S+)\s+import\s+(.+)")
    logger_re = re.compile(r"^\s*logger\s*=\s*logging\.getLogger\(")
    basicconfig_re = re.compile(r"^\s*logging\.basicConfig\(")

    for line in lines:
        stripped = line.strip()

        # Dedup: logger = logging.getLogger(...)
        if logger_re.match(stripped):
            if seen_logger:
                changes.append(f"DEDUP │ {stripped}")
                continue
            seen_logger = True
            result_lines.append(line)
            continue

        # Dedup: logging.basicConfig(...)
        if basicconfig_re.match(stripped):
            if seen_basicconfig:
                changes.append(f"DEDUP │ {stripped}")
                continue
            seen_basicconfig = True
            result_lines.append(line)
            continue

        # Dedup imports
        from_match = from_import_re.match(stripped)
        if from_match:
            module = from_match.group(1)
            names = {n.strip() for n in from_match.group(2).split(",")}

            if module in seen_from_modules:
                existing = seen_from_modules[module]
                if names.issubset(existing):
                    # All names already imported
                    changes.append(f"DEDUP IMPORT │ {stripped}")
                    continue
                else:
                    # New names — merge into existing
                    seen_from_modules[module].update(names)
                    result_lines.append(line)
                    continue
            else:
                seen_from_modules[module] = names
                result_lines.append(line)
                continue

        match = import_re.match(stripped)
        if match:
            normalized = re.sub(r"\s+", " ", stripped)
            if normalized in seen_imports:
                changes.append(f"DEDUP IMPORT │ {stripped}")
                continue
            seen_imports.add(normalized)

        result_lines.append(line)

    return "\n".join(result_lines), changes


# =============================================================================
# Remove empty blocks after cleanup
# =============================================================================

def remove_orphaned_checks(source: str) -> tuple[str, list[str]]:
    """
    Remove orphaned connection check patterns that remain after boilerplate removal.

    Patterns like:
        if not connection:
            logger.error("Failed to get database connection")
            return None
    """
    changes: list[str] = []

    # Pattern: if not connection:\n            ...\n            return None
    patterns = [
        (
            r"\n\s+if not connection:\s*\n\s+logger\.error\([^\)]+\)\s*\n\s+return None\s*\n\s+\n",
            "\n",
            "if not connection: ... return None",
        ),
        (
            r"\n\s+if not conn:\s*\n\s+logger\.error\([^\)]+\)\s*\n\s+return None\s*\n\s+\n",
            "\n",
            "if not conn: ... return None",
        ),
        # Remove: if 'connection' in locals():\n            pass
        (
            r"\n\s+if\s+'connection'\s+in\s+locals\(\):\s*\n\s+pass\s*\n",
            "\n",
            "if 'connection' in locals(): pass",
        ),
        (
            r"\n\s+if\s+'conn'\s+in\s+locals\(\):\s*\n\s+pass\s*\n",
            "\n",
            "if 'conn' in locals(): pass",
        ),
    ]

    for pattern, replacement, description in patterns:
        matches = re.findall(pattern, source)
        if matches:
            changes.append(f"REMOVE DEAD │ {description} (x{len(matches)})")
            source = re.sub(pattern, replacement, source)

    return source, changes


# =============================================================================
# Fix db: Session parameter ordering (AI sometimes puts it after defaults)
# =============================================================================

def fix_db_session_param_order(source: str) -> tuple[str, list[str]]:
    """
    Fix: def foo(name=None, db: Session) → def foo(db: Session, name=None)

    Python requires non-default args before default args.
    AI sometimes places db: Session after parameters with defaults.
    """
    changes: list[str] = []

    # Pattern: def funcname(...default_param=..., db: Session...)
    pattern = re.compile(
        r"^(def\s+\w+\s*\()([^)]*,\s*)(db\s*:\s*Session)(\s*\))", re.MULTILINE
    )

    def _fix_match(m):
        prefix = m.group(1)      # "def funcname("
        before_db = m.group(2)   # "name=None, company_id=None, "
        db_param = m.group(3)    # "db: Session"
        suffix = m.group(4)      # ")"

        # Check if there's actually a default arg before db
        params_before = before_db.rstrip(", ")
        if "=" in params_before:
            changes.append(f"FIX PARAM ORDER │ {prefix.strip()}: db: Session moved to first")
            return f"{prefix}{db_param}, {params_before}{suffix}"
        return m.group(0)

    source = pattern.sub(_fix_match, source)
    return source, changes


# =============================================================================
# Remove consecutive blank lines (max 2)
# =============================================================================

def normalize_blank_lines(source: str) -> str:
    """Collapse 3+ consecutive blank lines into 2."""
    return re.sub(r"\n{4,}", "\n\n\n", source)


# =============================================================================
# Detect incorrect .alias() on ORM models → should use aliased()
# =============================================================================

def fix_orm_alias_pattern(source: str) -> tuple[str, list[str]]:
    """
    Detect .alias('...') on ORM model classes, which is invalid in SQLAlchemy ORM.

    Pattern: Model.alias('name') → should be aliased(Model) from sqlalchemy.orm
    This is a common AI hallucination. We flag it with a TODO comment.

    Returns (cleaned_source, list_of_changes).
    """
    import re
    changes: list[str] = []
    lines = source.split("\n")

    # Pattern: SomeModel.alias('...')  or  SomeModel.alias("...")
    alias_pattern = re.compile(r'(\b[A-Z]\w+)\.alias\s*\(\s*[\'"](\w+)[\'"]\s*\)')

    new_lines = []
    for i, line in enumerate(lines):
        match = alias_pattern.search(line)
        if match:
            model_name = match.group(1)
            alias_name = match.group(2)

            # Add TODO marker above the problematic line
            indent = len(line) - len(line.lstrip())
            marker = " " * indent + f"# TODO: [MANUAL REVIEW] Replace {model_name}.alias('{alias_name}') with: {alias_name.title()} = aliased({model_name})  # from sqlalchemy.orm import aliased"
            new_lines.append(marker)
            changes.append(f"ALIAS │ L{i+1}: Flagged invalid .alias() on {model_name} — use aliased({model_name})")
            logger.warning("  [WARN] L%d: %s.alias('%s') → should use aliased(%s)", i + 1, model_name, alias_name, model_name)

        new_lines.append(line)

    return "\n".join(new_lines), changes


# =============================================================================
# Detect joinedload() on FK columns (should be on relationships)
# =============================================================================

def fix_joinedload_on_fk_column(source: str) -> tuple[str, list[str]]:
    """
    Detect joinedload(Model.some_id) where the attribute ends with _id.

    joinedload() expects a relationship attribute, not an FK column.
    Passing an FK column (e.g. partner_id) causes ArgumentError at runtime.

    Returns (cleaned_source, list_of_changes).
    """
    import re
    changes: list[str] = []
    lines = source.split("\n")

    # Pattern: joinedload(Model.attr_id)
    jl_pattern = re.compile(r'joinedload\(\s*([A-Z]\w+)\.(\w+_id)\s*\)')

    new_lines = []
    for i, line in enumerate(lines):
        match = jl_pattern.search(line)
        if match:
            model_name = match.group(1)
            fk_col = match.group(2)

            indent = len(line) - len(line.lstrip())
            marker = " " * indent + f"# TODO: [MANUAL REVIEW] joinedload({model_name}.{fk_col}) — {fk_col} is an FK column, not a relationship. Use outerjoin() or remove."
            new_lines.append(marker)
            changes.append(f"JOINEDLOAD │ L{i+1}: joinedload on FK column {model_name}.{fk_col}")
            logger.warning("  [WARN] L%d: joinedload(%s.%s) — FK column, not relationship", i + 1, model_name, fk_col)

        new_lines.append(line)

    return "\n".join(new_lines), changes


# =============================================================================
# Detect func.case() → should be standalone case()
# =============================================================================

def fix_func_case_pattern(source: str) -> tuple[str, list[str]]:
    """
    Detect func.case(...) which should be case(...) from sqlalchemy.

    func.case() tries to generate SQL `CASE(...)` function call, which is wrong.
    The correct pattern is `case((condition, value), else_=default)`.

    Returns (cleaned_source, list_of_changes).
    """
    import re
    changes: list[str] = []

    # Count occurrences
    occurrences = [(m.start(), m.group()) for m in re.finditer(r'func\.case\s*\(', source)]

    if occurrences:
        # Auto-fix: replace func.case( with case(
        new_source = re.sub(r'func\.case\s*\(', 'case(', source)
        for idx, (pos, match_text) in enumerate(occurrences):
            line_no = source[:pos].count('\n') + 1
            changes.append(f"FUNC_CASE │ L{line_no}: Replaced func.case() with case()")
            logger.warning("  [WARN] L%d: func.case() → case() (auto-fixed)", line_no)
        return new_source, changes

    return source, changes


# =============================================================================
# Fix logger= between decorator and def (SyntaxError)
# =============================================================================

def fix_logger_between_decorator(source: str) -> tuple[str, list[str]]:
    """
    Remove `logger = logging.getLogger(__name__)` lines placed between
    a decorator (@router, @staticmethod, etc.) and its function definition.

    This is a common AI hallucination that causes SyntaxError because Python
    does not allow non-decorator statements between @ and def.

    Returns (cleaned_source, list_of_changes).
    """
    changes: list[str] = []
    lines = source.split("\n")
    to_remove: set[int] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "logger = logging.getLogger(__name__)":
            # Check if previous non-blank line is a decorator
            j = i - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            if j >= 0 and lines[j].strip().startswith("@"):
                # Confirm next non-blank line is def/async def
                k = i + 1
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
                if k < len(lines) and (lines[k].strip().startswith("def ") or lines[k].strip().startswith("async def")):
                    to_remove.add(i)
                    changes.append(f"LOGGER_DEC │ L{i+1}: Removed logger= between @decorator and def")
                    logger.warning("  [WARN] L%d: Removed misplaced logger= between decorator and def", i + 1)

    if to_remove:
        lines = [line for idx, line in enumerate(lines) if idx not in to_remove]

    return "\n".join(lines), changes


# =============================================================================
# Deduplicate functions (same name defined multiple times at same scope)
# =============================================================================

def dedup_functions(source: str) -> tuple[str, list[str]]:
    """
    Detect and remove duplicate function definitions at the same scope.

    When the converter processes the same function twice, both copies get
    appended to the output. This keeps the FIRST definition and removes
    subsequent duplicates.

    Handles:
    - Top-level function duplicates
    - Class method duplicates (within the same class)
    - Skips property/setter pairs (those are legitimate duplicates)

    Returns (cleaned_source, list_of_changes).
    """
    import ast
    changes: list[str] = []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, changes

    lines = source.split("\n")
    to_remove_ranges: list[tuple[int, int]] = []  # (start_idx, end_idx) 0-indexed

    def _find_dups_in_scope(nodes, scope_name: str = ""):
        """Find duplicate function defs within a list of child nodes."""
        seen: dict[str, int] = {}  # name -> first_lineno
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip property setters — they legitimately share names with @property
                is_setter = any(
                    isinstance(d, ast.Attribute) and d.attr == "setter"
                    for d in node.decorator_list
                )
                if is_setter:
                    continue

                qualified = f"{scope_name}.{node.name}" if scope_name else node.name
                if node.name in seen:
                    # Duplicate! Mark for removal (0-indexed)
                    start = node.lineno - 1
                    end = node.end_lineno or node.lineno  # exclusive
                    # Include decorators above the function
                    if node.decorator_list:
                        start = node.decorator_list[0].lineno - 1
                    to_remove_ranges.append((start, end))
                    changes.append(
                        f"DEDUP │ L{node.lineno}: Removed duplicate {qualified}() "
                        f"(first defined at L{seen[node.name]})"
                    )
                    logger.warning(
                        "  [WARN] L%d: Removed duplicate %s() (first at L%d)",
                        node.lineno, qualified, seen[node.name]
                    )
                else:
                    seen[node.name] = node.lineno

            # Recurse into classes
            if isinstance(node, ast.ClassDef):
                _find_dups_in_scope(list(ast.iter_child_nodes(node)), node.name)

    _find_dups_in_scope(list(ast.iter_child_nodes(tree)))

    if to_remove_ranges:
        # Sort by start descending to remove from bottom-up
        to_remove_ranges.sort(key=lambda r: r[0], reverse=True)
        for start, end in to_remove_ranges:
            del lines[start:end]

        new_source = "\n".join(lines)
        # Verify syntax after removal
        try:
            ast.parse(new_source)
        except SyntaxError:
            logger.warning("  [WARN] dedup_functions caused SyntaxError — reverting")
            return source, []

        return new_source, changes

    return source, changes


# =============================================================================
# Flag unassigned db.execute() followed by _result usage
# =============================================================================

def flag_unassigned_execute(source: str) -> tuple[str, list[str]]:
    """
    Detect db.execute(text(...)) calls where the return value is not captured,
    followed by usage of undefined `_result` variable.

    The AI converter often produces:
        db.execute(text("..."), params)      # no assignment
        row = _result.mappings().first()      # _result is undefined!

    Should be:
        result = db.execute(text("..."), params)
        row = result.mappings().first()

    This flags (not auto-fixes) because the variable name choice depends on context.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []

    # Pattern: _result usage (the undefined variable)
    result_usage_re = re.compile(r"\b_result\b")

    # First pass: flag _result usages with TODO
    for i, line in enumerate(lines):
        if result_usage_re.search(line):
            stripped = line.strip()
            # Skip comment lines and string literals
            if stripped.startswith("#") or stripped.startswith(('"', "'")):
                new_lines.append(line)
                continue
            # Skip lines that are assigning TO _result (unlikely but safe)
            if stripped.startswith("_result") and "=" in stripped and not stripped.startswith("_result."):
                new_lines.append(line)
                continue

            indent = len(line) - len(line.lstrip())
            marker = " " * indent + "# TODO: [POST-PROCESS] _result is undefined — assign db.execute() result to a variable above"
            # Only add marker if not already present
            if i > 0 and "_result is undefined" not in lines[i - 1]:
                new_lines.append(marker)
                changes.append(f"UNASSIGNED_EXEC │ L{i+1}: _result used without assignment")
                logger.warning("  [WARN] L%d: _result used — db.execute() result not captured", i + 1)

        new_lines.append(line)

    return "\n".join(new_lines), changes


# =============================================================================
# Flag MySQL %s placeholders inside text() queries
# =============================================================================

def flag_mysql_placeholders(source: str) -> tuple[str, list[str]]:
    """
    Detect %s placeholders inside text() SQL queries.

    SQLAlchemy text() requires :named parameters, not MySQL-style %s.
    Example:
        db.execute(text("SELECT * FROM t WHERE id = %s"), (id,))  # WRONG
        db.execute(text("SELECT * FROM t WHERE id = :id"), {'id': id})  # CORRECT

    Flags with TODO comment — cannot auto-fix because param names are context-dependent.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []

    # We look for lines containing text("...%s...") or text('...%s...')
    # or multi-line text() blocks containing %s
    in_text_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip comment lines
        if stripped.startswith("#"):
            new_lines.append(line)
            continue

        # Track multi-line text() blocks
        if "text(" in line and ('"""' in line or "'''" in line):
            in_text_block = True
        if in_text_block and line.count('"""') >= 2:
            in_text_block = False
        if in_text_block and line.count("'''") >= 2:
            in_text_block = False

        # Check for %s in SQL context (inside text() or in a text block)
        has_percent_s = "%s" in line
        is_sql_context = (
            "text(" in line
            or in_text_block
            or (has_percent_s and any(kw in line.upper() for kw in
                ["SELECT", "INSERT", "UPDATE", "DELETE", "WHERE", "VALUES", "SET "]))
        )

        if has_percent_s and is_sql_context:
            indent = len(line) - len(line.lstrip())
            marker = " " * indent + "# TODO: [POST-PROCESS] Replace %s with :named parameters for SQLAlchemy text()"
            # Only add if not already flagged
            if i > 0 and "Replace %s with :named" not in lines[i - 1]:
                new_lines.append(marker)
                changes.append(f"MYSQL_PLACEHOLDER │ L{i+1}: %s placeholder in SQL query")
                logger.warning("  [WARN] L%d: %%s placeholder found in text() query", i + 1)

        new_lines.append(line)

    return "\n".join(new_lines), changes


# =============================================================================
# Expanded if-not-connection/conn removal (all return types)
# =============================================================================

def remove_connection_checks_expanded(source: str) -> tuple[str, list[str]]:
    """
    Remove all variations of connection/conn availability checks.

    The original remove_orphaned_checks() only handles:
        if not connection: logger.error(...) return None

    This expanded version handles ALL return patterns:
        if not connection:  return []
        if not conn:        return {"status": "error", ...}
        if not connection:  return None
        if not conn:        return False

    These are dead code in SQLAlchemy session context because the session
    is always injected via dependency injection.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []

    # Variable names to check
    conn_vars = {"connection", "conn", "cursor"}

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Match: if not connection/conn/cursor:
        matched_var = None
        for var in conn_vars:
            if stripped == f"if not {var}:":
                matched_var = var
                break

        if matched_var:
            # Collect the entire if-block (indented lines following)
            block_indent = len(lines[i]) - len(lines[i].lstrip())
            block_lines = [lines[i]]
            j = i + 1
            while j < len(lines):
                if lines[j].strip() == "":
                    block_lines.append(lines[j])
                    j += 1
                    continue
                line_indent = len(lines[j]) - len(lines[j].lstrip())
                if line_indent > block_indent:
                    block_lines.append(lines[j])
                    j += 1
                else:
                    break

            # Check if block ends with a return statement
            has_return = any(bl.strip().startswith("return") for bl in block_lines)
            if has_return:
                changes.append(f"CONN_CHECK │ L{i+1}: Removed 'if not {matched_var}:' block ({len(block_lines)} lines)")
                logger.warning("  [WARN] L%d: Removed 'if not %s:' dead code block", i + 1, matched_var)
                i = j  # Skip entire block
                continue

        new_lines.append(lines[i])
        i += 1

    return "\n".join(new_lines), changes


# =============================================================================
# Flag cursor variable references (should be db session)
# =============================================================================

def flag_cursor_references(source: str) -> tuple[str, list[str]]:
    """
    Detect usage of 'cursor' variable which is a MySQL pattern.

    In SQLAlchemy, there's no cursor — you use db (Session) directly.
    Common patterns:
        cursor.execute(...)  → db.execute(text(...), params)
        cursor.fetchone()    → result.mappings().first()
        cursor.fetchall()    → result.mappings().all()
        cursor.lastrowid     → result.lastrowid

    Flags with TODO — context-dependent replacement.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []

    # Pattern: cursor.method() or cursor.attribute (not in comments/strings)
    cursor_re = re.compile(r"\bcursor\.(execute|fetchone|fetchall|fetchmany|lastrowid|rowcount|close|description)")
    # Also catch standalone cursor usage as variable
    cursor_assign_re = re.compile(r"\bcursor\s*=\s*")
    # Catch cursor passed as function argument
    cursor_arg_re = re.compile(r"[,(]\s*cursor\s*[,)]")

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            new_lines.append(line)
            continue

        has_cursor = (
            cursor_re.search(line)
            or cursor_assign_re.search(stripped)
            or cursor_arg_re.search(line)
        )

        if has_cursor:
            indent = len(line) - len(line.lstrip())
            marker = " " * indent + "# TODO: [POST-PROCESS] 'cursor' is a MySQL pattern — use 'db' (Session) instead"
            if i > 0 and "cursor' is a MySQL pattern" not in lines[i - 1]:
                new_lines.append(marker)
                changes.append(f"CURSOR_REF │ L{i+1}: cursor usage detected")
                logger.warning("  [WARN] L%d: cursor variable used — should be db (Session)", i + 1)

        new_lines.append(line)

    return "\n".join(new_lines), changes


# =============================================================================
# Flag functions using db.execute but missing db: Session parameter
# =============================================================================

def flag_missing_db_session_param(source: str) -> tuple[str, list[str]]:
    """
    Detect functions that call db.execute() or db.query() but don't have
    'db: Session' in their parameter list.

    This catches AI conversion failures where the function signature
    wasn't updated but the body was partially converted.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []

    # Parse function boundaries
    func_re = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\((.*)")
    db_usage_re = re.compile(r"\bdb\.(execute|query|add|flush|commit|rollback|merge|delete|refresh)\b")

    i = 0
    while i < len(lines):
        func_match = func_re.match(lines[i])

        if func_match:
            indent = func_match.group(1)
            func_name = func_match.group(3)
            indent_len = len(indent)

            # Collect the full function signature (may span multiple lines)
            sig_lines = [lines[i]]
            j = i + 1
            # If the def line doesn't close with ), collect continuation
            open_parens = lines[i].count("(") - lines[i].count(")")
            while j < len(lines) and open_parens > 0:
                sig_lines.append(lines[j])
                open_parens += lines[j].count("(") - lines[j].count(")")
                j += 1

            full_sig = " ".join(sl.strip() for sl in sig_lines)
            has_db_param = "db:" in full_sig or "db :" in full_sig

            # Collect function body
            body_lines = []
            while j < len(lines):
                if lines[j].strip() == "":
                    body_lines.append(lines[j])
                    j += 1
                    continue
                body_indent = len(lines[j]) - len(lines[j].lstrip())
                if body_indent > indent_len:
                    body_lines.append(lines[j])
                    j += 1
                else:
                    break

            # Check if body uses db.
            body_text = "\n".join(body_lines)
            uses_db = db_usage_re.search(body_text)

            if uses_db and not has_db_param:
                marker = indent + f"# TODO: [POST-PROCESS] {func_name}() uses db.execute/query but has no 'db: Session' parameter"
                if "has no 'db: Session'" not in lines[i - 1] if i > 0 else True:
                    new_lines.append(marker)
                    changes.append(f"MISSING_DB │ L{i+1}: {func_name}() missing db: Session parameter")
                    logger.warning("  [WARN] L%d: %s() uses db but has no db: Session param", i + 1, func_name)

            # Add all collected lines
            new_lines.extend(sig_lines)
            new_lines.extend(body_lines)
            i = j
            continue

        new_lines.append(lines[i])
        i += 1

    return "\n".join(new_lines), changes


# =============================================================================
# Remove finally: pass blocks (dead code from AI conversion)
# =============================================================================

def remove_finally_pass(source: str) -> tuple[str, list[str]]:
    """
    Remove 'finally: pass' blocks that are remnants of old try/finally
    connection cleanup patterns.

    Handles two cases:
      Case A: try/except/finally:pass → remove only finally:pass
      Case B: try/finally:pass (NO except) → unwrap try block entirely,
              keeping the body at the same indent level

    Case B is critical: removing only finally:pass would leave an orphaned
    try: with no except/finally, causing SyntaxError.
    """
    changes: list[str] = []
    lines = source.split("\n")
    new_lines: list[str] = []
    count = 0

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Detect "finally:" line
        if stripped == "finally:":
            finally_indent = len(lines[i]) - len(lines[i].lstrip())

            # Check if the block is just "pass" (possibly with blank lines)
            j = i + 1
            block_is_pass_only = False
            pass_end = j

            while j < len(lines):
                inner_stripped = lines[j].strip()
                if inner_stripped == "":
                    j += 1
                    continue
                inner_indent = len(lines[j]) - len(lines[j].lstrip())
                if inner_indent > finally_indent and inner_stripped == "pass":
                    block_is_pass_only = True
                    pass_end = j + 1
                    # Skip trailing blank lines after pass
                    while pass_end < len(lines) and lines[pass_end].strip() == "":
                        pass_end += 1
                    break
                else:
                    break  # Not "pass" — real finally block, keep it

            if not block_is_pass_only:
                new_lines.append(lines[i])
                i += 1
                continue

            # Now check: does this try block have an except clause?
            # Walk backwards from finally: to find the matching try:
            has_except = False
            try_line_idx = None

            for k in range(i - 1, -1, -1):
                k_stripped = lines[k].strip()
                k_indent = len(lines[k]) - len(lines[k].lstrip())

                if k_stripped == "" :
                    continue

                # Same indent level except/else before finally
                if k_indent == finally_indent and k_stripped.startswith("except"):
                    has_except = True
                    break
                if k_indent == finally_indent and k_stripped.startswith("else:"):
                    continue  # else: can appear between except and finally
                # Same indent try: — this is our matching try
                if k_indent == finally_indent and k_stripped.startswith("try:"):
                    try_line_idx = k
                    break
                # Body line (deeper indent) — continue searching
                if k_indent > finally_indent:
                    continue
                # We've gone past the try block scope
                break

            if has_except:
                # Case A: try/except/finally:pass → remove only finally:pass
                count += 1
                i = pass_end
                continue
            elif try_line_idx is not None:
                # Case B: try/finally:pass (no except) → unwrap try body
                # Remove the "try:" line from new_lines (already added)
                # Find and remove the try: line from new_lines
                try_line_content = lines[try_line_idx]
                # Find and pop the try: line from new_lines
                removed_try = False
                for rev_idx in range(len(new_lines) - 1, -1, -1):
                    if new_lines[rev_idx] == try_line_content and not removed_try:
                        new_lines.pop(rev_idx)
                        removed_try = True
                        break

                if removed_try:
                    # Dedent all body lines between try: and finally:
                    # The body lines are already in new_lines (between try_line and current)
                    # We need to dedent them by one level (4 spaces or 1 tab)
                    body_start_in_new = rev_idx
                    body_end_in_new = len(new_lines)
                    for bl_idx in range(body_start_in_new, body_end_in_new):
                        bl = new_lines[bl_idx]
                        if bl.strip() == "":
                            continue
                        # Dedent by 4 spaces or 1 tab
                        if bl.startswith("    "):
                            new_lines[bl_idx] = bl[4:]
                        elif bl.startswith("\t"):
                            new_lines[bl_idx] = bl[1:]

                    count += 1
                    i = pass_end
                    continue
                else:
                    # Couldn't find try: in output — just remove finally:pass
                    count += 1
                    i = pass_end
                    continue
            else:
                # No matching try found — just remove finally:pass
                count += 1
                i = pass_end
                continue

        new_lines.append(lines[i])
        i += 1

    if count > 0:
        changes.append(f"FINALLY_PASS │ Removed finally: pass blocks (x{count})")
        logger.warning("  [WARN] Removed %d 'finally: pass' dead code blocks", count)

    return "\n".join(new_lines), changes


# =============================================================================
# Main post-process function
# =============================================================================

def post_process_file(filepath: Path) -> tuple[str, list[str]]:
    """
    Apply all post-processing steps to a single file.

    Args:
        filepath: Path to the Python file to clean up.

    Returns:
        Tuple of (cleaned_source, list_of_changes).
    """
    source = filepath.read_text(encoding="utf-8")
    all_changes: list[str] = []

    # Step 1: Remove old imports (LibCST)
    try:
        tree = cst.parse_module(source)

        transformer = RemoveOldImportsTransformer()
        tree = tree.visit(transformer)
        all_changes.extend(transformer.changes)

        transformer2 = RemoveDeadCodeTransformer()
        tree = tree.visit(transformer2)
        all_changes.extend(transformer2.changes)

        source = tree.code
    except cst.ParserSyntaxError as e:
        logger.warning("Could not parse %s for CST cleanup: %s", filepath.name, e)

    # Step 2: Dedup imports (string-based)
    source, dedup_changes = dedup_imports(source)
    all_changes.extend(dedup_changes)

    # Step 3: Fix db: Session parameter ordering
    source, param_changes = fix_db_session_param_order(source)
    all_changes.extend(param_changes)

    # Step 4: Remove orphaned patterns (regex) — original narrow patterns
    source, orphan_changes = remove_orphaned_checks(source)
    all_changes.extend(orphan_changes)

    # Step 5: Normalize blank lines
    source = normalize_blank_lines(source)

    # Step 6: Detect .alias() on ORM models (should be aliased() from sqlalchemy.orm)
    source, alias_changes = fix_orm_alias_pattern(source)
    all_changes.extend(alias_changes)

    # Step 7: Detect joinedload() on FK columns (should be on relationships)
    source, jl_changes = fix_joinedload_on_fk_column(source)
    all_changes.extend(jl_changes)

    # Step 8: Fix func.case() → case() (auto-fix)
    source, fc_changes = fix_func_case_pattern(source)
    all_changes.extend(fc_changes)

    # Step 9: Remove logger= between decorator and def (SyntaxError fix)
    source, logger_dec_changes = fix_logger_between_decorator(source)
    all_changes.extend(logger_dec_changes)

    # Step 10: Deduplicate functions (same name at same scope)
    source, dedup_changes2 = dedup_functions(source)
    all_changes.extend(dedup_changes2)

    # --- NEW RULES (v2) ---

    # Step 11: Expanded connection/conn check removal (all return types)
    source, conn_changes = remove_connection_checks_expanded(source)
    all_changes.extend(conn_changes)

    # Step 12: Remove finally: pass blocks (auto-fix, safe)
    source, fp_changes = remove_finally_pass(source)
    all_changes.extend(fp_changes)

    # Step 13: Flag unassigned db.execute() with _result usage
    source, exec_changes = flag_unassigned_execute(source)
    all_changes.extend(exec_changes)

    # Step 14: Flag MySQL %s placeholders in text() queries
    source, ph_changes = flag_mysql_placeholders(source)
    all_changes.extend(ph_changes)

    # Step 15: Flag cursor variable references
    source, cur_changes = flag_cursor_references(source)
    all_changes.extend(cur_changes)

    # Step 16: Flag functions missing db: Session parameter
    source, db_changes = flag_missing_db_session_param(source)
    all_changes.extend(db_changes)

    return source, all_changes


def post_process_directory(source_dir: Path, dry_run: bool = False) -> dict:
    """
    Apply post-processing to all Python files in a directory.

    Uses CodemodReporter to generate full diff reports with per-file breakdown.

    Args:
        source_dir: Directory containing Python files.
        dry_run: If True, don't write changes.

    Returns:
        Summary dict with results.
    """
    import difflib
    import json
    from datetime import datetime, timezone

    _REPORTS_DIR = REPORTS_DIR / "post_process"

    py_files = sorted(source_dir.rglob("*.py"))
    py_files = [f for f in py_files if "__pycache__" not in str(f)]

    start_time = datetime.now(timezone.utc)

    results: dict[str, Any] = {
        "total_files": len(py_files),
        "files_cleaned": 0,
        "files_unchanged": 0,
        "total_changes": 0,
        "changes_by_type": {},
    }

    # Collect per-file data for the report
    file_reports: list[dict[str, Any]] = []

    for filepath in py_files:
        cleaned, changes = post_process_file(filepath)
        original = filepath.read_text(encoding="utf-8")

        if cleaned != original:
            results["files_cleaned"] += 1
            results["total_changes"] += len(changes)

            # Generate unified diff
            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                cleaned.splitlines(keepends=True),
                fromfile=f"original/{filepath.name}",
                tofile=f"modified/{filepath.name}",
                n=2,
            ))

            file_reports.append({
                "filename": str(filepath.relative_to(source_dir)),
                "status": "modified",
                "changes": changes,
                "change_count": len(changes),
                "diff": "".join(diff_lines),
            })

            if not dry_run:
                atomic_write_text(filepath, cleaned)
                logger.info("  [PASS] %s (%d changes)", filepath.name, len(changes))
            else:
                logger.info("  ○ %s (%d changes) [dry-run]", filepath.name, len(changes))

            for change in changes:
                ctype = change.split("│")[0].strip() if "│" in change else "OTHER"
                results["changes_by_type"][ctype] = results["changes_by_type"].get(ctype, 0) + 1
        else:
            results["files_unchanged"] += 1
            file_reports.append({
                "filename": str(filepath.relative_to(source_dir)),
                "status": "unchanged",
                "changes": [],
                "change_count": 0,
                "diff": "",
            })

    # ── Save reports ──────────────────────────────────────────────
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = start_time.strftime("%Y%m%d_%H%M%S")
    mode = "dryrun" if dry_run else "applied"
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    # ── TXT report (with full diffs) ──
    txt_path = _REPORTS_DIR / f"post_process_{mode}_{timestamp}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"  POST-PROCESS REPORT — {timestamp}\n")
        f.write(f"  Mode: {'DRY RUN' if dry_run else 'APPLIED'}\n")
        f.write(f"  Source: {source_dir}\n")
        f.write("=" * 70 + "\n\n")

        # Summary
        f.write(f"Files scanned:    {results['total_files']}\n")
        f.write(f"Files modified:   {results['files_cleaned']}\n")
        f.write(f"Files unchanged:  {results['files_unchanged']}\n")
        f.write(f"Total changes:    {results['total_changes']}\n")
        f.write(f"Duration:         {elapsed:.1f}s\n\n")

        # Change type breakdown
        if results["changes_by_type"]:
            f.write("CHANGE TYPE BREAKDOWN:\n")
            for ctype, count in sorted(results["changes_by_type"].items()):
                f.write(f"  {ctype:<25} {count:>5}x\n")
            f.write("\n")

        # Per-file details with diffs
        f.write("=" * 70 + "\n")
        f.write("  PER-FILE DETAILS\n")
        f.write("=" * 70 + "\n\n")

        for r in sorted(file_reports, key=lambda x: x["change_count"], reverse=True):
            if r["status"] == "unchanged":
                continue

            f.write(f"{'─' * 70}\n")
            f.write(f"  {r['filename']} — {r['change_count']} changes\n")
            f.write(f"{'─' * 70}\n")

            if r["changes"]:
                f.write("  Changes:\n")
                for change in r["changes"]:
                    f.write(f"    {change}\n")
                f.write("\n")

            if r["diff"]:
                f.write("  Diff:\n")
                f.write(r["diff"])
                f.write("\n\n")

        f.write("=" * 70 + "\n")

    logger.info("TXT report saved: %s", txt_path)

    # ── JSON report (without diffs, for programmatic use) ──
    json_path = _REPORTS_DIR / f"post_process_{mode}_{timestamp}.json"
    json_data = {
        "timestamp": start_time.isoformat(),
        "mode": mode,
        "source_dir": str(source_dir),
        "duration_seconds": round(elapsed, 1),
        "summary": {
            "total_files": results["total_files"],
            "files_cleaned": results["files_cleaned"],
            "files_unchanged": results["files_unchanged"],
            "total_changes": results["total_changes"],
            "changes_by_type": results["changes_by_type"],
        },
        "files": [
            {
                "filename": r["filename"],
                "status": r["status"],
                "change_count": r["change_count"],
                "changes": r["changes"],
            }
            for r in file_reports
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

    logger.info("JSON report saved: %s", json_path)

    # Store report paths in results for caller
    results["report_txt"] = str(txt_path)
    results["report_json"] = str(json_path)

    return results


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Post-process AI converter output: dedup imports, remove dead code.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-dir", help="Directory to post-process.")
    group.add_argument("--file", help="Single file to post-process.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag, only a preview is produced.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if args.file:
        filepath = Path(args.file)
        if not filepath.is_file():
            logger.error("File not found: %s", filepath)
            sys.exit(1)

        cleaned, changes = post_process_file(filepath)
        if changes:
            logger.info("Changes for %s:", filepath.name)
            for c in changes:
                logger.info("  %s", c)
            if not dry_run:
                atomic_write_text(filepath, cleaned)
                logger.info("File updated.")
            else:
                logger.info("Dry run — no changes written.")
        else:
            logger.info("No changes needed for %s", filepath.name)
    else:
        source_dir = Path(args.source_dir)
        if not source_dir.is_dir():
            logger.error("Directory not found: %s", source_dir)
            sys.exit(1)

        logger.info("Post-processing: %s", source_dir)
        results = post_process_directory(source_dir, dry_run=dry_run)

        # Console summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("  POST-PROCESS RESULTS" + ("  [DRY RUN]" if dry_run else ""))
        logger.info("=" * 60)
        logger.info("")
        logger.info("  Files scanned:     %d", results["total_files"])
        logger.info("  Files modified:    %d", results["files_cleaned"])
        logger.info("  Files unchanged:   %d", results["files_unchanged"])
        logger.info("  Total changes:     %d", results["total_changes"])
        logger.info("")
        if results["changes_by_type"]:
            logger.info("  CHANGE TYPE BREAKDOWN:")
            for ctype, count in sorted(results["changes_by_type"].items()):
                logger.info("    %-25s %5dx", ctype, count)
        logger.info("")
        logger.info("  [FILE] TXT Report: %s", results.get("report_txt", "N/A"))
        logger.info("  [FILE] JSON Report: %s", results.get("report_json", "N/A"))
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
