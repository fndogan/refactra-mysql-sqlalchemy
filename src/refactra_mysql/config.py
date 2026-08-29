"""
Configuration loader for MySQL to SQLAlchemy Migration Tool.

Loads settings from environment variables or .env file.
All values are configurable — no hardcoded defaults for paths or secrets.
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load configuration from the caller's working directory. Reading beside an
# installed package would be surprising and can target a read-only site-packages
# directory.
_WORKING_DIR = Path.cwd()
load_dotenv(_WORKING_DIR / ".env")


# =============================================================================
# Source & Output Paths
# =============================================================================
# SOURCE_DIRS supports comma-separated paths for scanning multiple directories
_SOURCE_DIR_RAW = os.environ.get("SOURCE_DIR", "")
SOURCE_DIRS: list[Path] = [
    Path(p.strip()) for p in _SOURCE_DIR_RAW.split(",") if p.strip()
]
MODELS_FILE = os.environ.get("MODELS_FILE", "")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", str(_WORKING_DIR / "output"))
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(_WORKING_DIR / "reports")))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")


# =============================================================================
# AI Provider Settings
# =============================================================================
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic")  # "anthropic" or "openai"
AI_API_KEY = os.environ.get("AI_API_KEY", "")
# Model identifiers change over time. Require callers to select a currently
# supported model instead of silently using a stale provider-specific default.
AI_MODEL = os.environ.get("AI_MODEL", "").strip()
SYSTEM_PROMPT_FILE = os.environ.get("SYSTEM_PROMPT_FILE", "").strip()
DYNAMIC_PROMPT_FILE = os.environ.get("DYNAMIC_PROMPT_FILE", "").strip()
AI_PROMPT_CACHING = os.environ.get("AI_PROMPT_CACHING", "true").lower() in ("true", "1", "yes")


# =============================================================================
# Rate Limiting (API provider limits)
# =============================================================================
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "5"))
RATE_LIMIT_INPUT_TPM = int(os.environ.get("RATE_LIMIT_INPUT_TPM", "10000"))
RATE_LIMIT_OUTPUT_TPM = int(os.environ.get("RATE_LIMIT_OUTPUT_TPM", "4000"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "5.0"))


# =============================================================================
# Logging Configuration
# =============================================================================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "")


def setup_logging(name: str = "migration") -> logging.Logger:
    """
    Configure and return a logger instance.

    Args:
        name: Logger name (used as prefix in log messages).

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Prevent duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-8s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # File handler (optional)
    if LOG_FILE:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_fmt)
        logger.addHandler(file_handler)

    return logger


def validate_config() -> list[str]:
    """
    Validate that all required configuration values are set.

    Returns:
        List of error messages. Empty list means all config is valid.
    """
    errors = []

    if not SOURCE_DIRS:
        errors.append("SOURCE_DIR is not set. Specify one or more directories (comma-separated).")
    else:
        for src in SOURCE_DIRS:
            if not src.is_dir():
                errors.append(f"SOURCE_DIR path '{src}' does not exist or is not a directory.")

    if not MODELS_FILE:
        errors.append("MODELS_FILE is not set. Specify the path to your SQLAlchemy models file.")
    elif not Path(MODELS_FILE).is_file():
        errors.append(f"MODELS_FILE '{MODELS_FILE}' does not exist or is not a file.")

    return errors
