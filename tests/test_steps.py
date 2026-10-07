from analyst.backend import AgentStep, RunSnapshot
from analyst.steps import describe
from analyst.turns import PendingTurn, turn_steps


def tool(name, tool_input=None):
    return AgentStep(index=0, role="tool", tool_name=name, tool_input=tool_input)


def test_a_sql_call_shows_its_query_even_when_the_input_was_cut():
    cut = '{\n  "query": "SELECT race_year,\\n       COUNT(*) AS wins FROM `dbt-demo-project`.`dbt_prod`.`fct_f1__race_result` WHERE pos'
    line = describe(tool("run_sql_query", cut))
    assert line.startswith("Ran a SQL query: SELECT race_year, COUNT(*) AS wins FROM")
    assert line.endswith(" …") and "\\n" not in line


def test_known_unknown_and_hidden_tools():
    assert describe(tool("search_catalog", '{"query": "race results"}')) == "Searched the data catalog: race results"
    assert describe(tool("get_mart_models")) == "Listed the mart models"
    assert describe(tool("list_all_bigquery_projects_and_datasets")) == "List all bigquery projects and datasets"
    assert describe(tool("send_api_message", '{"message": "the answer"}')) is None


def test_agent_commentary_shows_but_the_answer_does_not():
    note = AgentStep(index=0, role="agent", content="Checking which mart holds race results.")
    answer = AgentStep(index=1, role="agent", content='Mercedes won.\n\n```viz\n{"type": "bar"}\n```')
    assert describe(note) == "Checking which mart holds race results."
    assert describe(answer) is None
    assert describe(AgentStep(index=2, role="user", content="question")) is None


def test_a_turn_only_sees_steps_after_its_question():
    turn = PendingTurn("s1", "q2", baseline=1, sent_at=0.0, first_turn=False, step_baseline=3)
    run = RunSnapshot("RUNNING", [], steps=[AgentStep(i, r) for i, r in enumerate(["user", "tool", "agent", "tool", "user", "tool"])])
    assert [s.index for s in turn_steps(turn, run)] == [5]
