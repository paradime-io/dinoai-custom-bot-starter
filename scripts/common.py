"""Settings and the Paradime backend for the command-line scripts, read the way the app reads them."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PARADIME_DISABLE_VERSION_CHECK", "1")  # no PyPI call on each run

from analyst.backend import ParadimeBackend  # noqa: E402

SECRETS_FILE = ROOT / ".streamlit" / "secrets.toml"


def load_secrets(path: Path = SECRETS_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


def setting(secrets: dict[str, Any], name: str, default: Any = None) -> Any:
    """Like the app: a name in the secrets file wins, even when blank; else the environment."""
    value = secrets[name] if name in secrets else os.environ.get(name)
    return default if value in (None, "") else value


def source(secrets: dict[str, Any], name: str) -> str:
    if name in secrets:
        return "secrets.toml"
    return "environment" if os.environ.get(name) else "not set"


def live_backend(secrets: dict[str, Any]) -> ParadimeBackend:
    return ParadimeBackend(
        api_endpoint=setting(secrets, "PARADIME_API_ENDPOINT", ""),
        api_secret=setting(secrets, "PARADIME_API_SECRET", ""),
        api_key=setting(secrets, "PARADIME_API_KEY", ""),
        workspace_uid=setting(secrets, "PARADIME_WORKSPACE_UID", ""),
        agent=setting(secrets, "DINOAI_AGENT", ""),
        model_family=setting(secrets, "DINOAI_MODEL_FAMILY", ""),
    )
