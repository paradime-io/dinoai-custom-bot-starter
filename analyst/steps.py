"""One-line progress updates from the agent's transcript steps.

APIs with `dinoaiAgentRun.steps` report each tool call as it finishes. This
turns a step into the kind of line the session panel in Paradime shows, for
example "Ran a SQL query: SELECT race_year, ...", and hides the plumbing.
"""

from __future__ import annotations

import re

from analyst.backend import AgentStep

_TOOL_LABELS = {
    "run_sql_query": "Ran a SQL query",
    "search_catalog": "Searched the data catalog",
    "get_all_models": "Listed the dbt models",
    "get_mart_models": "Listed the mart models",
    "get_node_details": "Read a model's details",
    "get_lineage": "Traced the lineage",
    "get_column_level_lineage": "Traced a column's lineage",
    "get_model_health": "Checked the model's health",
    "get_model_performance": "Checked the model's run times",
    "read_file": "Read a file",
    "ripgrep_search": "Searched the project",
    "search_files_and_directories": "Browsed the project",
    "todo_write": "Planned the steps",
    "load_skill_instructions": "Loaded a skill",
    "run_subagent": "Ran a sub-task",
    "invoke_agent": "Handed the work to another agent",
    "check_delegated_agents": "Checked on the other agent",
}
# Delivery and bookkeeping calls, not work the reader cares about.
_HIDDEN_TOOLS = frozenset({"send_api_message", "post_slack_message", "todo_read"})
# The argument that says what a call was about, in order of preference.
_DETAIL_KEYS = (
    "query", "sql", "search_query", "pattern", "path", "file_path", "unique_id", "model_name", "task", "agent",
)
# Inputs arrive cut at maxChars, so read the value with a regex, not json.loads.
_DETAIL = re.compile(r'"(%s)"\s*:\s*"((?:[^"\\]|\\.)*)' % "|".join(_DETAIL_KEYS))
_MAX_DETAIL = 90
_MAX_AGENT_NOTE = 200


def describe(step: AgentStep) -> str | None:
    """A progress line for the step, or None for a step not worth showing."""
    if step.role == "tool":
        name = step.tool_name or ""
        if name in _HIDDEN_TOOLS:
            return None
        label = _TOOL_LABELS.get(name) or (name.replace("_", " ").capitalize() or "Used a tool")
        detail = _detail(step.tool_input)
        return f"{label}: {detail}" if detail else label
    if step.role == "agent":
        # Short commentary between tool calls; the answer itself is shown in full later.
        text = (step.content or "").strip()
        if text and "```" not in text and len(text) <= _MAX_AGENT_NOTE:
            return text.splitlines()[0]
    return None


def _detail(tool_input: str | None) -> str | None:
    if not tool_input:
        return None
    values = {key: value for key, value in _DETAIL.findall(tool_input)}
    for key in _DETAIL_KEYS:
        if values.get(key):
            text = " ".join(values[key].replace("\\n", " ").replace('\\"', '"').split())
            return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL].rstrip() + " …"
    return None
