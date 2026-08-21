from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_project_environment() -> Path:
    """Load this project's .env file and return its resolved location."""
    load_dotenv(PROJECT_ENV_FILE)
    return PROJECT_ENV_FILE
