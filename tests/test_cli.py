import sys

from refactra_mysql import __version__
from refactra_mysql.__main__ import main


def test_top_level_help(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["refactra-mysql", "--help"])

    main()

    output = capsys.readouterr().out
    assert "Refactra: MySQL to SQLAlchemy" in output
    for command in (
        "analyze",
        "codemods",
        "convert",
        "convert-dynamic",
        "post-process",
        "syntax",
        "validate",
        "models",
        "n1",
        "consistency",
        "fix-consistency",
        "quality",
        "coverage",
        "compare",
        "semantic",
        "benchmark",
    ):
        assert f"refactra-mysql {command}" in output


def test_top_level_version(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["refactra-mysql", "--version"])

    main()

    assert capsys.readouterr().out.strip() == __version__
