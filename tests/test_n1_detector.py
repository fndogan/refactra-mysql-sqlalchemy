from pathlib import Path

from refactra_mysql.quality.n1_detector import scan_file_for_n1


def _scan(tmp_path: Path, source: str, **kwargs):
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    return scan_file_for_n1(target, **kwargs)


def test_detects_session_query_inside_loop(tmp_path: Path) -> None:
    warnings = _scan(
        tmp_path,
        "def load_all(db):\n"
        "    for item_id in [1, 2]:\n"
        "        db.query(Item).get(item_id)\n",
    )

    assert any("db.query() inside loop" in warning.pattern for warning in warnings)


def test_does_not_guess_plural_attributes_are_relationships(tmp_path: Path) -> None:
    warnings = _scan(
        tmp_path,
        "def render(items):\n"
        "    for item in items:\n"
        "        print(item.values)\n",
    )

    assert warnings == []


def test_detects_relationship_from_model_metadata(tmp_path: Path) -> None:
    warnings = _scan(
        tmp_path,
        "def render(users):\n"
        "    for user in users:\n"
        "        print(user.orders)\n",
        known_relationships={"orders"},
    )

    assert any("user.orders" in warning.pattern for warning in warnings)
