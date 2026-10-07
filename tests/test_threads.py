from analyst.protocol import build_first_message
from analyst.threads import ThreadStore, conversation_title, earlier_turns
from analyst.turns import PendingTurn

CHART = '```viz\n{"type": "table", "data": [{"a": 1}]}\n```'
HISTORY = [
    {"role": "user", "content": "Who won in 2017?"},
    {"role": "assistant", "answer": [f"Lewis Hamilton, with 363 points.\n\n{CHART}"], "notes": [],
     "error": None, "seconds": 43.0, "session_id": "xs6dv8kf0si346nx"},
    {"role": "notice", "content": "Reopened."},
    {"role": "user", "content": "And the best constructor?"},
]


def test_a_thread_survives_a_restart_with_its_pending_turn(tmp_path):
    turn = PendingTurn("xs6dv8kf0si346nx", "And the best constructor?", baseline=1, sent_at=100.0,
                       first_turn=False, saw_active=True, message_count=0, last_growth_at=100.0)
    ThreadStore(tmp_path).save("xs6dv8kf0si346nx", HISTORY, turn)

    history, pending = ThreadStore(tmp_path).load("xs6dv8kf0si346nx")
    assert history == HISTORY
    assert pending == turn


def test_a_finished_thread_has_no_pending_turn(tmp_path):
    ThreadStore(tmp_path).save("s1", HISTORY[:2], None)
    assert ThreadStore(tmp_path).load("s1") == (HISTORY[:2], None)


def test_unknown_unsafe_and_corrupt_threads_load_as_none(tmp_path):
    store = ThreadStore(tmp_path)
    store.save("../escape", HISTORY, None)
    assert not list(tmp_path.parent.glob("escape*"))
    assert store.load("../escape") is None
    assert store.load("missing") is None
    (tmp_path / "broken.json").write_text("{not json")
    assert store.load("broken") is None


def test_earlier_turns_pair_questions_with_answer_prose():
    pairs = earlier_turns(HISTORY)
    assert pairs == [("Who won in 2017?", "Lewis Hamilton, with 363 points.")]


def test_a_new_session_is_briefed_on_the_conversation_so_far():
    message = build_first_message("And the best constructor?", earlier=earlier_turns(HISTORY))
    assert "<conversation_so_far>\nQ: Who won in 2017?\nA: Lewis Hamilton, with 363 points.\n</conversation_so_far>" in message
    assert message.endswith("<question>\nAnd the best constructor?\n</question>")
    assert "```viz" not in message.split("<conversation_so_far>")[1]


def test_the_history_lists_threads_newest_first_with_their_stats(tmp_path, monkeypatch):
    store = ThreadStore(tmp_path)
    clock = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr("analyst.threads.time.time", lambda: next(clock))

    rated = [
        {"role": "user", "content": "Who won in 2017?"},
        {"role": "assistant", "answer": ["Hamilton."], "notes": [], "error": None, "rating": 1, "session_id": "a"},
        {"role": "user", "content": "And 2018?"},
        {"role": "assistant", "answer": [], "notes": [], "error": "The agent run failed.", "session_id": "a"},
    ]
    store.save("a", rated, None, owner="ana@example.com")
    store.save("b", [{"role": "user", "content": "Pole wins?"}], PendingTurn("b", "Pole wins?", 0, 0.0, True))
    store.save("a", rated + [{"role": "user", "content": "And 2019?"}], None, owner="ben@example.com")

    first, second = store.list()
    assert (first.session_id, second.session_id) == ("a", "b")  # a was active last
    assert first.title == "Who won in 2017?"
    assert (first.questions, first.answered, first.failed) == (3, 1, 1)
    assert (first.thumbs_up, first.thumbs_down) == (1, 0)
    assert first.created_at == 100.0 and first.updated_at == 300.0
    assert first.owner == "ana@example.com"  # whoever asked first keeps the thread
    assert second.pending and not first.pending


def test_titles_are_the_first_question_on_one_line():
    long_question = "Which constructors have won the most races since 2014, and how did wins per season evolve for the top three teams?"
    assert conversation_title([{"role": "user", "content": "  Who won\nin 2017?  "}]) == "Who won in 2017?"
    assert conversation_title([{"role": "user", "content": long_question}]).endswith(" …")
    assert conversation_title([]) == "Untitled conversation"


def test_threads_saved_before_titles_were_recorded_still_list(tmp_path):
    (tmp_path / "old.json").write_text('{"session_id": "old", "history": [{"role": "user", "content": "Hi"}], "pending": null}')
    (summary,) = ThreadStore(tmp_path).list()
    assert summary.title == "Hi" and summary.updated_at > 0


def test_reports_count_on_their_own_and_their_replies_are_not_answers(tmp_path):
    history = HISTORY[:2] + [
        {"role": "report", "category": "Wrong number", "comment": "", "about": 1, "question": "Who won in 2017?",
         "sent_at": 1.0, "status": "filed", "issue": {"key": "DNA-42", "url": "https://linear.app/a/issue/DNA-42"}},
        {"role": "assistant", "answer": ["Thanks, the data team has it."], "notes": [], "error": None,
         "session_id": "s1", "kind": "ack"},
        {"role": "assistant", "answer": ["Filed DNA-42."], "notes": [], "error": None, "session_id": "s1",
         "kind": "issue"},
        {"role": "user", "content": "And 2018?"},
    ]
    ThreadStore(tmp_path).save("s1", history, None)

    (summary,) = ThreadStore(tmp_path).list()
    assert (summary.questions, summary.answered, summary.reports, summary.issues) == (2, 1, 1, ("DNA-42",))
    # A new session's brief pairs questions with answers only.
    assert earlier_turns(history) == [("Who won in 2017?", "Lewis Hamilton, with 363 points.")]
