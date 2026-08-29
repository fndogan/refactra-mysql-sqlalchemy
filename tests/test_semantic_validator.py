import py_compile
from pathlib import Path

from refactra_mysql.quality.semantic_validator import generate_test_file


def test_generated_scaffold_is_generic_and_compiles(tmp_path: Path) -> None:
    original = tmp_path / "original"
    converted = tmp_path / "converted"
    output = tmp_path / "tests" / "test_equivalence.py"
    original.mkdir()
    converted.mkdir()
    (original / "queries.py").write_text(
        "def load_user(connection, user_id):\n    return {'id': user_id}\n",
        encoding="utf-8",
    )
    (converted / "queries.py").write_text(
        "def load_user(db, user_id):\n    return {'id': user_id}\n",
        encoding="utf-8",
    )

    stats = generate_test_file(original, converted, output)
    generated = output.read_text(encoding="utf-8")

    assert stats == {"total": 1, "generated": 1, "skipped": 0}
    assert str(tmp_path) not in generated
    assert "TEST_FILE_DIR = Path(__file__).resolve().parent" in generated
    assert "connection=raw_connection" in generated
    assert "db=db_session" in generated
    py_compile.compile(str(output), doraise=True)
