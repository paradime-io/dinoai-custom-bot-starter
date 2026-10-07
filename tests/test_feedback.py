from analyst.backend import AgentMessage, RunSnapshot
from analyst.feedback import (
    WATCH_SECONDS,
    FeedbackReport,
    answer_context,
    build_feedback_message,
    find_issue,
    pull_issue_reply,
    record_reply,
    report_item,
    reported,
    waiting_report,
    watch,
)

ANSWER = (
    "Red Bull won 21 of 22 races in 2023.\n\n"
    '```viz\n{"type": "bar", "title": "Wins by team", "x": "team", "y": "wins",\n'
    ' "sql": "select team,\\n  count(*) as wins -- race winners only\\nfrom wins group by 1",\n'
    ' "columns": ["team", "wins"], "rows": [["Red Bull", 21], ["Ferrari", 1]]}\n```'
)
HISTORY = [
    {"role": "user", "content": "How dominant was Red Bull in 2023?"},
    {"role": "assistant", "answer": [ANSWER], "notes": [], "error": None, "session_id": "s1",
     "transcript": [0, 3]},
]


def _report(**overrides) -> FeedbackReport:
    values = dict(
        category="Wrong number",
        comment="Ferrari won 2 races, not 1.",
        question="How dominant was Red Bull in 2023?",
        answer="Red Bull won 21 of 22 races in 2023.",
        visuals=[{"title": "Wins by team", "sql": "select team,\n  count(*) as wins -- race winners only\nfrom wins"}],
        session_id="s1",
        conversation_url="https://app.example.com/?thread=s1",
        feedback_agent="analytics-feedback",
        team_key="DNA",
        assignee_email="ana@example.com",
        reporter="ben@example.com",
    )
    return FeedbackReport(**{**values, **overrides})


def _snapshot(contents: list[str], status: str = "COMPLETED") -> RunSnapshot:
    return RunSnapshot(status=status, messages=[AgentMessage(ts=i, role="agent", content=c) for i, c in enumerate(contents)])


def test_the_brief_routes_the_report_to_the_feedback_agent_with_a_callback():
    message = build_feedback_message(_report())

    assert message.startswith("<feedback>\nCategory: Wrong number\nReader's comment: Ferrari won 2 races, not 1.\n")
    assert "Reported by: ben@example.com" in message
    assert 'Start the `analytics-feedback` agent with invoke_agent, with callback_session_id="s1"' in message
    assert 'the Linear team key "DNA", assignee_email "ana@example.com"' in message
    assert "https://app.example.com/?thread=s1" in message
    # The SQL keeps its lines: a -- comment must not swallow the rest of the query.
    assert "```sql\nselect team,\n  count(*) as wins -- race winners only\nfrom wins\n```" in message


def test_without_an_assignee_the_team_triages_the_issue():
    message = build_feedback_message(_report(assignee_email="", reporter="", comment="  ", visuals=[]))

    assert "no assignee (the team triages the issue)" in message
    assert "assignee_email" not in message
    assert "Reader's comment: (none)" in message
    assert "Reported by: a reader who is not signed in" in message
    assert "Visuals in the answer:\n- (none)" in message


def test_a_long_answer_is_cut_but_its_sql_is_not():
    message = build_feedback_message(_report(answer="x" * 5000, visuals=[{"title": "T", "sql": "y" * 3000}]))

    assert "x" * 1500 + " …" in message and "x" * 1501 not in message
    assert "y" * 3000 in message


def test_the_answer_context_has_the_question_the_prose_and_each_visual():
    question, prose, visuals = answer_context(HISTORY, 1)

    assert question == "How dominant was Red Bull in 2023?"
    assert prose == "Red Bull won 21 of 22 races in 2023."
    assert visuals == [{"title": "Wins by team", "sql": "select team,\n  count(*) as wins -- race winners only\nfrom wins group by 1"}]


def test_issue_links_are_found_in_markdown_and_bare_urls():
    assert find_issue("Filed [DNA-42](https://linear.app/acme/issue/DNA-42/wrong-wins-count), assigned to Ana.") == {
        "key": "DNA-42",
        "url": "https://linear.app/acme/issue/DNA-42/wrong-wins-count",
    }
    assert find_issue("See https://linear.app/acme/issue/DNA-7.")["key"] == "DNA-7"
    assert find_issue("COVID-19 cut the 2020 calendar; see https://example.com/issue/DNA-1") is None


def test_a_reply_with_a_link_files_the_report_and_one_without_only_answers_it():
    filed, replied = report_item("Other", "", 1, "Q", 0.0), report_item("Other", "", 1, "Q", 0.0)

    record_reply(filed, ["Working on it.", "Filed [DNA-42](https://linear.app/acme/issue/DNA-42)."])
    record_reply(replied, ["The feedback agent could not file the issue: LINEAR_API_KEY is not set."])

    assert (filed["status"], filed["issue"]["key"]) == ("filed", "DNA-42")
    assert (replied["status"], replied["issue"]) == ("replied", None)


