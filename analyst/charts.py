"""Build Altair charts from the agent's viz specs.

The agent chooses the form and the data. This module owns the look, so every
chart in the app reads as one system: a validated palette, thin marks with
rounded data-ends, chrome left to the Streamlit theme, and a hover layer on
every chart. Specs are untrusted input, so field names are checked against the
data and malformed formats are dropped instead of breaking the render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import altair as alt
import pandas as pd

MAX_ROWS = 500
MAX_HUES = 8
MAX_SCATTER_HUES = 3
MAX_PIE_SLICES = 6
MAX_LABELLED_BARS = 20
BAR_STEP = 28
BAR_THICKNESS = 18
CHART_HEIGHT = 300
ESTIMATED_WIDTH = 600
DEFAULT_FORMAT = ",~f"
OTHER = "Other"


class ChartError(ValueError):
    """The spec cannot be drawn as asked. The message is shown to the reader."""


@dataclass(frozen=True)
class ChartTheme:
    mode: str
    surface: str
    ink: str
    muted: str
    categorical: tuple[str, ...]
    sequential: tuple[str, ...]


# The dataviz reference palette led by the Paradime violet (#635ae0 in light
# mode, one step darker than the app's violet-600 #726ae3; violet-500 #827be6
# in dark), with blue moved to the slot the old purple held.
# Validated with the dataviz skill's validate_palette.js against Streamlit's own
# light (#ffffff) and dark (#0e1117) backgrounds: every check passes. Light-mode
# green, yellow and pink sit below 3:1 contrast, which is why every chart also
# has a Data tab. The sequential ramps are on the violet hue, evenly spaced in
# OKLab lightness; their low ends clear the background at 2:1 or more.
LIGHT = ChartTheme(
    mode="light",
    surface="#ffffff",
    ink="#52514e",
    muted="#898781",
    categorical=(
        "#635ae0", "#eb6834", "#1baf7a", "#eda100",
        "#e87ba4", "#008300", "#2a78d6", "#e34948",
    ),
    sequential=("#9898f8", "#837fec", "#6e66de", "#5c54bc", "#4a439b", "#39337c", "#28235d"),
)
DARK = ChartTheme(
    mode="dark",
    surface="#0e1117",
    ink="#c3c2b7",
    muted="#898781",
    categorical=(
        "#827be6", "#d95926", "#199e70", "#c98500",
        "#d55181", "#008300", "#3987e5", "#e66767",
    ),
    # Low values recede into the dark background; high values come forward.
    sequential=("#453f92", "#5a53b9", "#7068e0", "#8986ef", "#a3a4fe", "#c1c3fe", "#e0e2ff"),
)


@dataclass
class ColorRegistry:
    """Gives each entity a palette slot the first time it is charted.

    "Red Bull" then keeps its color in every chart of the conversation, and a
    chart that drops a series never repaints the others.
    """

    slots: dict[str, int] = field(default_factory=dict)

    def assign(self, names: list[str], size: int) -> list[int]:
        chosen: dict[str, int] = {}
        for name in names:  # entities seen before keep their slot
            slot = self.slots.get(name)
            if slot is not None and slot not in chosen.values():
                chosen[name] = slot
        for name in names:
            if name in chosen:
                continue
            taken = set(chosen.values())
            unowned = [i for i in range(size) if i not in taken and i not in self.slots.values()]
            if unowned:
                chosen[name] = self.slots[name] = unowned[0]
            else:  # every slot has an owner: borrow one for this chart only
                chosen[name] = next(i for i in range(size) if i not in taken)
        return [chosen[n] for n in names]


@dataclass
class Built:
    charts: list[alt.TopLevelMixin]
    notes: list[str]


@dataclass
class _Ctx:
    spec: dict[str, Any]
    theme: ChartTheme
    registry: ColorRegistry
    notes: list[str] = field(default_factory=list)


def frame(spec: dict[str, Any]) -> tuple[pd.DataFrame, list[str]]:
    """The spec's data as a DataFrame, capped at MAX_ROWS."""
    df = pd.DataFrame.from_records(spec.get("data") or [])
    notes = []
    if len(df) > MAX_ROWS:
        notes.append(f"Showing the first {MAX_ROWS:,} of {len(df):,} rows.")
        df = df.head(MAX_ROWS)
    return df, notes


