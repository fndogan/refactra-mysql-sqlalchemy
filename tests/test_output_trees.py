from pathlib import Path
from typing import cast

from refactra_mysql.codemods.reporter import CodemodReporter
from refactra_mysql.codemods.runner import process_file
from refactra_mysql.converter.engine import AIProvider, convert_file
from refactra_mysql.converter.model_extractor import ModelExtractor
from refactra_mysql.converter.rate_limiter import RateLimiter


def _unused_converter_dependencies() -> tuple[AIProvider, ModelExtractor, RateLimiter]:
    """Return typed sentinels for converter paths that exit before using dependencies."""
    return (
        cast(AIProvider, object()),
        cast(ModelExtractor, object()),
        cast(RateLimiter, object()),
    )


def test_codemod_output_includes_unchanged_python_files(tmp_path: Path) -> None:
    source = tmp_path / "source" / "helpers.py"
    output = tmp_path / "output" / "helpers.py"
    source.parent.mkdir()
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = process_file(source, output, CodemodReporter())

    assert result["status"] == "skipped"
    assert output.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_converter_output_includes_files_without_sql(tmp_path: Path) -> None:
    source = tmp_path / "source" / "helpers.py"
    output = tmp_path / "output" / "helpers.py"
    source.parent.mkdir()
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = convert_file(source, output, *_unused_converter_dependencies())

    assert result["status"] == "skipped"
    assert output.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_converter_writes_manual_review_marker_for_skipped_sql(tmp_path: Path) -> None:
    source = tmp_path / "source" / "schema.py"
    output = tmp_path / "output" / "schema.py"
    source.parent.mkdir()
    source.write_text(
        "def create_table(connection):\n"
        "    connection.cursor().execute('CREATE TABLE audit_log (id INT)')\n",
        encoding="utf-8",
    )

    result = convert_file(source, output, *_unused_converter_dependencies())
    generated = output.read_text(encoding="utf-8")

    assert result["functions_skipped_unsafe"] == 1
    assert "MANUAL REVIEW REQUIRED" in generated
    assert "CREATE TABLE audit_log" in generated
