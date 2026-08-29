import stat
from pathlib import Path

from refactra_mysql.io_utils import atomic_write_text


def test_atomic_write_creates_parent_and_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "result.py"

    atomic_write_text(target, "print('ok')\n")

    assert target.read_text(encoding="utf-8") == "print('ok')\n"
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_atomic_write_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "script.py"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)

    atomic_write_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
