from pathlib import Path

from refactra_mysql.converter.post_process import post_process_file


def test_project_import_keeps_unrelated_helpers(tmp_path: Path) -> None:
    source = tmp_path / "queries.py"
    source.write_text(
        "from example_project.data.helpers import get_db_connection, keep_me\n",
        encoding="utf-8",
    )

    cleaned, _ = post_process_file(source)

    assert "get_db_connection" not in cleaned
    assert "from example_project.data.helpers import keep_me" in cleaned


def test_legacy_driver_import_is_removed(tmp_path: Path) -> None:
    source = tmp_path / "queries.py"
    source.write_text("from pymysql.cursors import DictCursor\n", encoding="utf-8")

    cleaned, _ = post_process_file(source)

    assert "pymysql" not in cleaned
