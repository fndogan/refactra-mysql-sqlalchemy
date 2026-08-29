"""
N+1 Query Detector — Post-conversion static analyzer.

Scans AI-converted code for potential N+1 query patterns using proper
AST analysis (no regex). Detects:
  1. db.query() / session.query() / db.execute() inside loops
  2. Relationship attribute access inside loops without eager loading
  3. Comprehensions containing query calls
  4. Hidden N+1 via function calls inside loops (inter-procedural)
  5. Pydantic from_orm / model_dump serializer N+1
  6. Dynamic session variable name detection (not hardcoded to 'db')

Features:
  - Single O(N) AST traversal using NodeVisitor pattern
  - SQLAlchemy model introspection for relationship detection
  - `# noqa: N1` suppression support
  - Severity scoring based on loop context
  - CI-friendly exit codes
  - Inter-procedural analysis (function calls in loops)

Usage:
    refactra-mysql n1 ./output
    refactra-mysql n1 ./output --models ./models.py
    refactra-mysql n1 ./output --ci
"""
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# ─── Known query method names (ORM + raw) ───
QUERY_METHODS = frozenset({
    "query",      # db.query(Model)
    "execute",    # db.execute(select(...)), session.execute(...)
    "scalars",    # session.scalars(select(...))
    "scalar",     # session.scalar(select(...))
    "get",        # session.get(Model, id)  — BUT NOT dict.get()
})

# Methods that are ONLY query methods when called on a Session-like object
# "get" is excluded from general detection because dict.get() causes FP
AMBIGUOUS_METHODS = frozenset({"get"})

# Known Session type annotations (used to detect session variable names)
SESSION_TYPE_NAMES = frozenset({
    "Session", "AsyncSession", "ScopedSession",
    "session", "Optional[Session]",
})

# Eager loading function names
EAGER_LOAD_FUNCS = frozenset({
    "joinedload", "selectinload", "subqueryload",
    "lazyload", "raiseload", "noload",
    "contains_eager", "immediateload",
})

# Suppression comment patterns
NOQA_PATTERNS = frozenset({"noqa: N1", "noqa:N1", "ignore-n1", "noqa: n1"})

# Serializer methods that trigger lazy loading
SERIALIZER_METHODS = frozenset({
    "from_orm", "model_validate", "model_dump",
    "dict", "to_dict", "as_dict", "serialize",
})


@dataclass
class N1Warning:
    file: str
    line: int
    function: str
    pattern: str
    severity: str       # "high", "medium", "low"
    suggestion: str
    loop_depth: int = 1
    context: str = ""


@dataclass
class _LoopContext:
    """Tracks state while inside a loop during AST walk."""
    start_line: int
    end_line: int
    depth: int = 1
    has_eager_load: bool = False
    loop_var: Optional[str] = None


