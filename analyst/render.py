"""Streamlit rendering of agent answers: Markdown, charts, tables and KPI tiles."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
import streamlit as st

from analyst.charts import DARK, LIGHT, ChartError, ChartTheme, ColorRegistry, build_chart, frame
from analyst.protocol import TextSegment, VizError, VizSegment, escape_dollars, parse_message, plain_text

METRIC_FORMATS = {"plain", "localized", "percent", "dollar", "euro", "yen", "accounting", "compact"}
METRICS_PER_ROW = 3


def chart_theme() -> ChartTheme:
    return DARK if st.context.theme.type == "dark" else LIGHT


def color_registry() -> ColorRegistry:
    if "color_registry" not in st.session_state:
        st.session_state.color_registry = ColorRegistry()
    return st.session_state.color_registry


def render_answer(contents: list[str], key: str) -> None:
    for i, content in enumerate(contents):
        for j, segment in enumerate(parse_message(content)):
            segment_key = f"{key}-{i}-{j}"
            if isinstance(segment, TextSegment):
                st.markdown(escape_dollars(segment.markdown))
            elif isinstance(segment, VizSegment):
                render_viz(segment.spec, segment_key)
            else:
                render_viz_error(segment, segment_key)


def preview(content: str, limit: int = 600) -> str:
    """A short, chart-free version of a message for the live activity log."""
    parts = []
    for segment in parse_message(content):
        if isinstance(segment, TextSegment):
            parts.append(segment.markdown)
        elif isinstance(segment, VizSegment):
            title = segment.spec.get("title") or segment.spec.get("type", "visual")
            parts.append(f"*Visual: {title}*")
    text = "\n\n".join(parts).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return escape_dollars(text)


def headline(content: str, limit: int = 80) -> str:
    """The first line of a message's prose, for a one-line status label."""
    lines = [line.strip(" #*_>`-") for line in plain_text(content).splitlines()]
    first = next((line for line in lines if line), "DinoAI is working")
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + " …"


def render_viz(spec: dict[str, Any], key: str) -> None:
    kind = spec["type"]
    with st.container(border=True):
        if spec.get("title"):
            st.markdown(f"**{escape_dollars(str(spec['title']))}**")
        if spec.get("caption"):
            st.caption(escape_dollars(str(spec["caption"])))
        if kind == "metrics":
            _metrics(spec)
            _sql_expander(spec)
        elif kind == "table":
            df, notes = frame(spec)
            _table(df, spec, key)
            _notes(notes)
            _sql_expander(spec)
        elif kind == "vega-lite":
            _vega_lite(spec, key)
        else:
            _chart(spec, key)


def render_viz_error(segment: VizError, key: str) -> None:
    st.warning(f"The agent sent a visual this app could not read. {segment.error}", icon=":material/warning:")
    with st.expander("Show what the agent sent"):
        st.code(segment.raw, language="json", wrap_lines=True)


def render_notes(notes: list[str], steps: list[str] | None = None) -> None:
    """The agent's work behind an answer: its tool calls (when the API has them) and notes."""
    steps = steps or []
    if not notes and not steps:
        return
    count, noun = (len(steps), "step") if steps else (len(notes), "update")
    label = f"Agent activity · {count} {noun}{'s' if count != 1 else ''}"
    with st.expander(label, icon=":material/manage_search:"):
        for line in steps:
            st.markdown(f"- {escape_dollars(line)}")
        for note in notes:
            st.markdown(preview(note, limit=4000))


def _chart(spec: dict[str, Any], key: str) -> None:
    chart_tab, data_tab, sql_tab = st.tabs(["Chart", "Data", "SQL"])
    df, _ = frame(spec)
    with chart_tab:
        try:
            built = build_chart(spec, chart_theme(), color_registry())
        except ChartError as exc:
            st.info(f"Showing a table because the chart could not be drawn: {exc}", icon=":material/table:")
            _table(df, spec, f"{key}-fallback", download=False)
        else:
            for n, chart in enumerate(built.charts):
                st.altair_chart(chart, width="stretch", key=f"{key}-chart-{n}")
            _notes(built.notes)
    with data_tab:
        _table(df, spec, key)
    with sql_tab:
        _sql(spec)


