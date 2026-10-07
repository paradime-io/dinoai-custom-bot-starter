"""A conversation as Markdown, to share outside the app (Slack, Notion, email).

Charts become tables of the rows behind them, with the SQL that produced them,
so the export stays checkable without the app.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from analyst.protocol import TextSegment, VizSegment, parse_message
from analyst.threads import conversation_title

MAX_TABLE_ROWS = 50


def conversation_markdown(
    history: list[dict[str, Any]], session_id: str | None = None, now: datetime | None = None
) -> str:
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    source = f" from DinoAI session `{session_id}`" if session_id else ""
    lines = [
        f"# {conversation_title(history)}",
        "",
        f"_Exported on {when}{source}. Each table shows the rows behind a chart, with its SQL._",
        "",
    ]
    for item in history:
        role = item.get("role")
        if role == "user":
            lines += ["---", "", f"**Question:** {item['content'].strip()}", ""]
        elif role == "notice":
            lines += [f"> {item['content']}", ""]
        elif role == "report":
            comment = f" {item['comment']}" if item.get("comment") else ""
            issue = item.get("issue")
            filed = f" Filed as [{issue['key']}]({issue['url']})." if issue else ""
            lines += [f"> **Reported a problem: {item['category']}.**{comment}{filed}", ""]
        elif role == "assistant":
            if item.get("error"):
                lines += [f"> The agent did not finish: {item['error']}", ""]
            for content in item.get("answer") or []:
                for segment in parse_message(content):
                    if isinstance(segment, TextSegment):
                        lines += [segment.markdown.strip(), ""]
                    elif isinstance(segment, VizSegment):
                        lines += _visual(segment.spec)
                    else:
                        lines += ["_The agent sent a visual that could not be read._", ""]
    return "\n".join(lines).rstrip() + "\n"


def export_file_name(history: list[dict[str, Any]]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", conversation_title(history).lower()).strip("-")
    return f"{slug[:60].rstrip('-') or 'conversation'}.md"


def _visual(spec: dict[str, Any]) -> list[str]:
    lines = []
    if spec.get("title"):
        lines.append(f"**{spec['title']}**")
    if spec.get("caption"):
        lines.append(f"_{spec['caption']}_")
    if lines:
        lines.append("")
    if spec.get("type") == "metrics":
        for item in spec.get("items") or []:
            delta = f" ({item['delta']})" if item.get("delta") else ""
            lines.append(f"- {item.get('label', '')}: **{_metric_value(item)}**{delta}")
        lines.append("")
    else:
        lines += _table(spec.get("data") or [], spec.get("columns"))
    if spec.get("sql"):
        lines += ["```sql", str(spec["sql"]).strip(), "```", ""]
    return lines


def _table(rows: list[dict[str, Any]], columns: Any = None) -> list[str]:
    if not rows:
        return []
    keys = list(dict.fromkeys(key for row in rows for key in row))
    if isinstance(columns, list):
        keys = [c for c in columns if c in keys] + [k for k in keys if k not in columns]
    lines = [
        "| " + " | ".join(_cell(_label(k)) for k in keys) + " |",
        "|" + " --- |" * len(keys),
    ]
    for row in rows[:MAX_TABLE_ROWS]:
        lines.append("| " + " | ".join(_cell(row.get(k)) for k in keys) + " |")
    if len(rows) > MAX_TABLE_ROWS:
        lines += ["", f"_{len(rows) - MAX_TABLE_ROWS} more rows are in the app._"]
    return lines + [""]


def _metric_value(item: dict[str, Any]) -> str:
    value, fmt = item.get("value"), item.get("format")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    if fmt == "percent":
        return f"{value * 100:.1f}%"
    if fmt == "dollar":
        return f"${value:,.2f}"
    if fmt == "euro":
        return f"€{value:,.2f}"
    if fmt == "plain":
        return str(value)
    return f"{value:,}"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _label(name: str) -> str:
    text = re.sub(r"[_\s]+", " ", str(name)).strip()
    return text[:1].upper() + text[1:]
