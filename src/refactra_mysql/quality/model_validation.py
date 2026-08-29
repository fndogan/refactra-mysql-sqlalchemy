"""
Model Reference Validator — Checks that ORM model/column references in
converted code actually exist in the SQLAlchemy models file.

After AI conversion, code uses patterns like:
    db.query(Customer).filter(Customer.email == ...)

If `Customer` doesn't exist as a model, or `email` isn't a column on it,
this will cause AttributeError at runtime.

Checks performed:
  1. Model class existence — referenced model names exist in models file
  2. Column existence — Model.column references map to real columns
  3. Relationship existence — Model.relationship references exist
  4. Model instantiation — Model(...) constructor uses valid column names
  5. Import consistency — 'from models import X' matches real model names

Uses Python AST for both models file parsing and output code analysis.

Usage:
    refactra-mysql models
    refactra-mysql models --json
    refactra-mysql models --file output/admin/customers.py
"""
import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from refactra_mysql.config import MODELS_FILE, OUTPUT_DIR, REPORTS_DIR, setup_logging

logger = setup_logging("model_validation")

_REPORTS_DIR = REPORTS_DIR / "quality"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class ModelInfo:
    """Information about a SQLAlchemy model class."""

    name: str
    line: int
    columns: Set[str] = field(default_factory=set)
    relationships: Set[str] = field(default_factory=set)
    table_name: str = ""
    all_attributes: Set[str] = field(default_factory=set)  # columns + rels


@dataclass
class ModelIssue:
    """A single model reference issue."""

    category: str  # "missing_model", "missing_column", "bad_import"
    severity: str  # "critical", "warning", "info"
    line: int
    message: str
    detail: str = ""


@dataclass
class FileModelResult:
    """Model validation results for one file."""

    output_file: str
    issues: List[ModelIssue] = field(default_factory=list)
    models_referenced: int = 0
    columns_referenced: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def is_clean(self) -> bool:
        return self.critical_count == 0


# ============================================================================
# Phase 1: Parse Models File
# ============================================================================


def parse_models_file(models_path: Path) -> Dict[str, ModelInfo]:
    """
    Parse the SQLAlchemy models file and extract all model classes,
    their columns, and relationships.

    Returns:
        Dict mapping model_class_name → ModelInfo
    """
    try:
        src = models_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("Cannot read models file: %s", e)
        return {}

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        logger.error("Models file has syntax error: %s", e)
        return {}

    models: Dict[str, ModelInfo] = {}

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Skip non-model classes (Base, mixins)
        if node.name in ("Base",):
            continue

        model = ModelInfo(name=node.name, line=node.lineno)

        for child in ast.iter_child_nodes(node):
            # Annotated assignments: column: Mapped[type] = mapped_column(...)
            if isinstance(child, ast.AnnAssign) and isinstance(
                child.target, ast.Name
            ):
                attr_name = child.target.id
                # Skip dunder and table_args
                if attr_name.startswith("__"):
                    if attr_name == "__tablename__" and child.value:
                        if isinstance(child.value, ast.Constant):
                            if isinstance(child.value.value, str):
                                model.table_name = child.value.value
                    continue
                model.columns.add(attr_name)

            # Regular assignments: attr = relationship(...) or attr = Column(...)
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    attr_name = target.id
                    if attr_name.startswith("__"):
                        if attr_name == "__tablename__" and child.value:
                            if isinstance(child.value, ast.Constant):
                                if isinstance(child.value.value, str):
                                    model.table_name = child.value.value
                        continue

                    # Check if it's a relationship
                    if isinstance(child.value, ast.Call):
                        func = child.value.func
                        func_name = ""
                        if isinstance(func, ast.Name):
                            func_name = func.id
                        elif isinstance(func, ast.Attribute):
                            func_name = func.attr

                        if func_name == "relationship":
                            model.relationships.add(attr_name)
                        else:
                            model.columns.add(attr_name)
                    else:
                        model.columns.add(attr_name)

        model.all_attributes = model.columns | model.relationships
        models[node.name] = model

    # Also handle Table() objects (views, many-to-many tables)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(
                    node.value, ast.Call
                ):
                    func = node.value.func
                    if isinstance(func, ast.Name) and func.id == "Table":
                        # This is a Table object, extract column names
                        table_model = ModelInfo(
                            name=target.id, line=node.lineno
                        )
                        for arg in node.value.args:
                            if isinstance(arg, ast.Call):
                                f = arg.func
                                if isinstance(f, ast.Name) and f.id == "Column":
                                    # First arg is column name
                                    if arg.args and isinstance(
                                        arg.args[0], ast.Constant
                                    ):
                                        column_name = arg.args[0].value
                                        if isinstance(column_name, str):
                                            table_model.columns.add(column_name)
                        table_model.all_attributes = table_model.columns
                        models[target.id] = table_model

    return models


