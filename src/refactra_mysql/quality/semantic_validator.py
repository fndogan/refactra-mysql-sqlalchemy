"""Generate before/after semantic-equivalence test scaffolds.

The generated tests load matching top-level functions from an original and a
converted tree, then compare their results. Projects must provide ``db_session``
and ``raw_connection`` pytest fixtures in their own ``conftest.py``.

Usage:
    refactra-mysql semantic \
        --original-dir ./legacy_src \
        --converted-dir ./converted_src \
        --output-file ./tests/test_semantic_equivalence.py

Always run generated tests against isolated test data, never production.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from refactra_mysql.io_utils import atomic_write_text


def extract_function_signatures(filepath: Path) -> list[dict]:
    """Extract top-level function names and parameters from a Python file."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                {
                    "name": node.name,
                    "params": [arg.arg for arg in node.args.args],
                    "lineno": node.lineno,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                }
            )
    return functions


def _build_arguments(params: list[str], database_argument: str) -> str:
    arguments = []
    for parameter in params:
        if parameter == "self":
            continue
        if parameter in {"db", "session", "db_session", "connection", "cursor", "conn"}:
            arguments.append(f"{parameter}={database_argument}")
        elif parameter == "company_id":
            arguments.append(f"{parameter}=TEST_COMPANY_ID")
        elif "id" in parameter.lower():
            arguments.append(f"{parameter}=TEST_ID")
        else:
            arguments.append(f"{parameter}=None")
    return ", ".join(arguments)


def generate_test_stub(
    func_name: str,
    relative_path: Path,
    original_params: list[str],
    converted_params: list[str],
) -> str:
    """Generate one executable pytest scaffold for a matching function pair."""
    original_call = _build_arguments(original_params, "raw_connection")
    converted_call = _build_arguments(converted_params, "db_session")
    path_literal = repr(relative_path.as_posix())
    safe_name = re.sub(r"\W+", "_", f"{relative_path}_{func_name}")

    return f'''
def test_{safe_name}_equivalence(db_session, raw_connection):
    """Compare original and converted implementations of ``{func_name}``."""
    original_module = _load_module(
        "semantic_original_{safe_name}", ORIGINAL_DIR / {path_literal}
    )
    converted_module = _load_module(
        "semantic_converted_{safe_name}", CONVERTED_DIR / {path_literal}
    )
    original_function = getattr(original_module, {func_name!r})
    converted_function = getattr(converted_module, {func_name!r})

    old_result = original_function({original_call})
    new_result = converted_function({converted_call})

    if isinstance(old_result, dict) and isinstance(new_result, dict):
        assert old_result == new_result
    elif isinstance(old_result, list) and isinstance(new_result, list):
        assert len(old_result) == len(new_result)
        assert old_result == new_result
    else:
        assert old_result == new_result
'''


def generate_test_file(
    original_dir: Path,
    converted_dir: Path,
    output_file: Path,
) -> dict:
    """Generate a pytest file comparing matching converted functions."""
    test_functions = []
    stats = {"total": 0, "generated": 0, "skipped": 0}

    for converted_file in sorted(converted_dir.rglob("*.py")):
        if "__pycache__" in converted_file.parts:
            continue

        relative_path = converted_file.relative_to(converted_dir)
        original_file = original_dir / relative_path
        if not original_file.is_file():
            continue

        original_functions = {
            function["name"]: function
            for function in extract_function_signatures(original_file)
        }
        converted_functions = {
            function["name"]: function
            for function in extract_function_signatures(converted_file)
        }

        for func_name in sorted(original_functions.keys() & converted_functions.keys()):
            stats["total"] += 1
            original = original_functions[func_name]
            converted = converted_functions[func_name]

            has_raw_database_param = any(
                parameter in original["params"]
                for parameter in ("connection", "cursor", "conn", "db")
            )
            has_orm_database_param = any(
                parameter in converted["params"]
                for parameter in ("db", "session", "db_session")
            )
            if (
                original["is_async"]
                or converted["is_async"]
                or not (has_raw_database_param and has_orm_database_param)
            ):
                stats["skipped"] += 1
                continue

            test_functions.append(
                generate_test_stub(
                    func_name=func_name,
                    relative_path=relative_path,
                    original_params=original["params"],
                    converted_params=converted["params"],
                )
            )
            stats["generated"] += 1

    generated_from = output_file.parent.resolve()
    original_relative = os.path.relpath(original_dir.resolve(), start=generated_from)
    converted_relative = os.path.relpath(converted_dir.resolve(), start=generated_from)

    header = f'''"""Auto-generated semantic-equivalence test scaffolds.

Provide ``db_session`` and ``raw_connection`` fixtures in ``conftest.py``.
Set the test IDs below, review every generated argument, and use test data only.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


TEST_FILE_DIR = Path(__file__).resolve().parent
ORIGINAL_DIR = (TEST_FILE_DIR / {original_relative!r}).resolve()
CONVERTED_DIR = (TEST_FILE_DIR / {converted_relative!r}).resolve()
TEST_COMPANY_ID = 1  # TODO: use an isolated test record
TEST_ID = 1  # TODO: use an isolated test record


def _load_module(name: str, path: Path):
    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {{path}}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

'''

    atomic_write_text(output_file, header + "\n".join(test_functions))
    return stats


def main() -> None:
    """CLI entry point for the semantic-equivalence scaffold generator."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-dir", required=True)
    parser.add_argument("--converted-dir", required=True)
    parser.add_argument("--output-file", default="tests/test_semantic_equivalence.py")
    args = parser.parse_args()

    stats = generate_test_file(
        original_dir=Path(args.original_dir),
        converted_dir=Path(args.converted_dir),
        output_file=Path(args.output_file),
    )

    print(f"Generated {stats['generated']} test scaffolds")
    print(f"Skipped: {stats['skipped']} (async or missing database parameters)")
    print(f"Output: {args.output_file}")
    print("Review fixtures and test IDs before running.")


if __name__ == "__main__":
    main()
