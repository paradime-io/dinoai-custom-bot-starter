"""The contract between the Streamlit frontend and the DinoAI agent.

The agent API only returns Markdown messages, so visuals travel inside the
message as fenced ```viz blocks holding one JSON object each. This module
owns both sides of that contract: the instructions sent to the agent and the
parser that turns its reply back into text and visual segments. Keep them in
sync and bump PROTOCOL_VERSION when either changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Union

PROTOCOL_VERSION = 1

VIZ_TYPES = {"bar", "line", "area", "scatter", "pie", "heatmap", "table", "metrics"}

RESPONSE_FORMAT = """\
<response_format app="streamlit-analyst" version="1">
You are answering a business stakeholder inside a chat app. The app renders your final message as Markdown, and renders every fenced code block tagged `viz` as an interactive chart, table or KPI row.

How to work
- Answer from data: find the right dbt models (prefer documented marts), then query them with run_sql_query. Never estimate or invent numbers.
- Every number in your prose must appear in a query result. Do counts, sums and differences in SQL, not in your head.
- Stay read-only: do not edit files, open pull requests, run dbt or change the warehouse.
- Keep results small: aggregate in SQL, at most 60 rows per visual, and the smallest result that makes the point (for example the top 5, not the top 10, on a line chart). Every row you write out costs the reader waiting time.
- If the question is ambiguous, choose the most reasonable reading, state it in one line, and answer. Do not stop to ask.
- Narrate your work: before each tool call, post one short line (12 words at most) that says what you are about to do, for example "Searching the catalog for race results" or "Querying wins per season since 2014". The app shows these lines live while the reader waits. Put no numbers or findings in them; those belong in the final message.

Final message: post it as ONE message, written for a non-technical reader.
1. Start with the direct answer in one or two sentences (the number or the finding).
2. Add 1 or 2 ```viz blocks that support it.
3. Add 2-4 short bullets with the insights that matter.
4. If you made assumptions or used definitions, end with one line that starts with "Assumptions:".
Do not mention tools, file names, model names or SQL in the prose. The app shows the SQL of each visual in its own tab.
Before you post, check every comparison and number in your sentences and captions against the rows (who is higher, by how much, which year). Fix anything the rows do not support.

A ```viz block holds exactly one strict JSON object (no comments, no trailing commas). Example:
```viz
{"type": "bar", "title": "Race wins by constructor, 2014-2023", "caption": "Mercedes won more races than everyone else combined", "x": "constructor", "y": "wins", "horizontal": true, "format": {"wins": ",.0f"}, "sql": "select constructor, count(*) as wins from ... group by 1", "columns": ["constructor", "wins"], "rows": [["Mercedes", 111], ["Red Bull", 64]]}
```

Fields
- type: one of bar, line, area, scatter, pie, heatmap, table, metrics.
- title: short, sentence case. caption (optional): the one-line takeaway.
- x: the category or time column. y: one measure column, or a list of measure columns that share a unit.
- color (optional): a column that splits the data into series. At most 6 series (3 for scatter).
- value (heatmap only): the measure column. x and y are the two category columns.
- label (optional, scatter): the column that names each point.
- format (optional): a d3-format string per column, for example ",.0f", "$,.2f" or ".1%".
- sql: the exact query you ran, with fully qualified table names, so that running it returns exactly these rows. If the chart needs extra rows (for example zero values for missing periods), produce them in the SQL instead of adding them by hand.
- columns and rows: the query result, copied exactly. "columns" lists the column names; "rows" holds one array per row, values in the same order. Numbers are JSON numbers (not "1,234" or "$5"), dates are "YYYY-MM-DD", missing values are null.