def build_chart(
    spec: dict[str, Any], theme: ChartTheme = LIGHT, registry: ColorRegistry | None = None
) -> Built:
    builders = {
        "bar": _bar,
        "line": _line,
        "area": _area,
        "scatter": _scatter,
        "pie": _pie,
        "heatmap": _heatmap,
    }
    kind = spec.get("type")
    if kind not in builders:
        raise ChartError(f"'{kind}' is not a chart type.")
    df, notes = frame(spec)
    ctx = _Ctx(spec, theme, registry if registry is not None else ColorRegistry(), notes)
    measures = _mixed_units(df, spec)
    if not measures:
        return Built([builders[kind](df, ctx)], ctx.notes)
    # One axis per chart: measures of different scale get a panel each,
    # sharing the category order of the first measure.
    ctx.notes.append("These measures have different scales, so each has its own chart.")
    order = _category_order(df, spec, measures[0])
    charts = []
    for measure in measures:
        panel = _Ctx({**spec, "y": measure, "_order": order}, theme, ctx.registry, ctx.notes)
        title = alt.TitleParams(_title(measure), anchor="start", fontSize=13)
        charts.append(builders[kind](df.copy(), panel).properties(title=title))
    return Built(charts, ctx.notes)


def _mixed_units(df: pd.DataFrame, spec: dict[str, Any]) -> list[str] | None:
    """The measures to split into panels, when a chart mixes scales (e.g. points and wins)."""
    raw = spec.get("y")
    if spec.get("type") not in {"bar", "line", "area"} or spec.get("color") or not isinstance(raw, list):
        return None
    names = [n for n in raw if n in df.columns]
    peaks = [pd.to_numeric(df[n], errors="coerce").abs().max() for n in names]
    peaks = [float(p) for p in peaks if pd.notna(p) and p > 0]
    if len(peaks) < 2:
        return None
    formats = spec.get("format") if isinstance(spec.get("format"), dict) else {}
    distinct_formats = {formats[n] for n in names if formats.get(n)}
    if max(peaks) / min(peaks) > 5 or len(distinct_formats) > 1:
        return [str(n) for n in names]
    return None


def _category_order(df: pd.DataFrame, spec: dict[str, Any], measure: str) -> list[str] | None:
    x = spec.get("x")
    if spec.get("type") != "bar" or x not in df.columns:
        return None
    values = pd.to_numeric(df[measure], errors="coerce")
    ranked = df.assign(_v=values).groupby(df[x].astype(str), sort=False)["_v"].sum()
    return [str(v) for v in ranked.sort_values(ascending=False).index]


# --- Field helpers ----------------------------------------------------------------


def _field(df: pd.DataFrame, spec: dict[str, Any], key: str, required: bool = True) -> str | None:
    name = spec.get(key)
    if isinstance(name, list):
        name = name[0] if name else None
    if not name:
        if required:
            raise ChartError(f"The spec does not name its '{key}' column.")
        return None
    if name not in df.columns:
        if required:
            columns = ", ".join(map(str, df.columns))
            raise ChartError(f"Column '{name}' (from '{key}') is not in the data ({columns}).")
        return None
    return str(name)


def _measures(df: pd.DataFrame, spec: dict[str, Any]) -> list[str]:
    raw = spec.get("y")
    names = [n for n in (raw if isinstance(raw, list) else [raw]) if n]
    if not names:
        raise ChartError("The spec does not name its 'y' column.")
    missing = [n for n in names if n not in df.columns]
    if missing:
        columns = ", ".join(map(str, df.columns))
        raise ChartError(f"Column(s) {missing} from 'y' are not in the data ({columns}).")
    for name in names:
        df[name] = pd.to_numeric(df[name], errors="coerce")
    return [str(n) for n in names]


def _title(name: str) -> str:
    text = re.sub(r"[_\s]+", " ", str(name)).strip()
    return text[:1].upper() + text[1:]


def _escape(name: str) -> str:
    """Vega-Lite reads dots and brackets in field names as paths."""
    return re.sub(r"([.\[\]\\])", r"\\\1", str(name))


def _js_string(name: str) -> str:
    return "'" + str(name).replace("\\", "\\\\").replace("'", "\\'") + "'"


_NUMBER_FORMAT = re.compile(r"^(?:.?[<>=^])?[-+( ]?[$#]?0?\d*,?(?:\.\d+)?~?[efgrs%pbodxXc]?$")


