import time

import pytest
import requests
from paradime.client.api_exception import ParadimeAPIException

from analyst.backend import AgentMessage, BackendError, ParadimeBackend, ReplayBackend
from analyst.protocol import build_first_message, build_follow_up_message
from conftest import FIXTURES


@pytest.fixture
def paradime(monkeypatch):
    """A ParadimeBackend whose GraphQL calls are answered locally, never sent."""
    monkeypatch.setenv("PARADIME_DISABLE_VERSION_CHECK", "1")
    backend = ParadimeBackend(
        api_endpoint="https://paradime.invalid/graphql",
        api_secret="prdm_cmp_test",
        workspace_uid="ws1",
        agent="stakeholder-analyst",
    )
    calls = []

    def answer(query, variables={}):
        calls.append(variables)
        if "triggerDinoaiAgentRun" in query:
            return {"triggerDinoaiAgentRun": {"ok": True, "agentSessionId": "s1", "status": "queued"}}
        if "sendDinoaiAgentMessage" in query:
            return {"sendDinoaiAgentMessage": {"ok": True, "agentSessionId": "s1", "status": "queued"}}
        return {
            "dinoaiAgentRun": {
                "ok": True,
                "status": "WAITING",
                "messages": [{"ts": 1791362175.55, "role": "agent", "content": "Done."}],
                "childSessionIds": [],
                "workspaceUid": "ws1",
            }
        }

    monkeypatch.setattr(backend._client, "_call_gql", answer)
    return backend, calls


def test_paradime_backend_drives_the_sdk(paradime):
    backend, calls = paradime
    assert backend.start("q1") == ("s1", None)
    backend.send("s1", "q2")
    run = backend.get("s1")
    assert calls[0]["agent"] == "stakeholder-analyst" and calls[0]["message"] == "q1"
    assert calls[1] == {"id": "s1", "message": "q2"}
    # A status this app does not know must keep it polling.
    assert run.status == "WAITING" and not run.terminal
    assert run.messages[0].ts == 1791362175.55 and run.messages[0].from_agent


def test_a_model_family_goes_to_graphql_and_its_warning_comes_back(paradime, monkeypatch):
    backend, _ = paradime
    backend.model_family = "fast-preset"
    sent = {}

    def answer(query, variables={}):
        sent.update(variables)
        assert "modelFamily: $modelFamily" in query
        run = {"ok": True, "agentSessionId": "s2", "status": "queued", "warning": "Unknown family; used default"}
        return {"triggerDinoaiAgentRun": run}

    monkeypatch.setattr(backend._client, "_call_gql", answer)
    assert backend.start("q") == ("s2", "Unknown family; used default")
    assert sent["modelFamily"] == "fast-preset" and sent["agent"] == "stakeholder-analyst"


@pytest.mark.parametrize(
    "failure", [ParadimeAPIException("Agent 'x' not found"), requests.ConnectionError("network down")]
)
def test_paradime_failures_become_backend_errors(paradime, monkeypatch, failure):
    backend, _ = paradime

    def fail(query, variables={}):
        raise failure

    monkeypatch.setattr(backend._client, "_call_gql", fail)
    with pytest.raises(BackendError, match=str(failure).split()[0]):
        backend.start("q")