Pick the type by the job
- line: a trend over time. area: one cumulative series over time.
- bar: compare categories, sorted by value. Set "horizontal": true for long names or more than 8 bars. With color, "stack": true shows part-to-whole and "stack": false compares series side by side.
- scatter: the relationship between two measures.
- heatmap: one measure across two categories.
- pie: share of a whole, with 6 slices or fewer.
- table: rows the reader will scan or look up, in the order of "columns".
- metrics: headline numbers, for example {"type": "metrics", "items": [{"label": "Races in 2023", "value": 22, "format": "localized", "delta": "+1 vs 2022"}]}. Keep each label to 4 words or fewer. format is one of plain, localized, compact, dollar, euro or percent (percent expects a fraction: 0.12 shows as 12%).
Never put measures with different units on one chart (for example points and wins). Use a scatter with one measure per axis, or two viz blocks.
</response_format>"""

FOLLOW_UP_REMINDER = (
    "(Answer in the same response format as before: a short line before each tool call, "
    "then one final message in Markdown with ```viz JSON blocks.)"
)


EARLIER_TURNS = 4
EARLIER_ANSWER_CHARS = 600


def build_first_message(
    question: str, context: str | None = None, earlier: list[tuple[str, str]] | None = None
) -> str:
    """The opening message of a session: the response contract, then the question.

    ``earlier`` holds (question, answer text) pairs from a conversation that
    has to continue in a new session, for example after the old one expired.
    """
    parts = [RESPONSE_FORMAT]
    if context and context.strip():
        parts.append(f"<business_context>\n{context.strip()}\n</business_context>")
    if earlier:
        turns = [
            f"Q: {q.strip()}\nA: {a.strip()[:EARLIER_ANSWER_CHARS]}"
            for q, a in earlier[-EARLIER_TURNS:]
        ]
        parts.append("<conversation_so_far>\n" + "\n\n".join(turns) + "\n</conversation_so_far>")
    parts.append(f"<question>\n{question.strip()}\n</question>")
    return "\n\n".join(parts)


def build_follow_up_message(question: str) -> str:
    return f"{question.strip()}\n\n{FOLLOW_UP_REMINDER}"


@dataclass
class TextSegment:
    markdown: str


@dataclass
class VizSegment:
    spec: dict[str, Any]


@dataclass
class VizError:
    raw: str
    error: str


Segment = Union[TextSegment, VizSegment, VizError]

_FENCE_OPEN = re.compile(r"^\s*```\s*([A-Za-z0-9_-]*)[^`]*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")
_VIZ_TAGS = {"viz", "paradime-viz"}
_VEGA_TAGS = {"vega-lite", "vegalite", "vega_lite"}


@dataclass
class _Fence:
    tag: str
    body: list[str] = field(default_factory=list)


def parse_message(content: str) -> list[Segment]:
    """Split an agent message into Markdown text and visual segments.

    Fenced blocks tagged ``viz`` (or ``vega-lite``) become visuals. A ``json``
    block becomes a visual only when it is clearly a viz spec, so ordinary JSON
    in an answer stays as code. Anything that fails to parse is returned as a
    VizError so the UI can show it instead of dropping it.
    """
    segments: list[Segment] = []
    text: list[str] = []
    fence: _Fence | None = None
    fence_lines: list[str] = []

    def flush_text() -> None:
        chunk = "\n".join(text).strip("\n")
        if chunk.strip():
            segments.append(TextSegment(chunk))
        text.clear()

    for line in content.splitlines():
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened:
                fence = _Fence(tag=opened.group(1).lower())
                fence_lines = [line]
            else:
                text.append(line)
            continue

        fence_lines.append(line)
        if not _FENCE_CLOSE.match(line):
            fence.body.append(line)
            continue

        segment = _fence_to_segment(fence)
        if segment is None:
            text.extend(fence_lines)
        else:
            flush_text()
            segments.append(segment)
        fence = None

    if fence is not None:
        # Unclosed fence: keep it as text rather than guessing where it ends.
        text.extend(fence_lines)
    flush_text()
    return segments


def plain_text(content: str) -> str:
    """The prose of a message, without its visuals."""
    return "\n\n".join(s.markdown for s in parse_message(content) if isinstance(s, TextSegment))


def _fence_to_segment(fence: _Fence) -> Segment | None:
    raw = "\n".join(fence.body).strip()
    if fence.tag in _VIZ_TAGS:
        return _parse_viz(raw)
    if fence.tag in _VEGA_TAGS:
        try:
            spec = loads_lenient(raw)
        except ValueError as exc:
            return VizError(raw, f"Invalid Vega-Lite JSON: {exc}")
        return VizSegment({"type": "vega-lite", "spec": spec})
    if fence.tag in {"json", ""} and raw.startswith("{"):
        try:
            obj = loads_lenient(raw)
        except ValueError:
            return None
        if _looks_like_viz(obj):
            return _validate(obj, raw)
    return None


def _parse_viz(raw: str) -> Segment:
    try:
        obj = loads_lenient(raw)
    except ValueError as exc:
        return VizError(raw, f"Invalid JSON: {exc}")
    return _validate(obj, raw)


def _looks_like_viz(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and str(obj.get("type", "")).lower() in VIZ_TYPES
        and ("data" in obj or "rows" in obj or "items" in obj)
    )


def _rows_to_records(obj: dict[str, Any]) -> list[dict[str, Any]] | str:
    """Expand the compact {"columns": [...], "rows": [[...]]} form into row objects.

    Returns an error message instead when a row does not match the columns.
    """
    columns, rows = obj.get("columns"), obj.get("rows")
    if not isinstance(columns, list) or not columns:
        return "Compact rows need a non-empty 'columns' list."
    if not isinstance(rows, list):
        return "The 'rows' field must be a list of arrays."
    for i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            return f"Row {i + 1} has {len(row) if isinstance(row, list) else 0} values for {len(columns)} columns."
    return [dict(zip(columns, row)) for row in rows]


def _validate(obj: Any, raw: str) -> Segment:
    if not isinstance(obj, dict):
        return VizError(raw, "A viz block must hold one JSON object.")
    kind = str(obj.get("type", "")).lower().strip()
    if kind in {"column", "barh"}:
        kind = "bar"
    elif kind in {"donut", "doughnut"}:
        kind = "pie"
    elif kind in {"kpi", "kpis", "metric"}:
        kind = "metrics"
    elif kind in {"vega-lite", "vegalite"} and isinstance(obj.get("spec"), dict):
        return VizSegment({**obj, "type": "vega-lite"})
    if kind not in VIZ_TYPES:
        return VizError(raw, f"Unknown viz type {obj.get('type')!r}.")
    obj = {**obj, "type": kind}

    if kind == "metrics":
        items = obj.get("items")
        if not isinstance(items, list) or not items:
            return VizError(raw, "A metrics block needs a non-empty 'items' list.")
        return VizSegment(obj)

    if "data" not in obj and "rows" in obj:
        records = _rows_to_records(obj)
        if isinstance(records, str):
            return VizError(raw, records)
        obj = {**obj, "data": records}
    data = obj.get("data")
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) for r in data):
        return VizError(raw, "The visual has no rows.")
    return VizSegment(obj)


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_BARE_NON_FINITE = re.compile(r"(?<![\"\w])(NaN|-?Infinity)(?![\"\w])")


def loads_lenient(raw: str) -> Any:
    """json.loads that forgives the usual LLM slips.

    Accepts raw newlines inside strings (common in multi-line SQL), trailing
    commas, and bare NaN/Infinity. Raises ValueError when it still fails.
    """
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        pass
    repaired = _BARE_NON_FINITE.sub("null", _TRAILING_COMMA.sub(r"\1", raw))
    try:
        return json.loads(repaired, strict=False)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{exc.msg} (line {exc.lineno}, column {exc.colno})") from exc


_CODE_SPANS = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def escape_dollars(markdown: str) -> str:
    """Escape $ outside code so Streamlit does not render "$1.2M to $3M" as LaTeX."""
    parts = _CODE_SPANS.split(markdown)
    return "".join(
        part if i % 2 else re.sub(r"(?<!\\)\$", r"\\$", part) for i, part in enumerate(parts)
    )