def _number_format(ctx: _Ctx, name: str) -> str:
    formats = ctx.spec.get("format")
    fmt = formats.get(name) if isinstance(formats, dict) else None
    if isinstance(fmt, str) and fmt and _NUMBER_FORMAT.match(fmt):
        return fmt
    return DEFAULT_FORMAT


@dataclass(frozen=True)
class _Axis:
    name: str
    vl_type: str
    time_format: str | None = None
    time_unit: str | None = None


_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def _dimension(df: pd.DataFrame, name: str, *, continuous_time: bool) -> _Axis:
    """Decide how to place a dimension column, coercing years and ISO dates in place."""
    values = df[name].dropna()
    if values.empty:
        return _Axis(name, "nominal")
    if pd.api.types.is_bool_dtype(values):
        df[name] = df[name].astype("string")
        return _Axis(name, "nominal")
    if pd.api.types.is_numeric_dtype(values):
        if (values == values.round()).all() and values.between(1800, 2200).all():
            if continuous_time:
                years = df[name].astype("Int64").astype("string")
                df[name] = pd.to_datetime(years, format="%Y", errors="coerce")
                return _Axis(name, "temporal", "%Y")
            return _Axis(name, "ordinal")
        return _Axis(name, "quantitative")
    if values.astype(str).str.match(_ISO_DATE).all():
        df[name] = pd.to_datetime(df[name], errors="coerce", format="mixed")
        dates = df[name].dropna()
        if (dates.dt.month == 1).all() and (dates.dt.day == 1).all():
            fmt, unit = "%Y", "year"
        elif (dates.dt.day == 1).all():
            fmt, unit = "%b %Y", "yearmonth"
        else:
            fmt, unit = "%b %-d, %Y", "yearmonthdate"
        if continuous_time:
            return _Axis(name, "temporal", fmt)
        return _Axis(name, "ordinal", fmt, unit)
    return _Axis(name, "nominal")


def _position(axis: _Axis, channel: Any = alt.X, axis_props: dict[str, Any] | None = None, **kwargs: Any):
    props: dict[str, Any] = {"labelLimit": 220, "labelOverlap": True, **(axis_props or {})}
    extra: dict[str, Any] = {}
    if axis.time_format:
        props["format"] = axis.time_format
        if axis.vl_type == "ordinal":
            props["formatType"] = "time"
    if axis.time_unit:
        extra["timeUnit"] = axis.time_unit
    kwargs.setdefault("title", None)
    return channel(_escape(axis.name), type=axis.vl_type, axis=alt.Axis(**props), **extra, **kwargs)


def _tooltip(axis: _Axis) -> alt.Tooltip:
    kwargs: dict[str, Any] = {"title": _title(axis.name)}
    if axis.time_format:
        kwargs["format"] = axis.time_format
        if axis.vl_type == "ordinal":
            kwargs["formatType"] = "time"
    if axis.time_unit:
        kwargs["timeUnit"] = axis.time_unit
    return alt.Tooltip(_escape(axis.name), type=axis.vl_type, **kwargs)


def _measure_tooltip(name: str, fmt: str, title: str | None = None) -> alt.Tooltip:
    return alt.Tooltip(_escape(name), type="quantitative", format=fmt, title=title or _title(name))