# ============================================================================
# Phase 2: Scan Output Files for Model References
# ============================================================================


@dataclass
class ModelReference:
    """A reference to a model or model attribute in output code."""

    model_name: str
    attribute: Optional[str]  # None if just model name, else column/rel name
    line: int
    context: str  # "query", "filter", "join", "instantiation", "attribute"
    source_line: str  # The actual code line


def extract_model_references(
    code: str, filepath: str = ""
) -> List[ModelReference]:
    """
    Extract all model references from output code using AST + regex hybrid.

    Detects:
    - db.query(ModelName)
    - db.query(ModelName.column)
    - .filter(ModelName.column == ...)
    - .filter_by(column=...)  (can't verify model without context)
    - .join(ModelName, ...)
    - .outerjoin(ModelName, ...)
    - ModelName(column=value, ...)  (instantiation)
    - from models import ModelName
    """
    refs: List[ModelReference] = []
    lines = code.splitlines()

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return refs

    # Known non-model uppercase names to skip
    skip_names = {
        "Session", "Query", "True", "False", "None", "Optional",
        "List", "Dict", "Any", "Tuple", "Set", "Union", "Type",
        "Request", "Response", "JSONResponse", "HTMLResponse",
        "RedirectResponse", "HTTPException", "Form", "File",
        "UploadFile", "Depends", "Column", "Integer", "String",
        "Text", "Boolean", "DateTime", "Date", "Float", "Numeric",
        "ForeignKey", "Table", "Index", "Base", "Mapped",
        "BigInteger", "SmallInteger", "LargeBinary", "JSONB",
        "PrimaryKeyConstraint", "ForeignKeyConstraint",
        "DeclarativeBase", "Path", "Counter", "OrderedDict",
        "Exception", "ValueError", "TypeError", "KeyError",
        "AttributeError", "OSError", "IOError", "RuntimeError",
        "NotImplementedError", "StopIteration", "FileNotFoundError",
        "PermissionError",
        # Non-model service/utility classes
        "ZoomService", "Jinja2Templates", "IntervalTrigger",
        "CronTrigger", "APIRouter", "WebsiteConfigService",
        "ThemeFileResolver", "ThemeResolver", "ThemeResolverError",
        "DraftService", "WebsiteUtils", "DomainVerifier",
        # FastAPI / Starlette types
        "PathParam", "FileResponse",
        # Pydantic response/request models (not DB models)
        "DocumentUploadResponse", "EmployeeDeleteResponse",
        "AvatarUploadResponse", "NextEmployeeIdResponse",
        "SuccessResponse", "EmployeeVerifyResponse",
        "DemoRequests",
        # Python stdlib / third-party classes
        "Environment", "Credentials", "MIMEApplication",
        "MIMEMultipart", "MIMEText", "MIMEBase",
    }

    def _is_all_caps(name: str) -> bool:
        """Check if name is ALL_CAPS (constant, not model)."""
        return name == name.upper() and '_' in name

    def _walk_calls(node):
        """Walk AST and find model-related calls."""
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            func = child.func

            # Pattern: db.query(ModelName) or db.query(ModelName.col)
            if isinstance(func, ast.Attribute) and func.attr == "query":
                for arg in child.args:
                    if isinstance(arg, ast.Name) and arg.id[0].isupper():
                        if arg.id not in skip_names and not _is_all_caps(arg.id):
                            refs.append(
                                ModelReference(
                                    model_name=arg.id,
                                    attribute=None,
                                    line=arg.lineno,
                                    context="query",
                                    source_line=lines[arg.lineno - 1].strip()
                                    if arg.lineno <= len(lines)
                                    else "",
                                )
                            )
                    elif isinstance(arg, ast.Attribute) and isinstance(
                        arg.value, ast.Name
                    ):
                        if (
                            arg.value.id[0].isupper()
                            and arg.value.id not in skip_names
                            and not _is_all_caps(arg.value.id)
                        ):
                            refs.append(
                                ModelReference(
                                    model_name=arg.value.id,
                                    attribute=arg.attr,
                                    line=arg.lineno,
                                    context="query",
                                    source_line=lines[arg.lineno - 1].strip()
                                    if arg.lineno <= len(lines)
                                    else "",
                                )
                            )

            # Pattern: .join(ModelName, ...) or .outerjoin(ModelName, ...)
            if isinstance(func, ast.Attribute) and func.attr in (
                "join",
                "outerjoin",
            ):
                if child.args:
                    first_arg = child.args[0]
                    if (
                        isinstance(first_arg, ast.Name)
                        and first_arg.id[0].isupper()
                        and first_arg.id not in skip_names
                        and not _is_all_caps(first_arg.id)
                    ):
                        refs.append(
                            ModelReference(
                                model_name=first_arg.id,
                                attribute=None,
                                line=first_arg.lineno,
                                context="join",
                                source_line=lines[first_arg.lineno - 1].strip()
                                if first_arg.lineno <= len(lines)
                                else "",
                            )
                        )

    _walk_calls(tree)

    # Pattern: ModelName.column_name in comparisons (filter conditions)
    # Use regex for these since they're in various AST contexts
    attr_pattern = re.compile(r"\b([A-Z]\w+)\.(\w+)\b")
    for i, line in enumerate(lines, 1):
        for m in attr_pattern.finditer(line):
            model_name = m.group(1)
            attr_name = m.group(2)
            if model_name in skip_names or _is_all_caps(model_name):
                continue
            # Skip common false positives — SQLAlchemy internals and Python methods
            if attr_name.startswith("__"):  # __table__, __tablename__, etc.
                continue
            if attr_name in (
                "query", "session", "metadata",
                "add", "delete", "commit", "flush", "rollback", "close",
                "merge", "execute", "scalar", "first", "all", "one",
                "one_or_none", "count", "exists",
                "filter", "filter_by", "join", "outerjoin",
                "order_by", "group_by", "having", "limit", "offset",
                "distinct", "subquery", "with_entities", "options",
                "label", "desc", "asc",
                "ilike", "like", "in_", "notin_", "is_", "isnot",
                "between", "contains", "startswith", "endswith",
                "strip", "lower", "upper", "replace", "format", "split",
                "append", "extend", "get", "items", "keys", "values",
                "update", "pop",
                "error", "info", "warning", "debug", "exception", "critical",
                "TemplateResponse", "route", "Response",
                "get_folder_path", "get_theme",
                # Class methods / common ORM methods
                "create", "save", "load", "init", "from_dict", "to_dict",
                "validate", "serialize", "deserialize",
            ):
                continue
            # Only if this looks like Model.column context not method call
            refs.append(
                ModelReference(
                    model_name=model_name,
                    attribute=attr_name,
                    line=i,
                    context="attribute",
                    source_line=line.strip(),
                )
            )

    # Pattern: Model instantiation — ModelName(col=val, col2=val2)
    for child in ast.walk(tree):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            name = child.func.id
            if name[0].isupper() and name not in skip_names and not _is_all_caps(name):
                # Check if it looks like a model instantiation
                if child.keywords:
                    kw_names = [
                        k.arg for k in child.keywords if k.arg is not None
                    ]
                    if kw_names:
                        for kw_name in kw_names:
                            refs.append(
                                ModelReference(
                                    model_name=name,
                                    attribute=kw_name,
                                    line=child.lineno,
                                    context="instantiation",
                                    source_line=lines[
                                        child.lineno - 1
                                    ].strip()
                                    if child.lineno <= len(lines)
                                    else "",
                                )
                            )

    return refs