def test_replies_go_to_reports_in_the_order_they_were_sent():
    first, second = report_item("Other", "", 1, "Q", 0.0), report_item("Other", "", 1, "Q", 1.0)
    first["status"] = "failed"
    history = HISTORY + [first, second]

    assert waiting_report(history) is second
    assert reported(history, 1) is second  # a failed report does not count: the reader can report again
    second["status"] = "filed"
    assert waiting_report(history) is None


def _waiting(acknowledged_at: int, sent_at: float = 0.0):
    report = report_item("Wrong number", "", 1, "Q", sent_at)
    ack = {"role": "assistant", "answer": ["Thanks."], "notes": [], "error": None, "session_id": "s1",
           "kind": "ack", "transcript": [3, acknowledged_at]}
    return report, HISTORY + [report, ack]


def test_the_watcher_waits_then_takes_the_unclaimed_reply_once_the_run_is_idle():
    report, history = _waiting(acknowledged_at=4)
    before = ["a", "b", "c", "Thanks."]
    reply = "Filed [DNA-42](https://linear.app/acme/issue/DNA-42)."

    assert watch(history, report, _snapshot(before), 60.0, None).state == "waiting"
    assert watch(history, report, _snapshot(before, "RUNNING"), 61.0, 4).state == "posting"
    # Idle with a new message, but the transcript just grew: poll once more.
    assert watch(history, report, _snapshot(before + [reply]), 62.0, 4).state == "waiting"
    result = watch(history, report, _snapshot(before + [reply]), 63.0, 5)
    assert (result.state, result.reply, result.transcript) == ("done", [reply], [4, 5])


def test_messages_that_a_later_turn_claimed_are_not_the_reply():
    """The reader asked a follow-up meanwhile; the callback came after it."""
    report, history = _waiting(acknowledged_at=4)
    follow_up = {"role": "assistant", "answer": ["2 wins."], "notes": [], "error": None, "session_id": "s1",
                 "transcript": [4, 6]}
    history.append(follow_up)
    snapshot = _snapshot(["a", "b", "c", "Thanks.", "Checking…", "2 wins.", "Filed DNA-42."])

    result = watch(history, report, snapshot, 90.0, 7)

    assert (result.reply, result.transcript) == (["Filed DNA-42."], [6, 7])


def test_a_reply_that_landed_before_a_follow_up_is_still_found():
    """The callback came between the acknowledgement and a follow-up the reader sent."""
    report, history = _waiting(acknowledged_at=4)
    history.append({"role": "assistant", "answer": ["2 wins."], "notes": [], "error": None, "session_id": "s1",
                    "transcript": [5, 7]})
    snapshot = _snapshot(["a", "b", "c", "Thanks.", "Filed DNA-42.", "Checking…", "2 wins."])

    result = watch(history, report, snapshot, 90.0, 7)

    assert (result.reply, result.transcript) == (["Filed DNA-42."], [4, 5])


def test_the_watcher_gives_up_after_a_while():
    report, history = _waiting(acknowledged_at=4, sent_at=100.0)

    assert watch(history, report, _snapshot(["a", "b", "c", "Thanks."]), 100.0 + WATCH_SECONDS, 4).state == "expired"


def test_a_callback_inside_a_follow_up_turn_is_pulled_out_of_it():
    messages = [
        AgentMessage(1, "agent", "Checking the race results."),
        AgentMessage(2, "agent", "Filed [DNA-42](https://linear.app/acme/issue/DNA-42), assigned to Ana."),
        AgentMessage(3, "agent", "Ferrari won 1 race in 2023."),
    ]

    assert pull_issue_reply(messages) is messages[1]
    assert pull_issue_reply(messages[:1]) is None


def test_a_reply_that_is_already_there_wins_over_the_deadline():
    """Someone opens the thread long after the report: the first poll must not give up."""
    report, history = _waiting(acknowledged_at=4, sent_at=100.0)
    snapshot = _snapshot(["a", "b", "c", "Thanks.", "Filed DNA-42."])
    late = 100.0 + 2 * WATCH_SECONDS

    assert watch(history, report, snapshot, late, None).state == "waiting"
    assert watch(history, report, snapshot, late + 3, 5).state == "done"


def test_a_link_to_the_readers_own_computer_stays_out_of_the_brief():
    message = build_feedback_message(_report(conversation_url="http://localhost:8765/?thread=s1"))

    assert "localhost" not in message
    assert "the Linear team key \"DNA\", assignee_email \"ana@example.com\", the DinoAI session id s1." in message