def wait(backend, session_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        run = backend.get(session_id)
        if run.terminal or time.monotonic() > deadline:
            return run
        time.sleep(0.01)


def test_replay_answers_a_recorded_question_then_a_follow_up(recorded):
    backend = ReplayBackend(FIXTURES, speed=1000, min_step=0)
    first = recorded["01-constructor-wins"]
    session, _ = backend.start(build_first_message(first["question"]))
    run = wait(backend, session)
    assert run.status == "COMPLETED"
    assert [m.content for m in run.messages] == [first["turn"][0]["content"]]

    backend.send(session, build_follow_up_message("Zoom in on Red Bull's 2023 season headline numbers"))
    run = wait(backend, session)
    assert len(run.messages) == 2
    assert run.messages[1].content.startswith("Red Bull dominated the 2023 season")


def test_replay_lists_its_questions_when_it_has_no_recording():
    backend = ReplayBackend(FIXTURES, speed=1000, min_step=0)
    run = wait(backend, backend.start(build_first_message("What is our churn by cohort?"))[0])
    assert "Demo mode only has recorded answers" in run.messages[0].content


def test_roles_other_than_the_agent_are_not_answers():
    assert AgentMessage(0, "agent", "x").from_agent
    assert AgentMessage(0, "assistant", "x").from_agent
    assert not AgentMessage(0, "user", "x").from_agent


STEPS_RUN = {
    "status": "RUNNING",
    "messages": [],
    "childSessionIds": [],
    "steps": [
        {"index": 0, "role": "USER", "toolName": None, "toolInput": None, "content": "question"},
        {"index": 1, "role": "TOOL", "toolName": "run_sql_query", "toolInput": '{"query": "select 1"}', "content": "1"},
    ],
    "startupSteps": [{"label": "Starting the agent", "done": True}],
}


def test_steps_and_the_startup_checklist_come_back_when_the_api_has_them(paradime, monkeypatch):
    backend, _ = paradime
    monkeypatch.setattr(backend._client, "_call_gql", lambda query, variables={}: {"dinoaiAgentRun": STEPS_RUN})

    run = backend.get("s1")
    assert [(s.index, s.role, s.tool_name) for s in run.steps] == [(0, "user", None), (1, "tool", "run_sql_query")]
    assert run.startup[0].label == "Starting the agent" and run.startup[0].done


def test_an_api_without_steps_falls_back_to_the_plain_run_once(paradime, monkeypatch):
    backend, _ = paradime
    queries = []

    def answer(query, variables={}):
        queries.append("steps" if "steps(" in query else "plain")
        if "steps(" in query:
            raise ParadimeAPIException("Error: 400 - Cannot query field 'steps' on type 'DinoAiAgentRun'.")
        run = {"ok": True, "status": "RUNNING", "messages": [], "childSessionIds": [], "workspaceUid": "ws1"}
        return {"dinoaiAgentRun": run}

    monkeypatch.setattr(backend._client, "_call_gql", answer)
    first, second = backend.get("s1"), backend.get("s1")
    assert first.status == second.status == "RUNNING" and not first.steps
    assert queries == ["steps", "plain", "plain"]  # it does not try again after the first refusal


def test_replay_acknowledges_a_report_then_wakes_up_with_the_issue_link(monkeypatch):
    """The squad flow: an acknowledgement, an idle run, then the callback's reply."""
    from analyst.feedback import FeedbackReport, build_feedback_message, find_issue

    clock = [0.0]
    monkeypatch.setattr("analyst.backend.time.monotonic", lambda: clock[0])
    backend = ReplayBackend(FIXTURES, speed=1, min_step=0)
    session_id, _ = backend.start("Anything")
    backend._sessions[session_id].queue.clear()  # skip the reply to the first message
    clock[0] = 1000.0
    report = FeedbackReport("Wrong number", "", "Q", "A", [], session_id, "u", "analytics-feedback", "DNA", "ana@example.com")
    backend.send(session_id, build_feedback_message(report))

    statuses = []
    for at in (1001.0, 1004.5, 1010.0, 1033.0, 1035.0):
        clock[0] = at
        run = backend.get(session_id)
        statuses.append((run.status, len(run.messages)))

    assert statuses == [
        ("RUNNING", 0),  # working on the acknowledgement
        ("COMPLETED", 1),  # acknowledged; idle while the feedback agent works
        ("COMPLETED", 1),
        ("RUNNING", 1),  # the callback woke the session up
        ("COMPLETED", 2),
    ]
    assert find_issue(run.messages[-1].content)["key"] == "DNA-1"
    assert "ana@example.com" in run.messages[-1].content


def test_messages_reach_the_api_without_links_to_local_hosts(paradime):
    """The API firewall answers HTTP 403 to a body that links to localhost."""
    backend, calls = paradime

    backend.start("Look at http://localhost:8765/?thread=abc and https://linear.app/a/issue/DNA-1")
    backend.send("s1", "Reported from http://127.0.0.1:8765/?thread=abc")

    assert calls[0]["message"] == "Look at localhost:8765/?thread=abc and https://linear.app/a/issue/DNA-1"
    assert calls[1]["message"] == "Reported from 127.0.0.1:8765/?thread=abc"


def test_a_firewall_block_becomes_a_readable_error(paradime, monkeypatch):
    from test_sanitize import FIREWALL_PAGE

    backend, _ = paradime

    def blocked(query, variables={}):
        raise ParadimeAPIException(FIREWALL_PAGE)

    monkeypatch.setattr(backend._client, "_call_gql", blocked)
    with pytest.raises(BackendError) as caught:
        backend.send("s1", "hello")

    assert "firewall blocked the request" in str(caught.value)
    assert "<div" not in str(caught.value)


def test_the_key_identity_comes_back_or_is_none(paradime, monkeypatch):
    backend, _ = paradime
    identity = {"capabilities": ["dinoai:agent:read", "dinoai:agent:trigger"], "expiresAt": "2030-06-10T09:25:43"}
    monkeypatch.setattr(backend._client, "_call_gql", lambda query, variables={}: {"apiKeyIdentity": identity})
    assert backend.key_identity() == identity

    def fails(query, variables={}):
        raise ParadimeAPIException("Cannot query field 'apiKeyIdentity'")

    monkeypatch.setattr(backend._client, "_call_gql", fails)
    assert backend.key_identity() is None
