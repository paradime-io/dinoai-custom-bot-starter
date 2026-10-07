from datetime import datetime, timezone

from analyst.export import MAX_TABLE_ROWS, conversation_markdown, export_file_name

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CHART = (
    'Mercedes won the most.\n\n```viz\n'
    '{"type": "bar", "title": "Wins", "caption": "Mercedes leads", "x": "team", "y": "wins",'
    ' "sql": "select team, wins from t", "columns": ["team", "wins"],'
    ' "rows": [["Mercedes", 116.0], ["Red|Bull", 75]]}\n```\n\n- Mercedes peaked in 2016.'
)
KPIS = '```viz\n{"type": "metrics", "items": [{"label": "Win rate", "value": 0.5073, "format": "percent", "delta": "+2 pts"}]}\n```'
HISTORY = [
    {"role": "user", "content": "Who has won the most races?"},
    {"role": "assistant", "answer": [CHART], "notes": [], "error": None},
    {"role": "user", "content": "And the pole conversion?"},
    {"role": "assistant", "answer": [KPIS], "notes": [], "error": None},
    {"role": "user", "content": "And in 2025?"},
    {"role": "assistant", "answer": [], "notes": [], "error": "The agent run failed."},
]


def test_a_conversation_exports_as_checkable_markdown():
    text = conversation_markdown(HISTORY, "s1", now=NOW)

    assert text.startswith("# Who has won the most races?\n")
    assert "Exported on 2026-10-07 12:00 UTC from DinoAI session `s1`" in text
    assert "**Question:** Who has won the most races?" in text
    assert "Mercedes won the most." in text and "- Mercedes peaked in 2016." in text
    assert "**Wins**\n_Mercedes leads_" in text
    assert "| Team | Wins |" in text
    assert "| Mercedes | 116 |" in text
    assert "| Red\\|Bull | 75 |" in text
    assert "```sql\nselect team, wins from t\n```" in text
    assert "- Win rate: **50.7%** (+2 pts)" in text
    assert "> The agent did not finish: The agent run failed." in text
    assert "```viz" not in text


def test_long_tables_are_cut_with_a_note():
    rows = ", ".join(f'["t{i}", {i}]' for i in range(MAX_TABLE_ROWS + 5))
    answer = f'```viz\n{{"type": "table", "columns": ["team", "wins"], "rows": [{rows}]}}\n```'
    text = conversation_markdown([{"role": "assistant", "answer": [answer], "notes": [], "error": None}], now=NOW)

    assert f"| t{MAX_TABLE_ROWS - 1} |" in text and f"| t{MAX_TABLE_ROWS} |" not in text
    assert "_5 more rows are in the app._" in text


def test_the_file_name_comes_from_the_first_question():
    assert export_file_name(HISTORY) == "who-has-won-the-most-races.md"
    assert export_file_name([]) == "untitled-conversation.md"


def test_a_report_exports_with_its_issue():
    history = [
        {"role": "user", "content": "Who won in 2017?"},
        {"role": "report", "category": "Wrong number", "comment": "It was 363 points.", "about": 1,
         "question": "Who won in 2017?", "sent_at": 1.0, "status": "filed",
         "issue": {"key": "DNA-42", "url": "https://linear.app/a/issue/DNA-42"}},
    ]

    markdown = conversation_markdown(history, now=datetime(2026, 10, 7, tzinfo=timezone.utc))

    assert "> **Reported a problem: Wrong number.** It was 363 points. Filed as [DNA-42](https://linear.app/a/issue/DNA-42)." in markdown