class _N1Visitor(ast.NodeVisitor):
    """
    Single-pass AST visitor that detects N+1 query patterns.

    Key improvements over naive approach:
    - Dynamically detects session variable names from type annotations
    - Inter-procedural: warns on function calls in loops
    - Supports # noqa: N1 suppression
    - Detects Pydantic serializer N+1
    - Excludes dict.get() false positives
    """

    def __init__(
        self,
        filepath: str,
        source_lines: list[str],
        known_relationships: set[str] | None = None,
        known_columns: set[str] | None = None,
        functions_with_queries: set[str] | None = None,
        custom_relationship_hints: set[str] | None = None,
    ):
        self.filepath = filepath
        self.lines = source_lines
        self.warnings: list[N1Warning] = []
        self._func_stack: list[str] = []
        self._loop_stack: list[_LoopContext] = []
        self._known_relationships = known_relationships or set()
        self._known_columns = known_columns or set()
        self._functions_with_queries = functions_with_queries or set()
        self._custom_hints = custom_relationship_hints or set()

        # Dynamically discovered session variable names
        self._session_vars: set[str] = set()
        # Functions in this file that contain queries (for inter-proc)
        self._local_query_funcs: set[str] = set()
        # Whether current function has eager loading calls (selectinload/joinedload)
        self._func_has_eager_load: bool = False

    @property
    def _current_func(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<module>"

    @property
    def _in_loop(self) -> bool:
        return len(self._loop_stack) > 0

    @property
    def _loop_depth(self) -> int:
        return len(self._loop_stack)

    def _is_suppressed(self, lineno: int) -> bool:
        """Check if a line has a # noqa: N1 suppression comment."""
        if 0 < lineno <= len(self.lines):
            line = self.lines[lineno - 1]
            return any(pat in line for pat in NOQA_PATTERNS)
        return False

    def _add_warning(self, **kwargs) -> None:
        """Add warning only if not suppressed."""
        line = kwargs.get("line", 0)
        if not self._is_suppressed(line):
            self.warnings.append(N1Warning(**kwargs))

    # ─── Pre-scan: discover session variable names ───

    def _prescan_session_vars(self, tree: ast.AST) -> None:
        """
        First pass: find all variables annotated with Session type.
        This lets us detect queries regardless of variable name.

        Examples detected:
          def foo(db: Session): ...
          def bar(session: AsyncSession): ...
          def baz(s: Session, company_id: int): ...
        """
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args:
                    if arg.annotation:
                        ann_name = self._get_annotation_name(arg.annotation)
                        if ann_name in SESSION_TYPE_NAMES:
                            self._session_vars.add(arg.arg)

        # Always include common defaults
        self._session_vars.update({"db", "session", "db_session"})

    def _get_annotation_name(self, annotation: ast.AST) -> str:
        """Extract annotation name as string."""
        if isinstance(annotation, ast.Name):
            return annotation.id
        if isinstance(annotation, ast.Attribute):
            return annotation.attr
        if isinstance(annotation, ast.Subscript):
            # Optional[Session] → Session
            if isinstance(annotation.value, ast.Name):
                return self._get_annotation_name(annotation.slice)
        return ""

    # ─── Pre-scan: find functions that contain queries ───

    def _prescan_query_functions(self, tree: ast.AST) -> None:
        """
        First pass: identify which functions in this file contain DB queries.
        Used for inter-procedural analysis.
        """
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and self._is_query_call(child):
                    self._local_query_funcs.add(node.name)
                    break

    # ─── Function tracking ───

    def _func_body_has_eager_load(self, node: ast.AST) -> bool:
        """Check if a function body contains any eager loading calls."""
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if self._is_eager_load_call(child):
                    return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(node.name)
        old_eager = self._func_has_eager_load
        self._func_has_eager_load = self._func_body_has_eager_load(node)
        self.generic_visit(node)
        self._func_has_eager_load = old_eager
        self._func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        old_eager = self._func_has_eager_load
        self._func_has_eager_load = self._func_body_has_eager_load(node)
        self.generic_visit(node)
        self._func_has_eager_load = old_eager
        self._func_stack.pop()

    # ─── Loop tracking ───

    def visit_For(self, node: ast.For) -> None:
        loop_var = None
        if isinstance(node.target, ast.Name):
            loop_var = node.target.id

        has_eager = self._check_eager_load_context(node)

        ctx = _LoopContext(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            depth=self._loop_depth + 1,
            has_eager_load=has_eager,
            loop_var=loop_var,
        )
        self._loop_stack.append(ctx)
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_While(self, node: ast.While) -> None:
        ctx = _LoopContext(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            depth=self._loop_depth + 1,
        )
        self._loop_stack.append(ctx)
        self.generic_visit(node)
        self._loop_stack.pop()

    # ─── Query detection inside loops ───

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_loop:
            loop_ctx = self._loop_stack[-1]

            # Only detect queries in loop BODY, not the iterable
            if node.lineno > loop_ctx.start_line:

                # Pattern 1: Direct query call (db.query, session.execute, etc.)
                if self._is_query_call(node):
                    method_name = self._get_call_method(node)
                    caller_name = self._get_caller_name(node)
                    context = self._get_line_context(node.lineno)

                    self._add_warning(
                        file=self.filepath,
                        line=node.lineno,
                        function=self._current_func,
                        pattern=f"{caller_name}.{method_name}() inside loop",
                        severity="high",
                        suggestion=(
                            "Move query outside loop, use IN filter with list of IDs, "
                            "or add .options(selectinload(...)) to parent query"
                        ),
                        loop_depth=self._loop_depth,
                        context=context,
                    )

                # Pattern 2: Inter-procedural — function call that contains queries
                elif self._is_query_function_call(node):
                    func_name = self._get_called_func_name(node)
                    context = self._get_line_context(node.lineno)

                    self._add_warning(
                        file=self.filepath,
                        line=node.lineno,
                        function=self._current_func,
                        pattern=f"Function call {func_name}() in loop (contains DB queries)",
                        severity="medium",
                        suggestion=(
                            f"Function '{func_name}' contains database queries. "
                            "Calling it inside a loop causes N+1. "
                            "Consider refactoring to bulk query or passing pre-fetched data."
                        ),
                        loop_depth=self._loop_depth,
                        context=context,
                    )

            # Pattern 3: Pydantic serializer N+1 (in or out of loop)
            self._check_serializer_n1(node)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Detect relationship access on loop variables."""
        if not self._in_loop:
            self.generic_visit(node)
            return

        loop_ctx = self._loop_stack[-1]
        if loop_ctx.loop_var is None:
            self.generic_visit(node)
            return

        if (isinstance(node.value, ast.Name)
                and node.value.id == loop_ctx.loop_var
                and isinstance(node.ctx, ast.Load)):

            attr = node.attr
            is_relationship = self._is_likely_relationship(attr)

            if is_relationship and not loop_ctx.has_eager_load and not self._func_has_eager_load:
                if not self._is_suppressed(node.lineno):
                    context = self._get_line_context(node.lineno)
                    self._add_warning(
                        file=self.filepath,
                        line=node.lineno,
                        function=self._current_func,
                        pattern=f"Lazy relationship access: {loop_ctx.loop_var}.{attr}",
                        severity="high",
                        suggestion=f"Add .options(joinedload(Model.{attr})) or selectinload() to query",
                        loop_depth=self._loop_depth,
                        context=context,
                    )

        self.generic_visit(node)

    # ─── Comprehension detection ───

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._check_comp_for_queries(node)
        self._check_comp_for_serializer(node)
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._check_comp_for_queries(node)
        self._check_comp_for_serializer(node)
        self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._check_comp_for_queries(node)
        self._check_comp_for_serializer(node)
        self.generic_visit(node)

    def _check_comp_for_queries(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and self._is_query_call(child):
                method_name = self._get_call_method(child)
                context = self._get_line_context(child.lineno)
                self._add_warning(
                    file=self.filepath,
                    line=child.lineno,
                    function=self._current_func,
                    pattern=f"{method_name}() inside comprehension",
                    severity="high",
                    suggestion="Use bulk query with IN filter instead of per-item query",
                    context=context,
                )

    def _check_comp_for_serializer(self, comp_node: ast.AST) -> None:
        """Detect [Schema.from_orm(x) for x in items] without eager loading."""
        for child in ast.walk(comp_node):
            if isinstance(child, ast.Call) and self._is_serializer_call(child):
                context = self._get_line_context(child.lineno)
                self._add_warning(
                    file=self.filepath,
                    line=child.lineno,
                    function=self._current_func,
                    pattern="Pydantic serializer in comprehension (potential lazy-load N+1)",
                    severity="medium",
                    suggestion=(
                        "If the schema accesses relationships, ensure the source "
                        "query uses joinedload/selectinload for those relationships"
                    ),
                    context=context,
                )

    # ─── Pydantic / serializer N+1 ───

    def _check_serializer_n1(self, node: ast.Call) -> None:
        """Detect from_orm/model_validate calls in loops."""
        if self._in_loop and self._is_serializer_call(node):
            context = self._get_line_context(node.lineno)
            self._add_warning(
                file=self.filepath,
                line=node.lineno,
                function=self._current_func,
                pattern="Pydantic serializer in loop (potential lazy-load N+1)",
                severity="medium",
                suggestion=(
                    "Serializing ORM objects in a loop may trigger lazy relationship loading. "
                    "Ensure source query has eager loading for all schema fields."
                ),
                loop_depth=self._loop_depth,
                context=context,
            )

    def _is_serializer_call(self, node: ast.Call) -> bool:
        """Check if call is a Pydantic/serializer method."""
        if isinstance(node.func, ast.Attribute):
            return node.func.attr in SERIALIZER_METHODS
        return False

    # ─── Helper methods ───

    def _is_query_call(self, node: ast.Call) -> bool:
        """
        Check if a Call node is a database query call.
        Uses dynamically detected session variable names.
        Filters out dict.get() false positives.
        """
        if not isinstance(node.func, ast.Attribute):
            return False

        method = node.func.attr

        # Skip ambiguous methods (like "get") unless caller is known Session
        if method in AMBIGUOUS_METHODS:
            caller = self._get_caller_name(node)
            return caller in self._session_vars

        if method not in QUERY_METHODS:
            return False

        # For query/execute/scalars — check if caller is a session variable
        caller = self._get_caller_name(node)

        # Direct match: db.query, session.execute, etc.
        if caller in self._session_vars:
            return True

        # Attribute chain: self.db.query, self.session.execute
        if isinstance(node.func.value, ast.Attribute):
            if isinstance(node.func.value.value, ast.Name):
                inner_attr = node.func.value.attr
                if inner_attr in self._session_vars:
                    return True

        # "query" is almost always a DB query method
        if method == "query":
            return True

        return False

    def _is_query_function_call(self, node: ast.Call) -> bool:
        """
        Check if a function call is to a function known to contain DB queries.
        Inter-procedural analysis (within same file).
        """
        func_name = self._get_called_func_name(node)
        if not func_name:
            return False

        # Check local functions
        if func_name in self._local_query_funcs:
            return True

        # Check cross-file functions (if provided)
        if func_name in self._functions_with_queries:
            return True

        return False

    def _get_called_func_name(self, node: ast.Call) -> str:
        """Get the name of a called function."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            # self.some_method() → some_method
            return node.func.attr
        return ""

    def _get_call_method(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
        return "unknown"

    def _get_caller_name(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                return node.func.value.id
            if isinstance(node.func.value, ast.Attribute):
                if isinstance(node.func.value.value, ast.Name):
                    return f"{node.func.value.value.id}.{node.func.value.attr}"
        return "?"

    def _is_likely_relationship(self, attr_name: str) -> bool:
        """
        Determine if an attribute is likely a SQLAlchemy relationship.

        Strategy (domain-agnostic, zero business-specific assumptions):
          1. Model introspection (ground truth) — columns & relationships from models file
          2. User-provided hints — custom relationship names via --hints or --config
          3. Conservative fallback — unknown attributes are not guessed

        Relationship names cannot be inferred reliably from plural spelling alone;
        ordinary list and dataclass attributes look identical in static Python code.
        Use --models for accurate detection or --hints for custom relationships.
        """

        # ── Tier 0: PascalCase names are model classes, not relationships ──
        # When code does: result = db.query(Model1, Model2.col).first()
        #   result.Model1.id  ← this is tuple attribute access, NOT lazy loading
        # Model class names start with uppercase and follow PascalCase convention
        if attr_name and attr_name[0].isupper():
            return False

        # ── Tier 1: Model introspection (ground truth) ──
        # Check columns FIRST — if a name is both a column (on some model) and
        # a relationship (on another model), column access is far more common
        # and treating it as a relationship would produce false positives.
        if self._known_columns and attr_name in self._known_columns:
            return False
        if self._known_relationships and attr_name in self._known_relationships:
            return True

        # ── Tier 2: User-provided hints ──
        if attr_name in self._custom_hints:
            return True

        # No model metadata or explicit hint: do not invent a relationship.
        return False

    def _check_eager_load_context(self, loop_node: ast.For) -> bool:
        """Check if eager loading is present in the loop's iterable."""
        for child in ast.walk(loop_node.iter):
            if isinstance(child, ast.Call):
                if self._is_eager_load_call(child):
                    return True
        return False

    def _is_eager_load_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in EAGER_LOAD_FUNCS
        if isinstance(node.func, ast.Attribute):
            return node.func.attr == "options"
        return False

    def _get_line_context(self, lineno: int) -> str:
        if 0 < lineno <= len(self.lines):
            return self.lines[lineno - 1].strip()
        return ""


# ─── Model introspection ───

def _extract_from_models(models_file: Path) -> tuple[set[str], set[str]]:
    """
    Extract column AND relationship attribute names from SQLAlchemy models file.

    Supports BOTH assignment styles:
      - Old: name = Column(String(100))                           (ast.Assign)
      - New: name: Mapped[str] = mapped_column(String(100))       (ast.AnnAssign)

    Returns:
        (columns, relationships) — two sets of attribute names
    """
    columns: set[str] = set()
    relationships: set[str] = set()

    if not models_file.is_file():
        return columns, relationships

    try:
        source = models_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return columns, relationships

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            name, value = _extract_assignment(item)
            if name is None or value is None:
                continue
            if name.startswith("_"):
                continue
            if not isinstance(value, ast.Call):
                continue

            func_name = _get_func_name(value)
            if func_name == "relationship":
                relationships.add(name)
            elif func_name in ("Column", "mapped_column"):
                columns.add(name)

    return columns, relationships


def _extract_assignment(node: ast.AST) -> tuple[str | None, ast.AST | None]:
    """
    Extract (name, value) from both assignment styles:
      ast.Assign:    name = Column(...)
      ast.AnnAssign: name: Mapped[str] = mapped_column(...)
    """
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.value is not None:
            return node.target.id, node.value
    elif isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id, node.value
    return None, None


def _get_func_name(node: ast.Call) -> str:
    """Get the function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def extract_relationships_from_models(models_file: Path) -> set[str]:
    """Extract relationship attribute names from SQLAlchemy models file."""
    _, relationships = _extract_from_models(models_file)
    return relationships


def extract_columns_from_models(models_file: Path) -> set[str]:
    """Extract column attribute names from SQLAlchemy models file."""
    columns, _ = _extract_from_models(models_file)
    return columns


# ─── Public API ───

def scan_file_for_n1(
    filepath: Path,
    known_relationships: set[str] | None = None,
    known_columns: set[str] | None = None,
    functions_with_queries: set[str] | None = None,
    custom_hints: set[str] | None = None,
) -> list[N1Warning]:
    """Scan a Python file for N+1 query patterns. O(N) single-pass."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    visitor = _N1Visitor(
        str(filepath), lines, known_relationships, known_columns,
        functions_with_queries, custom_hints,
    )

    # Pre-scan passes (still O(N) total)
    visitor._prescan_session_vars(tree)
    visitor._prescan_query_functions(tree)

    # Main analysis pass
    visitor.visit(tree)

    return visitor.warnings


def scan_directory(
    directory: Path,
    models_file: Path | None = None,
    custom_hints: set[str] | None = None,
) -> dict:
    """Scan all Python files in directory for N+1 patterns."""

    known_rels: set[str] | None = None
    known_cols: set[str] | None = None
    if models_file and models_file.is_file():
        known_rels = extract_relationships_from_models(models_file)
        known_cols = extract_columns_from_models(models_file)

    # Phase 1: Identify all functions with queries (for inter-proc)
    all_query_funcs: set[str] = set()
    files = sorted(f for f in directory.rglob("*.py") if "__pycache__" not in str(f))

    for filepath in files:
        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Attribute):
                            if child.func.attr in ("query", "execute", "scalars"):
                                all_query_funcs.add(node.name)
                                break

    # Phase 2: Full scan with inter-procedural context
    all_warnings: list[N1Warning] = []
    for filepath in files:
        file_warnings = scan_file_for_n1(filepath, known_rels, known_cols, all_query_funcs, custom_hints)
        all_warnings.extend(file_warnings)

    # Deduplicate
    seen = set()
    unique_warnings = []
    for w in all_warnings:
        key = (w.file, w.line, w.pattern)
        if key not in seen:
            seen.add(key)
            unique_warnings.append(w)

    high = sum(1 for w in unique_warnings if w.severity == "high")
    medium = sum(1 for w in unique_warnings if w.severity == "medium")
    low = sum(1 for w in unique_warnings if w.severity == "low")

    return {
        "total_warnings": len(unique_warnings),
        "high": high,
        "medium": medium,
        "low": low,
        "known_relationships": len(known_rels) if known_rels else 0,
        "known_columns": len(known_cols) if known_cols else 0,
        "query_functions_detected": len(all_query_funcs),
        "files_scanned": len(files),
        "warnings": [
            {
                "file": w.file,
                "line": w.line,
                "function": w.function,
                "pattern": w.pattern,
                "severity": w.severity,
                "suggestion": w.suggestion,
                "loop_depth": w.loop_depth,
                "context": w.context,
            }
            for w in unique_warnings
        ],
    }


def _load_config(config_path: Path) -> dict:
    """
    Load configuration from JSON file.

    Example .n1rc.json:
    {
        "relationship_hints": ["player", "enemy", "inventory", "weapon"],
        "column_hints": ["xp_points", "health_bar"],
        "exclude_files": ["**/migrations/**"]
    }
    """
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    """CLI entrypoint for N+1 Query Detector."""
    import argparse

    parser = argparse.ArgumentParser(
        description="N+1 Query Detector — scan ORM code for query-in-loop patterns"
    )
    parser.add_argument(
        "target", nargs="?", default="output",
        help="Directory or file to scan (default: output/)",
    )
    parser.add_argument(
        "--models", default=None,
        help="Path to SQLAlchemy models file for relationship introspection",
    )
    parser.add_argument(
        "--hints", nargs="*", default=None,
        help="Custom relationship names to detect (e.g. --hints player enemy inventory)",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config JSON file (e.g. .n1rc.json) with relationship_hints, column_hints",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: exit 1 if any high-severity warnings found",
    )
    parser.add_argument(
        "--json", default=None,
        help="Save results to JSON file",
    )
    args = parser.parse_args()

    target = Path(args.target)
    models = Path(args.models) if args.models else None

    # Build custom hints from --hints CLI and --config file
    custom_hints: set[str] = set()
    if args.hints:
        custom_hints.update(args.hints)
    if args.config:
        cfg = _load_config(Path(args.config))
        custom_hints.update(cfg.get("relationship_hints", []))

    if target.is_file():
        rels = extract_relationships_from_models(models) if models else None
        cols = extract_columns_from_models(models) if models else None
        warnings = scan_file_for_n1(target, rels, cols, custom_hints=custom_hints or None)
        result: dict[str, Any] = {
            "total_warnings": len(warnings),
            "high": sum(1 for w in warnings if w.severity == "high"),
            "medium": sum(1 for w in warnings if w.severity == "medium"),
            "low": sum(1 for w in warnings if w.severity == "low"),
            "warnings": [
                {"file": w.file, "line": w.line, "function": w.function,
                 "pattern": w.pattern, "severity": w.severity,
                 "suggestion": w.suggestion, "context": w.context}
                for w in warnings
            ],
        }
    else:
        result = scan_directory(target, models, custom_hints=custom_hints or None)

    # Output
    print(f"\n{'='*60}")
    print("  N+1 QUERY DETECTOR")
    print(f"  Warnings: {result['total_warnings']}  "
          f"(HIGH: {result['high']}  MEDIUM: {result.get('medium', 0)}  "
          f"LOW: {result.get('low', 0)})")
    if result.get("known_relationships"):
        print(f"  Model relationships loaded: {result['known_relationships']}")
    if result.get("known_columns"):
        print(f"  Model columns loaded: {result['known_columns']}")
    if custom_hints:
        print(f"  Custom hints: {sorted(custom_hints)}")
    if result.get("query_functions_detected"):
        print(f"  Functions with DB queries: {result['query_functions_detected']}")
    print(f"{'='*60}")

    for w in result["warnings"]:
        icon = {"high": "[FAIL]", "medium": "[WARN]", "low": "[PASS]"}.get(w["severity"], "[INFO]")
        print(f"  {icon} {w['file']}:{w['line']}")
        print(f"     {w['function']}() — {w['pattern']}")
        if w.get("context"):
            print(f"     [NOTE] {w['context'][:120]}")
        print(f"     [TIP] {w['suggestion']}")
        print()

    # Save JSON
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  [SAVED] Saved to {args.json}")

    # CI exit code
    if args.ci and result["high"] > 0:
        print(f"\n[FAIL] CI FAILED: {result['high']} high-severity N+1 warnings")
        sys.exit(1)
    elif args.ci:
        print("\n[PASS] CI PASSED: no high-severity N+1 warnings")
        sys.exit(0)


if __name__ == "__main__":
    main()