def _vega_lite(spec: dict[str, Any], key: str) -> None:
    vl = dict(spec.get("spec") or {})
    if "data" not in vl and isinstance(spec.get("data"), list):
        vl["data"] = {"values": spec["data"]}
    vl.setdefault("width", "container")
    chart_tab, sql_tab = st.tabs(["Chart", "SQL"])
    with chart_tab:
        st.vega_lite_chart(vl, width="stretch", key=f"{key}-vl")
    with sql_tab:
        _sql(spec)


def _table(df: pd.DataFrame, spec: dict[str, Any], key: str, download: bool = True) -> None:
    columns = spec.get("columns")
    if isinstance(columns, list):
        ordered = [c for c in columns if c in df.columns]
        df = df[ordered + [c for c in df.columns if c not in ordered]]
    formats = spec.get("format") if isinstance(spec.get("format"), dict) else {}
    config = {}
    for name in df.columns:
        label = _label(name)
        if pd.api.types.is_numeric_dtype(df[name]):
            config[name] = st.column_config.NumberColumn(label, format=_streamlit_format(formats.get(name)))
        else:
            config[name] = st.column_config.Column(label)
    st.dataframe(df, hide_index=True, column_config=config, width="stretch", height="auto")
    if download and not df.empty:
        st.download_button(
            "Download CSV",
            df.to_csv(index=False),
            file_name=f"{_slug(spec.get('title') or 'data')}.csv",
            mime="text/csv",
            key=f"{key}-csv",
            on_click="ignore",
            icon=":material/download:",
            type="tertiary",
        )


def _metrics(spec: dict[str, Any]) -> None:
    items = [item for item in spec.get("items", []) if isinstance(item, dict)]
    for start in range(0, len(items), METRICS_PER_ROW):
        row = items[start : start + METRICS_PER_ROW]
        for column, item in zip(st.columns(METRICS_PER_ROW), row):
            with column:
                _metric(item)


def _metric(item: dict[str, Any]) -> None:
    delta = item.get("delta")
    kwargs: dict[str, Any] = {}
    fmt = _metric_format(item.get("format"))
    if fmt:
        kwargs["format"] = fmt
    if isinstance(delta, str) and delta.strip() and delta.strip()[0] not in "+-−":
        # A note such as "95% win rate" is context, not a change: no arrow, no color.
        kwargs.update(delta_arrow="off", delta_color="off")
    elif item.get("delta_color") in {"normal", "inverse", "off"}:
        kwargs["delta_color"] = item["delta_color"]
    if item.get("delta_description"):
        kwargs["delta_description"] = str(item["delta_description"])
    st.metric(
        label=str(item.get("label", "")),
        value=_scalar(item.get("value", "—")),
        delta=delta if delta not in ("", None) else None,
        help=item.get("help"),
        border=True,
        **kwargs,
    )


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (int, float, str)) or value is None else str(value)


def _metric_format(fmt: Any) -> str | None:
    if not isinstance(fmt, str) or not fmt:
        return None
    if fmt in METRIC_FORMATS:
        return fmt
    return _streamlit_format(fmt)


def _streamlit_format(d3: Any) -> str | None:
    """Map the agent's d3 format strings onto Streamlit's number formats."""
    if not isinstance(d3, str) or not d3:
        return None
    if "%" in d3:
        return "percent"
    if d3.startswith("$"):
        return "dollar"
    if d3.rstrip().endswith("s"):
        return "compact"
    if "," in d3:
        return "localized"
    return None


def _sql(spec: dict[str, Any]) -> None:
    sql = spec.get("sql")
    if sql:
        st.code(str(sql), language="sql", wrap_lines=True)
    else:
        st.caption("The agent did not include the query for this visual.")


def _sql_expander(spec: dict[str, Any]) -> None:
    if spec.get("sql"):
        with st.expander("SQL", icon=":material/code:"):
            _sql(spec)


def _notes(notes: list[str]) -> None:
    for note in notes:
        st.caption(note)


def _label(name: Any) -> str:
    text = re.sub(r"[_\s]+", " ", str(name)).strip()
    return text[:1].upper() + text[1:]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "data"