def _series(
    df: pd.DataFrame, ctx: _Ctx, name: str, max_hues: int, value: str | None = None, keys: list[str] | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """Name the series of a color column, largest first.

    Past ``max_hues`` the tail folds into a gray "Other" that sums the folded
    values per ``keys`` (the right fold for counts and totals, which is what the
    agent is asked to chart).
    """
    df[name] = df[name].astype("string").fillna("(blank)")
    if value:
        totals = df.groupby(name, sort=False)[value].sum(min_count=1).sort_values(ascending=False)
        names = [str(n) for n in totals.index]
    else:
        names = [str(n) for n in pd.unique(df[name])]
    if len(names) <= max_hues:
        return df, names
    keep = names[:max_hues]
    df.loc[~df[name].isin(keep), name] = OTHER
    if value and keys:
        df = df.groupby([*keys, name], as_index=False, sort=False, dropna=False)[value].sum(min_count=1)
    ctx.notes.append(
        f"{len(names) - len(keep)} smaller groups are combined as “{OTHER}”. "
        "The Data tab has every row."
    )
    return df, keep + [OTHER]


def _color(ctx: _Ctx, name: str, names: list[str], legend: dict[str, Any] | None = None) -> alt.Color:
    palette = ctx.theme.categorical
    real = [n for n in names if n != OTHER]
    slots = ctx.registry.assign(real, len(palette))
    colors = {n: palette[s] for n, s in zip(real, slots)}
    colors[OTHER] = ctx.theme.muted
    return alt.Color(
        _escape(name),
        type="nominal",
        scale=alt.Scale(domain=names, range=[colors[n] for n in names]),
        sort=names,
        legend=alt.Legend(**{"orient": "top", "title": None, **(legend or {})}),
    )


def _melt(df: pd.DataFrame, x: str, measures: list[str]) -> tuple[pd.DataFrame, str, str]:
    """Several measures with one unit become one value column split by measure."""
    long = df.melt(id_vars=[x], value_vars=measures, var_name="Measure", value_name="Value")
    long["Measure"] = long["Measure"].map(_title)
    return long, "Value", "Measure"


def _headroom(values: pd.Series, share: float = 0.14) -> alt.Scale:
    """Room past the longest bar for its value label, so the label is never cut."""
    low = float(min(0.0, values.min())) if len(values) else 0.0
    high = float(max(0.0, values.max())) if len(values) else 0.0
    span = high - low
    return alt.Scale(domain=[low, high + span * share if span else 1.0], nice=False)


# --- Bar ------------------------------------------------------------------------


def _bar(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    spec = ctx.spec
    x = _field(df, spec, "x")
    measures = _measures(df, spec)
    color = _field(df, spec, "color", required=False)
    fmt = _number_format(ctx, measures[0])
    y, y_title = measures[0], _title(measures[0])
    if len(measures) > 1 and not color:
        df, y, color = _melt(df, x, measures)
        y_title = None
    elif len(measures) > 1:
        ctx.notes.append(f"Showing {_title(y)} only. The other measures are in the Data tab.")

    axis = _dimension(df, x, continuous_time=False)
    if axis.vl_type == "quantitative":
        df[x] = df[x].astype("string")
        axis = _Axis(x, "nominal")
    labels = df[x].astype(str)
    n_bands = labels.nunique()
    horizontal = spec.get("horizontal")
    if horizontal is None:
        horizontal = axis.vl_type == "nominal" and (n_bands > 8 or labels.str.len().max() > 14)
    stacked = bool(color) and spec.get("stack", True) is not False
    grouped = bool(color) and not stacked
    names: list[str] = []
    if color:
        df, names = _series(df, ctx, color, MAX_HUES, value=y, keys=[x])

    sort_kw: dict[str, Any] = {}
    if axis.vl_type == "nominal":
        if spec.get("_order"):
            sort_kw["sort"] = spec["_order"]
        elif str(spec.get("sort", "")).lower() in {"none", "data", "x"}:
            sort_kw["sort"] = None
        else:
            sort_kw["sort"] = alt.EncodingSortField(field=_escape(y), op="sum", order="descending")

    labelled = not color and n_bands <= MAX_LABELLED_BARS
    value_channel, category_channel = (alt.X, alt.Y) if horizontal else (alt.Y, alt.X)
    value_kw: dict[str, Any] = {"stack": "zero" if stacked else None}
    if labelled:
        value_kw["scale"] = _headroom(df[y].dropna())
    value_enc = value_channel(
        _escape(y),
        type="quantitative",
        title=None if horizontal else y_title,
        axis=alt.Axis(format=fmt, tickCount=5, labelOverlap=True),
        **value_kw,
    )
    category_enc = _position(axis, category_channel, {"ticks": False}, **sort_kw)
    position = {"x": value_enc, "y": category_enc} if horizontal else {"x": category_enc, "y": value_enc}

    tooltip = [_tooltip(axis)]
    encodings: dict[str, Any] = dict(position)
    mark: dict[str, Any] = {}
    if color:
        encodings["color"] = _color(ctx, color, names, {"symbolType": "square"})
        tooltip.append(alt.Tooltip(_escape(color), type="nominal", title=_title(color)))
        if stacked:
            # A 2px gap in the background color between stacked segments.
            mark.update(stroke=ctx.theme.surface, strokeWidth=2)
        else:
            offset = alt.YOffset if horizontal else alt.XOffset
            encodings["yOffset" if horizontal else "xOffset"] = offset(_escape(color), sort=names)
            mark.update(cornerRadiusEnd=4)
    else:
        mark.update(cornerRadiusEnd=4, color=ctx.theme.categorical[0])
    tooltip.append(_measure_tooltip(y, fmt, y_title))
    encodings["tooltip"] = tooltip

    if horizontal:
        step = (len(names) * (BAR_THICKNESS - 4) + 12) if grouped else BAR_STEP
        props: dict[str, Any] = {"height": alt.Step(step)}
        if not grouped:
            mark["size"] = BAR_THICKNESS
    else:
        props = {"height": CHART_HEIGHT}
        if not grouped:
            mark["size"] = max(2, min(24, int(ESTIMATED_WIDTH / max(n_bands, 1) * 0.6)))

    bars = alt.Chart(df).mark_bar(**mark).encode(**encodings)
    if not labelled:
        return bars.properties(**props)
    text_mark = (
        {"align": "left", "baseline": "middle", "dx": 4}
        if horizontal
        else {"align": "center", "baseline": "bottom", "dy": -4}
    )
    values = alt.Chart(df).mark_text(color=ctx.theme.ink, fontSize=11, **text_mark).encode(
        **position, text=alt.Text(_escape(y), type="quantitative", format=fmt)
    )
    return alt.layer(bars, values).properties(**props)


# --- Line and area ----------------------------------------------------------------


@dataclass
class _Series:
    df: pd.DataFrame
    axis: _Axis
    y: str
    y_title: str | None
    color: str | None
    names: list[str]
    fmt: str


def _over_x(df: pd.DataFrame, ctx: _Ctx) -> _Series:
    spec = ctx.spec
    x = _field(df, spec, "x")
    measures = _measures(df, spec)
    color = _field(df, spec, "color", required=False)
    fmt = _number_format(ctx, measures[0])
    y, y_title = measures[0], _title(measures[0])
    if len(measures) > 1 and not color:
        df, y, color = _melt(df, x, measures)
        y_title = None
    elif len(measures) > 1:
        ctx.notes.append(f"Showing {_title(y)} only. The other measures are in the Data tab.")
    axis = _dimension(df, x, continuous_time=True)
    if axis.vl_type == "nominal":
        axis = _Axis(x, "ordinal")
    names: list[str] = []
    if color:
        df, names = _series(df, ctx, color, MAX_HUES, value=y, keys=[x])
    df = df.dropna(subset=[x]).sort_values(x)
    return _Series(df, axis, y, y_title, color, names, fmt)


def _series_color(ctx: _Ctx, s: _Series, symbol: str) -> Any:
    if s.color:
        return _color(ctx, s.color, s.names, {"symbolType": symbol})
    return alt.value(ctx.theme.categorical[0])


def _crosshair(ctx: _Ctx, s: _Series, with_dots: bool = True) -> list[alt.Chart]:
    """A rule that snaps to the nearest x and lists every series at that x."""
    x_enc = _position(s.axis)
    nearest = alt.selection_point(
        nearest=True, on="pointerover", fields=[_escape(s.axis.name)], empty=False, clear="pointerout"
    )
    if s.color:
        base = alt.Chart(s.df).transform_pivot(
            _escape(s.color), value=_escape(s.y), groupby=[_escape(s.axis.name)]
        )
        tips = [_tooltip(s.axis)] + [_measure_tooltip(n, s.fmt, n) for n in s.names]
    else:
        base = alt.Chart(s.df)
        tips = [_tooltip(s.axis), _measure_tooltip(s.y, s.fmt, s.y_title)]
    layers = [
        base.mark_rule(color=ctx.theme.muted, strokeWidth=1)
        .encode(
            x=x_enc,
            opacity=alt.when(nearest).then(alt.value(0.8)).otherwise(alt.value(0)),
            tooltip=tips,
        )
        .add_params(nearest)
    ]
    if with_dots:
        layers.append(
            alt.Chart(s.df)
            .mark_point(filled=True, size=64, stroke=ctx.theme.surface, strokeWidth=2, opacity=1)
            .encode(x=x_enc, y=alt.Y(_escape(s.y), type="quantitative"), color=_series_color(ctx, s, "stroke"))
            .transform_filter(nearest)
        )
    return layers


def _end_marks(ctx: _Ctx, s: _Series) -> list[alt.Chart]:
    """An end-dot on every series; a single series also gets its last value."""
    groupby = [_escape(s.color)] if s.color else []
    last = (
        alt.Chart(s.df)
        .transform_window(
            rank="row_number()", sort=[alt.SortField(_escape(s.axis.name), order="descending")], groupby=groupby
        )
        .transform_filter(alt.datum.rank == 1)
    )
    x_enc, y_enc = _position(s.axis), alt.Y(_escape(s.y), type="quantitative")
    layers = [
        last.mark_point(filled=True, size=64, stroke=ctx.theme.surface, strokeWidth=2, opacity=1).encode(
            x=x_enc, y=y_enc, color=_series_color(ctx, s, "stroke")
        )
    ]
    if not s.color:
        layers.append(
            last.mark_text(align="right", baseline="bottom", dx=4, dy=-8, color=ctx.theme.ink, fontSize=11).encode(
                x=x_enc, y=y_enc, text=alt.Text(_escape(s.y), type="quantitative", format=s.fmt)
            )
        )
    return layers


def _y(s: _Series, **kwargs: Any) -> alt.Y:
    return alt.Y(
        _escape(s.y), type="quantitative", title=s.y_title, axis=alt.Axis(format=s.fmt, tickCount=5), **kwargs
    )


def _line(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    s = _over_x(df, ctx)
    line = alt.Chart(s.df).mark_line(strokeWidth=2, strokeCap="round", strokeJoin="round").encode(
        x=_position(s.axis), y=_y(s), color=_series_color(ctx, s, "stroke")
    )
    return alt.layer(line, *_end_marks(ctx, s), *_crosshair(ctx, s)).properties(height=CHART_HEIGHT)


def _area(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    if ctx.spec.get("color") and ctx.spec.get("stack") is False:
        return _line(df, ctx)
    s = _over_x(df, ctx)
    if s.color:
        area = alt.Chart(s.df).mark_area(opacity=0.85, stroke=ctx.theme.surface, strokeWidth=1).encode(
            x=_position(s.axis), y=_y(s, stack="zero"), color=_series_color(ctx, s, "square")
        )
        return alt.layer(area, *_crosshair(ctx, s, with_dots=False)).properties(height=CHART_HEIGHT)
    hue = ctx.theme.categorical[0]
    wash = alt.Chart(s.df).mark_area(color=hue, opacity=0.1).encode(x=_position(s.axis), y=_y(s))
    line = alt.Chart(s.df).mark_line(color=hue, strokeWidth=2, strokeCap="round").encode(
        x=_position(s.axis), y=alt.Y(_escape(s.y), type="quantitative")
    )
    return alt.layer(wash, line, *_end_marks(ctx, s), *_crosshair(ctx, s)).properties(height=CHART_HEIGHT)


# --- Scatter, pie, heatmap -------------------------------------------------------------


def _scatter(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    spec = ctx.spec
    x = _field(df, spec, "x")
    y = _measures(df, spec)[0]
    df[x] = pd.to_numeric(df[x], errors="coerce")
    df = df.dropna(subset=[x, y])
    color = _field(df, spec, "color", required=False)
    label = _field(df, spec, "label", required=False)
    x_fmt, y_fmt = _number_format(ctx, x), _number_format(ctx, y)

    tooltip = [_measure_tooltip(x, x_fmt), _measure_tooltip(y, y_fmt)]
    if color:
        tooltip.insert(0, alt.Tooltip(_escape(color), type="nominal", title=_title(color)))
    if label:
        tooltip.insert(0, alt.Tooltip(_escape(label), type="nominal", title=_title(label)))
    position = {
        "x": alt.X(_escape(x), type="quantitative", title=_title(x), axis=alt.Axis(format=x_fmt, tickCount=5), scale=alt.Scale(zero=False)),
        "y": alt.Y(_escape(y), type="quantitative", title=_title(y), axis=alt.Axis(format=y_fmt, tickCount=5), scale=alt.Scale(zero=False)),
    }
    if color:
        # On a scatter every pair of colors meets, and only three slots stay distinct.
        df, names = _series(df, ctx, color, MAX_SCATTER_HUES)
        color_enc = _color(ctx, color, names, {"symbolType": "circle"})
    else:
        color_enc = alt.value(ctx.theme.categorical[0])

    dots = alt.Chart(df).mark_point(filled=True, size=64, opacity=0.9, stroke=ctx.theme.surface, strokeWidth=2).encode(
        **position, color=color_enc, tooltip=tooltip
    )
    # An invisible 24px target per point, so hovering does not need pixel-perfect aim.
    targets = alt.Chart(df).mark_point(size=576, opacity=0).encode(**position, tooltip=tooltip)
    layers = [dots, targets]
    if label:
        # Label only the points the story is about; the tooltip names the rest,
        # so clustered points never get a pile of overlapping names.
        story = df.loc[sorted(set(df.nlargest(3, y).index) | set(df.nlargest(1, x).index))]
        layers.append(
            alt.Chart(story).mark_text(align="left", dx=7, dy=-7, fontSize=11, color=ctx.theme.ink).encode(
                **position, text=alt.Text(_escape(label), type="nominal")
            )
        )
    return alt.layer(*layers).properties(height=CHART_HEIGHT)


def _pie(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    spec = ctx.spec
    category = _field(df, spec, "x", required=False) or _field(df, spec, "color")
    value = _measures(df, spec)[0]
    df = df.dropna(subset=[value])
    df = df[df[value] > 0].copy()
    df[category] = df[category].astype("string").fillna("(blank)")
    df = df.groupby(category, as_index=False, sort=False)[value].sum().sort_values(value, ascending=False)
    if len(df) > MAX_PIE_SLICES:
        head, tail = df.head(MAX_PIE_SLICES - 1), df.iloc[MAX_PIE_SLICES - 1 :]
        df = pd.concat([head, pd.DataFrame({category: [OTHER], value: [tail[value].sum()]})])
        ctx.notes.append(f"{len(tail)} smaller slices are combined as “{OTHER}”.")
    names = [str(n) for n in df[category]]
    fmt = _number_format(ctx, value)
    return (
        alt.Chart(df)
        .transform_joinaggregate(total=f"sum({_escape(value)})")
        .transform_calculate(share=f"datum[{_js_string(value)}] / datum.total")
        .mark_arc(innerRadius=70, outerRadius=120, stroke=ctx.theme.surface, strokeWidth=2)
        .encode(
            theta=alt.Theta(_escape(value), type="quantitative", stack=True),
            color=_color(ctx, category, names, {"symbolType": "square", "orient": "right"}),
            order=alt.Order(_escape(value), sort="descending"),
            tooltip=[
                alt.Tooltip(_escape(category), type="nominal", title=_title(category)),
                _measure_tooltip(value, fmt),
                alt.Tooltip("share", type="quantitative", format=".1%", title="Share"),
            ],
        )
        .properties(height=260)
    )


def _heatmap(df: pd.DataFrame, ctx: _Ctx) -> alt.TopLevelMixin:
    spec = ctx.spec
    x = _field(df, spec, "x")
    y = _field(df, spec, "y")
    value = spec.get("value") or spec.get("color")
    if not value or value not in df.columns:
        numeric = [c for c in df.columns if c not in {x, y} and pd.api.types.is_numeric_dtype(df[c])]
        if not numeric:
            raise ChartError("A heatmap needs a numeric 'value' column.")
        value = numeric[0]
    df[value] = pd.to_numeric(df[value], errors="coerce")
    axes = []
    for name in (x, y):
        axis = _dimension(df, name, continuous_time=False)
        axes.append(_Axis(name, "ordinal") if axis.vl_type == "quantitative" else axis)
    x_axis, y_axis = axes
    fmt = _number_format(ctx, value)
    chrome = {"ticks": False, "domain": False}
    return (
        alt.Chart(df)
        .mark_rect(stroke=ctx.theme.surface, strokeWidth=2, cornerRadius=2)
        .encode(
            x=_position(x_axis, alt.X, chrome),
            y=_position(y_axis, alt.Y, chrome),
            color=alt.Color(
                _escape(value),
                type="quantitative",
                title=_title(value),
                scale=alt.Scale(range=list(ctx.theme.sequential)),
                legend=alt.Legend(orient="top", format=fmt, gradientLength=220),
            ),
            tooltip=[_tooltip(x_axis), _tooltip(y_axis), _measure_tooltip(value, fmt)],
        )
        .properties(height=alt.Step(26) if df[y].nunique() > 6 else CHART_HEIGHT)
    )
