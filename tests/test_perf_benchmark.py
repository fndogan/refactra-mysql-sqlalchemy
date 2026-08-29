import py_compile
from pathlib import Path

from refactra_mysql.quality.perf_benchmark import generate_benchmark_file


def test_generated_benchmark_is_generic_and_compiles(tmp_path: Path) -> None:
    converted = tmp_path / "converted"
    output = tmp_path / "tests" / "test_performance.py"
    converted.mkdir()
    (converted / "queries.py").write_text(
        "def load_user(db, user_id):\n    return {'id': user_id}\n",
        encoding="utf-8",
    )

    stats = generate_benchmark_file(converted, output)
    generated = output.read_text(encoding="utf-8")

    assert stats == {"total": 1, "generated": 1}
    assert 'migration_pairs["queries:load_user"]' in generated
    assert "original_load_user" not in generated
    assert "converted_load_user" not in generated
    py_compile.compile(str(output), doraise=True)
