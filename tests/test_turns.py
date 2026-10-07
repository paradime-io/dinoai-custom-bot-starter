from analyst.backend import AgentMessage, RunSnapshot
from analyst.turns import (
    NO_REPLY_SECONDS,
    SETTLE_SECONDS,
    TURN_TIMEOUT_SECONDS,
    PendingTurn,
    observe,
    outcome,
)

FINAL = 'Answer.\n\n```viz\n{"type": "table", "data": [{"a": 1}]}\n```'
PREVIOUS = AgentMessage(1.0, "agent", "Earlier answer.")


def snap(status, *contents, history=()):
    messages = list(history) + [AgentMessage(10.0 + i, "agent", c) for i, c in enumerate(contents)]
    return RunSnapshot(status=status, messages=messages)


def test_first_turn_ends_when_the_run_completes():
    turn = PendingTurn("s1", "q", baseline=0, sent_at=0.0, first_turn=True)
    assert not observe(turn, snap("QUEUED"), 1)
    assert not observe(turn, snap("RUNNING", "Looking at race results."), 20)
    assert observe(turn, snap("COMPLETED", "Looking at race results.", FINAL), 40)

    result = outcome(turn, snap("COMPLETED", "Looking at race results.", FINAL), 40)
    assert result.error is None
    assert result.answer == [FINAL]
    assert result.notes == ["Looking at race results."]


def test_follow_up_ignores_the_previous_turns_completed_status():
    turn = PendingTurn("s1", "q2", baseline=1, sent_at=100.0, first_turn=False)
    stale = snap("COMPLETED", history=[PREVIOUS])
    assert not observe(turn, stale, 101)  # the run has not picked up the message yet
    assert not observe(turn, snap("RUNNING", history=[PREVIOUS]), 104)
    done = snap("COMPLETED", FINAL, history=[PREVIOUS])
    assert observe(turn, done, 130)
    assert outcome(turn, done, 130).answer == [FINAL]


def test_follow_up_that_finished_between_polls_settles_first():
    turn = PendingTurn("s1", "q2", baseline=1, sent_at=100.0, first_turn=False)
    done = snap("COMPLETED", FINAL, history=[PREVIOUS])
    assert not observe(turn, done, 110)  # new message seen, but never saw the run active
    assert observe(turn, done, 110 + SETTLE_SECONDS)


def test_follow_up_without_any_reply_gives_up():
    turn = PendingTurn("s1", "q2", baseline=1, sent_at=100.0, first_turn=False)
    stale = snap("COMPLETED", history=[PREVIOUS])
    assert not observe(turn, stale, 150)
    assert observe(turn, stale, 100 + NO_REPLY_SECONDS)
    assert outcome(turn, stale, 100 + NO_REPLY_SECONDS).error == "The agent finished without replying."


def test_user_messages_in_the_transcript_are_not_answers():
    turn = PendingTurn("s1", "q2", baseline=1, sent_at=0.0, first_turn=False, saw_active=True)
    run = RunSnapshot("COMPLETED", [PREVIOUS, AgentMessage(5, "user", "q2"), AgentMessage(9, "agent", FINAL)])
    assert observe(turn, run, 30)
    assert outcome(turn, run, 30).answer == [FINAL]


def test_an_earlier_message_with_a_visual_stays_in_the_answer():
    chart = '```viz\n{"type": "table", "data": [{"a": 1}]}\n```'
    turn = PendingTurn("s1", "q", baseline=0, sent_at=0.0, first_turn=True)
    run = snap("COMPLETED", "On it.", chart, "Summary.")
    result = outcome(turn, run, 60)
    assert result.answer == [chart, "Summary."]
    assert result.notes == ["On it."]


def test_failed_and_expired_runs_explain_themselves():
    turn = PendingTurn("s1", "q", baseline=0, sent_at=0.0, first_turn=True)
    assert outcome(turn, snap("FAILED", "Warehouse error."), 5).error == "The agent run failed."
    assert "never started" in outcome(turn, snap("EXPIRED"), 5).error


def test_a_run_that_never_finishes_times_out():
    turn = PendingTurn("s1", "q", baseline=0, sent_at=0.0, first_turn=True)
    assert observe(turn, snap("RUNNING"), TURN_TIMEOUT_SECONDS)
    result = outcome(turn, snap("RUNNING"), TURN_TIMEOUT_SECONDS)
    assert result.status == "TIMEOUT" and "s1" in result.error
