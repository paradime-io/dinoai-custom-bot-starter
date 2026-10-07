import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "fixtures" / "replay"


@pytest.fixture(autouse=True)
def no_ambient_settings(monkeypatch, tmp_path_factory):
    """Tests never see the developer's live credentials or app settings.

    Two sources leak them: the shell's PARADIME_* variables, and the
    developer's .streamlit/secrets.toml. Streamlit copies every top-level
    value of that file into os.environ when it loads it (st.user does, for
    its auth section), so point Streamlit at an empty secrets file instead.
    """
    import os

    from streamlit import config
    from streamlit.runtime.secrets import secrets_singleton

    empty = tmp_path_factory.mktemp("secrets") / "secrets.toml"
    empty.write_text("")
    files = config.get_option("secrets.files")
    secrets_singleton._reset()  # forget, and take out of os.environ, a file loaded earlier
    config.set_option("secrets.files", [str(empty)])
    for name in list(os.environ):
        if name.startswith(("PARADIME_", "DINOAI_", "ANALYST_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PARADIME_DISABLE_VERSION_CHECK", "1")
    yield
    secrets_singleton._reset()
    config.set_option("secrets.files", files)


@pytest.fixture
def recorded():
    """The recorded DinoAI answers, keyed by file stem."""
    return {path.stem: json.loads(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))}
