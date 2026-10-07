import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))

from check_setup import diagnose  # noqa: E402
from common import setting  # noqa: E402


@pytest.mark.parametrize(
    ("error", "ok", "hint"),
    [
        ("agent_session_id '3f2a' not found", True, "work"),
        ("company API key is not authorised for workspace 'abc'", False, "PARADIME_WORKSPACE_UID"),
        ("The provided API key is either invalid, has expired, or does not have the required permissions.", False, "new one"),
        ("Paradime's API firewall blocked the request: ...", False, "troubleshooting"),
        ("Something else", False, "Unexpected"),
    ],
)
def test_the_setup_check_explains_what_paradime_answered(error, ok, hint):
    result, text = diagnose(error)
    assert result is ok and hint in text


def test_scripts_read_settings_like_the_app(monkeypatch):
    monkeypatch.setenv("DINOAI_AGENT", "from-shell")
    monkeypatch.setenv("ANALYST_FEEDBACK_TEAM", "OPS")

    secrets = {"DINOAI_AGENT": ""}

    assert setting(secrets, "DINOAI_AGENT", "default") == "default"  # the file wins, even when blank
    assert setting(secrets, "ANALYST_FEEDBACK_TEAM", "DNA") == "OPS"  # not in the file: the environment
