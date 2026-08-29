from pathlib import Path

import pytest

from refactra_mysql.converter.prompt_loader import load_prompt


def test_loads_packaged_example_prompt() -> None:
    prompt = load_prompt("", "system_prompt.example.txt")

    assert "general example" in prompt


def test_loads_local_override(tmp_path: Path) -> None:
    prompt_file = tmp_path / "custom-prompt.txt"
    prompt_file.write_text("Project-owned prompt", encoding="utf-8")

    assert load_prompt(str(prompt_file), "system_prompt.example.txt") == "Project-owned prompt"


def test_rejects_empty_prompt(tmp_path: Path) -> None:
    prompt_file = tmp_path / "empty.txt"
    prompt_file.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Prompt file is empty"):
        load_prompt(str(prompt_file), "system_prompt.example.txt")