# ============================================================================
# Phase 3: Validate References
# ============================================================================


def validate_file(
    output_path: Path,
    output_dir: Path,
    models: Dict[str, ModelInfo],
) -> FileModelResult:
    """Validate all model references in a single output file."""
    rel_path = str(output_path.relative_to(output_dir))
    result = FileModelResult(output_file=rel_path)

    try:
        code = output_path.read_text(encoding="utf-8")
    except OSError:
        return result

    refs = extract_model_references(code, rel_path)

    # Detect aliased() patterns: X = aliased(ModelName)
    # These create valid model aliases that should be recognized
    # Use a local copy to avoid mutating the shared models dict
    local_models = dict(models)
    alias_pattern = re.compile(r'(\w+)\s*=\s*aliased\((\w+)\)')
    for m in alias_pattern.finditer(code):
        alias_name = m.group(1)
        source_model = m.group(2)
        if source_model in local_models and alias_name not in local_models:
            # Create a temporary ModelInfo entry for the alias
            source = local_models[source_model]
            local_models[alias_name] = ModelInfo(
                name=alias_name,
                line=0,
                columns=source.columns.copy(),
                relationships=source.relationships.copy(),
                table_name=source.table_name,
                all_attributes=source.all_attributes.copy(),
            )

    # Deduplicate by (model, attribute, line)
    seen = set()
    unique_refs = []
    for ref in refs:
        key = (ref.model_name, ref.attribute, ref.line)
        if key not in seen:
            seen.add(key)
            unique_refs.append(ref)

    # Track unique models and columns referenced
    models_seen = set()
    columns_seen = set()

    for ref in unique_refs:
        models_seen.add(ref.model_name)
        if ref.attribute:
            columns_seen.add((ref.model_name, ref.attribute))

        # Check 1: Does the model exist?
        if ref.model_name not in local_models:
            # Could be a non-model class — only flag if it's in query/join/filter
            if ref.context in ("query", "join"):
                result.issues.append(
                    ModelIssue(
                        category="missing_model",
                        severity="critical",
                        line=ref.line,
                        message=(
                            f"Model '{ref.model_name}' used in {ref.context} "
                            f"but NOT found in models file"
                        ),
                        detail=ref.source_line[:120],
                    )
                )
            elif ref.context == "instantiation":
                result.issues.append(
                    ModelIssue(
                        category="missing_model",
                        severity="warning",
                        line=ref.line,
                        message=(
                            f"'{ref.model_name}' instantiated but NOT found "
                            f"in models file (may be non-model class)"
                        ),
                        detail=ref.source_line[:120],
                    )
                )
            continue

        # Check 2: Does the column/attribute exist on this model?
        if ref.attribute and ref.model_name in local_models:
            model = local_models[ref.model_name]
            if ref.attribute not in model.all_attributes:
                if ref.context in ("query", "attribute", "instantiation"):
                    result.issues.append(
                        ModelIssue(
                            category="missing_column",
                            severity="critical",
                            line=ref.line,
                            message=(
                                f"'{ref.model_name}.{ref.attribute}' — "
                                f"column/relationship NOT found on model"
                            ),
                            detail=f"Available: {sorted(model.all_attributes)[:15]}... | Code: {ref.source_line[:80]}",
                        )
                    )

    result.models_referenced = len(models_seen)
    result.columns_referenced = len(columns_seen)

    # Check 3: Model imports
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return result

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "models" in node.module:
                for alias in node.names:
                    name = alias.name
                    if name not in models and name != "*":
                        result.issues.append(
                            ModelIssue(
                                category="bad_import",
                                severity="warning",
                                line=node.lineno,
                                message=(
                                    f"Import 'from {node.module} import {name}' "
                                    f"— '{name}' not found in models file"
                                ),
                                detail="May be in a different models module",
                            )
                        )

    return result


