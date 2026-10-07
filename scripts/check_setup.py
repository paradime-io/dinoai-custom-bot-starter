"""Check the app's configuration and its access to Paradime. Starts no agent run.

    python scripts/check_setup.py

It reads .streamlit/secrets.toml (or the environment) like the app does, and
reads a DinoAI session that does not exist: Paradime answers "not found" only
when the key, the workspace and the agents API all work. Exit code 1 on a failure.
"""

from __future__ import annotations

import re
import sys
import uuid

from common import SECRETS_FILE, live_backend, load_secrets, setting, source

from analyst.backend import BackendError

# A UI-created agent gets a generated slug: the first letters of its name and 6 hex digits.
_GENERATED_SLUG = re.compile(r"^[a-z0-9-]+-[0-9a-f]{6}$")


def diagnose(error: str) -> tuple[bool, str]:
    """What an error from reading a missing session says about the setup."""
    lowered = error.lower()
    if "not found" in lowered:
        return True, "The key, the workspace and the agents API work."
    if "not authorised for workspace" in lowered or "not authorized for workspace" in lowered:
        return False, "The key has no access to this workspace. Check PARADIME_WORKSPACE_UID."
    if "invalid, has expired" in lowered or "api key" in lowered:
        return False, "Paradime rejected the key. Create a new one, with access to the DinoAI agents API."
    if "firewall" in lowered:
        return False, "Paradime's firewall blocked the request. See docs/troubleshooting.md."
    return False, f"Unexpected answer from Paradime: {error}"


def main() -> int:
    secrets = load_secrets()
    failures = 0

    def report(ok: bool, text: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")

    print(f"Settings ({SECRETS_FILE if SECRETS_FILE.exists() else 'no secrets file: environment only'})")
    names = (
        "PARADIME_API_ENDPOINT", "PARADIME_API_SECRET", "PARADIME_WORKSPACE_UID", "PARADIME_API_KEY",
        "DINOAI_AGENT", "ANALYST_FEEDBACK_AGENT", "ANALYST_FEEDBACK_TEAM", "ANALYST_FEEDBACK_ASSIGNEE",
    )
    for name in names:
        value = setting(secrets, name, "")
        hidden = "SECRET" in name or name.endswith("_KEY")
        shown = "(set)" if hidden and value else (value or "(empty)")
        print(f"  {name:28s} {shown:32s} from {source(secrets, name)}")

    print("\nRequired values")
    endpoint = setting(secrets, "PARADIME_API_ENDPOINT", "")
    secret = setting(secrets, "PARADIME_API_SECRET", "")
    report(bool(endpoint), "PARADIME_API_ENDPOINT is set")
    report(bool(secret), "PARADIME_API_SECRET is set")
    if str(secret).startswith("prdm_cmp_"):
        report(bool(setting(secrets, "PARADIME_WORKSPACE_UID")), "An account key needs PARADIME_WORKSPACE_UID")
    agent = setting(secrets, "DINOAI_AGENT", "")
    report(bool(agent), "DINOAI_AGENT is set (without it, Report a problem stays off)")

    if endpoint and secret:
        print("\nParadime access")
        try:
            backend = live_backend(secrets)
            backend.get(uuid.uuid4().hex[:16])  # a session that does not exist
            report(False, "Paradime returned a session that should not exist.")
        except ValueError as exc:  # the SDK checks the credential combination
            report(False, f"The credentials are incomplete: {exc}")
        except BackendError as exc:
            report(*diagnose(str(exc)))
            identity = backend.key_identity()
            if identity:
                capabilities = identity.get("capabilities") or []
                report(
                    {"dinoai:agent:trigger", "dinoai:agent:read"} <= set(capabilities),
                    f"The key can start and read agent runs (capabilities: {', '.join(capabilities) or 'none'})",
                )
                expires = identity.get("expiresAt")
                print(f"  NOTE  The key expires on {expires[:10]}." if expires else "  NOTE  The key does not expire.")

    print("\nReport a problem")
    feedback = setting(secrets, "ANALYST_FEEDBACK_AGENT", "analytics-feedback")
    if agent and _GENERATED_SLUG.match(str(agent)) and not _GENERATED_SLUG.match(str(feedback)):
        report(
            False,
            f"DINOAI_AGENT is a generated slug, but ANALYST_FEEDBACK_AGENT is {feedback!r}. "
            "Use the feedback agent's slug from its builder URL.",
        )
    else:
        report(True, f"The analyst hands reports to {feedback!r}.")
    print(
        "  NOTE  Not checkable from here: LINEAR_API_KEY in the workspace's agent environment\n"
        "        variables, and the analyst's tools and squad. scripts/smoke_test.py --report tests them."
    )

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
