"""The app itself, run headless with Streamlit's AppTest in demo mode."""

import os
import time

import pytest
from streamlit.testing.v1 import AppTest

from analyst.threads import ThreadStore
from conftest import ROOT


def _thread(question: str, session_id: str) -> list:
    return [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "answer": [f"The answer to: {question}"],
            "notes": [],
            "error": None,
            "seconds": 12.0,
            "session_id": session_id,
        },
    ]


def _app(tmp_path, **settings: str) -> AppTest:
    """The app with these settings only.

    Given secrets, AppTest replaces st.secrets, so the developer's own
    .streamlit/secrets.toml (with live credentials) is never read.
    """
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    app.secrets = {"ANALYST_THREADS_DIR": str(tmp_path), **settings}
    return app


@pytest.fixture
def app(tmp_path):
    store = ThreadStore(tmp_path)
    store.save("thread-a", _thread("Who won in 2017?", "thread-a"), None)
    store.save("thread-b", _thread("Who won in 2018?", "thread-b"), None)
    return _app(tmp_path, ANALYST_MODE="demo")


def _open(app: AppTest, thread: str) -> AppTest:
    app.query_params["thread"] = thread
    return app.run()


def _active_thread(app: AppTest) -> list:
    """The thread buttons drawn as the open one (with an outline)."""
    return [b.key for b in app.button if (b.key or "").startswith("thread-") and b.proto.type == "secondary"]


def test_a_thread_link_opens_that_conversation(app):
    _open(app, "thread-a")

    assert not app.exception
    assert app.session_state["session_id"] == "thread-a"
    assert _active_thread(app) == ["thread-thread-a"]


def test_clicking_a_recent_conversation_opens_it(app):
    _open(app, "thread-a")

    app.button(key="thread-thread-b").click().run()

    assert app.session_state["session_id"] == "thread-b"
    assert _active_thread(app) == ["thread-thread-b"]
    assert app.query_params["thread"] == "thread-b"
    assert any("Who won in 2018?" in m.value for m in app.markdown)


def test_a_click_still_lands_after_the_list_changed(app, tmp_path):
    """The flaky case: the order and the "1 min ago" times change between runs.

    Each thread's button is keyed by its session, so a click lands whatever moved.
    """
    _open(app, "thread-a")
    data = (tmp_path / "thread-b.json").read_text()
    time.sleep(0.01)
    (tmp_path / "thread-b.json").write_text(data)  # thread-b moves to the top
    os.utime(tmp_path / "thread-b.json")

    app.button(key="thread-thread-b").click().run()

    assert app.session_state["session_id"] == "thread-b"


def test_clicking_the_open_thread_keeps_it(app):
    _open(app, "thread-a")

    app.button(key="thread-thread-a").click().run()

    assert app.session_state["session_id"] == "thread-a"
    assert not app.exception


def test_new_conversation_clears_the_thread_and_the_selection(app):
    _open(app, "thread-a")

    app.button(key="new_conversation").click().run()

    assert "session_id" not in app.session_state
    assert _active_thread(app) == []
    assert "thread" not in app.query_params
    # The selection must not pull the old thread back on the next run.
    app.run()
    assert "session_id" not in app.session_state


def _wait_until(app: AppTest, done, timeout: float = 15.0) -> None:
    """Rerun the app until ``done(app)``, as the watcher's timer would in a browser."""
    deadline = time.monotonic() + timeout
    while not done(app):
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)
        app.run()


def test_a_report_on_an_answer_comes_back_with_its_linear_issue(tmp_path, recorded):
    """The whole squad flow, in demo mode: report, acknowledgement, then the issue link."""
    app = _app(
        tmp_path,
        ANALYST_MODE="demo",
        ANALYST_REPLAY_SPEED="1000",
        ANALYST_REPLAY_MIN_STEP="0.3",
        ANALYST_POLL_SECONDS="0.05",
        ANALYST_FEEDBACK_ASSIGNEE="ana@example.com",
    ).run()
    app.chat_input[0].set_value(recorded["01-constructor-wins"]["question"]).run()
    session_id = app.session_state["session_id"]
    key = f"{session_id}-1"

    app.session_state[f"{key}-report-category"] = "Wrong number"
    app.button(key=f"{key}-report").click().run()  # opens the dialog
    app.text_area(key=f"{key}-report-comment").input("Ferrari won 2 races, not 1.")
    app.button(key="send_report").click().run()  # sends, then waits for the acknowledgement

    history = app.session_state["history"]
    report, ack = history[2], history[3]
    assert (report["role"], report["category"], report["status"]) == ("report", "Wrong number", "waiting")
    assert report["comment"] == "Ferrari won 2 races, not 1."
    assert ack["kind"] == "ack" and "data team has your report" in ack["answer"][0]
    assert any("Waiting for the Linear issue" in c.value for c in app.caption)

    _wait_until(app, lambda a: a.session_state["history"][2]["status"] != "waiting")

    history = app.session_state["history"]
    assert history[2]["status"] == "filed"
    assert history[2]["issue"]["key"] == "DNA-1"
    assert history[-1]["kind"] == "issue" and "ana@example.com" in history[-1]["answer"][0]
    assert any("Filed as [DNA-1]" in c.value for c in app.caption)
    # The thread on disk has it too, for the History page.
    (summary,) = ThreadStore(tmp_path).list()
    assert (summary.reports, summary.issues, summary.answered) == (1, ("DNA-1",), 1)


def test_reports_need_the_squad_agent_in_live_mode(tmp_path):
    store = ThreadStore(tmp_path)
    store.save("thread-a", _thread("Who won in 2017?", "thread-a"), None)
    app = _app(
        tmp_path,
        ANALYST_MODE="live",
        PARADIME_API_ENDPOINT="https://paradime.invalid/graphql",
        PARADIME_API_SECRET="prdm_cmp_test",
        PARADIME_WORKSPACE_UID="ws1",
    )  # and no DINOAI_AGENT
    app.query_params["thread"] = "thread-a"
    app.run()

    button = app.button(key="thread-a-1-report")
    assert button.disabled
    assert "DINOAI_AGENT" in button.help


def test_the_developers_live_settings_never_reach_a_test(app):
    """Streamlit copies a loaded secrets.toml into os.environ, where setting() would find it."""
    _open(app, "thread-a")
    app.run()

    assert not app.exception
    assert not [name for name in os.environ if name.startswith(("PARADIME_API", "DINOAI_"))]