# ============================================================================
# Report
# ============================================================================


def print_report(
    results: List[FileModelResult],
    models: Dict[str, ModelInfo],
    detailed: bool = True,
) -> bool:
    """Print human-readable report. Returns True if no criticals."""
    total_critical = sum(r.critical_count for r in results)
    total_issues = sum(len(r.issues) for r in results)
    clean_files = sum(1 for r in results if r.is_clean)
    print()
    print("=" * 80)
    print("  MODEL REFERENCE VALIDATION REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    print(f"  Models in models file:  {len(models)}")
    print(
        f"  Columns in models file: "
        f"{sum(len(m.all_attributes) for m in models.values())}"
    )
    print()

    for r in results:
        if not r.issues and r.models_referenced == 0:
            continue  # Skip files with no model usage

        if r.is_clean:
            if r.models_referenced > 0:
                print(
                    f"  [PASS] {r.output_file}: {r.models_referenced} models, "
                    f"{r.columns_referenced} columns — clean"
                )
        else:
            print(
                f"  [FAIL] {r.output_file}: {r.critical_count} critical, "
                f"{len(r.issues)} total"
            )

    # Critical issues detail
    files_with_criticals = [r for r in results if r.critical_count > 0]
    if files_with_criticals and detailed:
        print()
        print("-" * 80)
        print("  [FAIL] CRITICAL: Invalid Model/Column References")
        print("-" * 80)

        for r in files_with_criticals:
            for issue in r.issues:
                if issue.severity != "critical":
                    continue
                print(f"\n  [FILE] {r.output_file}")
                print(f"     [FAIL] L{issue.line} [{issue.category}]")
                print(f"        {issue.message}")
                if issue.detail:
                    print(f"        → {issue.detail}")

    # Warnings
    warning_files = [
        r for r in results if any(i.severity == "warning" for i in r.issues)
    ]
    if warning_files and detailed:
        print()
        print("-" * 80)
        warn_count = sum(
            sum(1 for i in r.issues if i.severity == "warning")
            for r in results
        )
        print(f"  [WARN] WARNINGS ({warn_count} total)")
        print("-" * 80)
        for r in warning_files:
            warnings = [i for i in r.issues if i.severity == "warning"]
            if warnings:
                print(f"\n  [FILE] {r.output_file}")
                for issue in warnings:
                    print(
                        f"     [WARN] L{issue.line} [{issue.category}] {issue.message}"
                    )

    # Category summary
    print()
    print("-" * 80)
    print("  CHECK RESULTS BY CATEGORY")
    print("-" * 80)

    cats: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        for i in r.issues:
            cats[i.category][i.severity] += 1

    for cat, label in [
        ("missing_model", "Missing Model Classes"),
        ("missing_column", "Missing Columns/Attributes"),
        ("bad_import", "Invalid Model Imports"),
    ]:
        c = cats.get(cat, Counter())
        crit = c.get("critical", 0)
        warn = c.get("warning", 0)
        total = crit + warn
        if total == 0:
            print(f"  [PASS] {label}: 0 issues")
        else:
            parts = []
            if crit:
                parts.append(f"[FAIL] {crit} critical")
            if warn:
                parts.append(f"[WARN] {warn} warning")
            print(f"  {'[FAIL]' if crit else '[WARN] '} {label}: {', '.join(parts)}")

    # Summary
    print()
    print("=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"  Files scanned:          {len(results)}")
    active = sum(1 for r in results if r.models_referenced > 0)
    print(f"  Files using models:     {active}")
    print(f"  Clean files:            {clean_files}")
    print(f"  Critical issues:        {total_critical}")
    print(f"  Total issues:           {total_issues}")
    print()

    if total_critical == 0:
        print("  [PASS] ALL MODEL REFERENCES ARE VALID!")
    else:
        print(
            f"  [WARN]  {total_critical} INVALID MODEL REFERENCES — "
            f"will cause AttributeError at runtime!"
        )

    print("=" * 80)
    print()

    return total_critical == 0


# ============================================================================
# JSON Report
# ============================================================================


def save_json_report(
    results: List[FileModelResult],
    models: Dict[str, ModelInfo],
    output_path: Path,
) -> Path:
    """Save machine-readable JSON report."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "models_in_file": len(models),
        "total_columns": sum(len(m.all_attributes) for m in models.values()),
        "total_files": len(results),
        "files_using_models": sum(
            1 for r in results if r.models_referenced > 0
        ),
        "total_critical": sum(r.critical_count for r in results),
        "total_issues": sum(len(r.issues) for r in results),
        "all_valid": all(r.is_clean for r in results),
        "models": {
            name: {
                "line": m.line,
                "table_name": m.table_name,
                "columns": sorted(m.columns),
                "relationships": sorted(m.relationships),
            }
            for name, m in models.items()
        },
        "files": [
            {
                "file": r.output_file,
                "models_referenced": r.models_referenced,
                "columns_referenced": r.columns_referenced,
                "critical_count": r.critical_count,
                "is_clean": r.is_clean,
                "issues": [
                    {
                        "category": i.category,
                        "severity": i.severity,
                        "line": i.line,
                        "message": i.message,
                        "detail": i.detail,
                    }
                    for i in r.issues
                ],
            }
            for r in results
            if r.models_referenced > 0 or r.issues
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    logger.info("JSON report saved: %s", output_path)
    return output_path


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Validate that ORM model and column references in converted "
            "code actually exist in the SQLAlchemy models file."
        ),
    )
    parser.add_argument(
        "--models-file",
        default=MODELS_FILE,
        help="Path to SQLAlchemy models file (default: from .env)",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Directory with converted files (default: from .env)",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        default=True,
        help="Show detailed issues",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        default=False,
        help="Show only summary",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Save JSON report",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Validate a single file",
    )
    args = parser.parse_args()

    # ── Resolve models file ──
    models_file = Path(args.models_file)
    if not models_file.is_file():
        logger.error("Models file not found: %s", models_file)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        logger.error("Output directory not found: %s", output_dir)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Model Reference Validator")
    logger.info("=" * 60)
    logger.info("Models file: %s", models_file)
    logger.info("Output dir:  %s", output_dir)

    # ── Phase 1: Parse models ──
    logger.info("Phase 1: Parsing models file...")
    models = parse_models_file(models_file)
    total_cols = sum(len(m.all_attributes) for m in models.values())
    logger.info(
        "  Found %d models with %d total columns/relationships",
        len(models),
        total_cols,
    )

    # ── Phase 2 & 3: Scan and validate output files ──
    logger.info("Phase 2: Scanning output files for model references...")

    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            logger.error("File not found: %s", file_path)
            sys.exit(1)
        output_files = [file_path]
    else:
        output_files = sorted(output_dir.rglob("*.py"))
        output_files = [
            f for f in output_files if "__pycache__" not in str(f)
        ]

    results: List[FileModelResult] = []
    for output_path in output_files:
        result = validate_file(output_path, output_dir, models)
        results.append(result)

    # ── Report ──
    detailed = args.detailed and not args.summary_only
    all_ok = print_report(results, models, detailed=detailed)

    # ── Save JSON ──
    if args.json:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = _REPORTS_DIR / f"model_validation_{timestamp}.json"
        save_json_report(results, models, json_path)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
