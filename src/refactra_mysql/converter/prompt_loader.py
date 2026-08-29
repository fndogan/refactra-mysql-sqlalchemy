"""Load public example prompts or caller-provided local prompt files."""

from pathlib import Path


_EXAMPLE_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(override_path: str, example_filename: str) -> str:
    """Return a non-empty prompt from a local override or packaged example."""
    prompt_path = Path(override_path).expanduser() if override_path else _EXAMPLE_PROMPTS_DIR / example_filename

    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Unable to read prompt file: {prompt_path}") from exc

    if not prompt:
        raise ValueError(f"Prompt file is empty: {prompt_path}")

    return prompt
