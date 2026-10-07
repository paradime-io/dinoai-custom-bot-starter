import pytest

from analyst.charts import DARK, LIGHT, ChartError, ColorRegistry, build_chart
from analyst.protocol import VizSegment, parse_message


def compile_all(built):
    """to_dict() validates every chart against the Vega-Lite schema."""
    return [chart.to_dict() for chart in built.charts]


def recorded_specs(recorded):
    for fixture in recorded.values():
        for step in fixture["turn"]:
            for segment in parse_message(step["content"]):
                if isinstance(segment, VizSegment) and segment.spec["type"] != "metrics":
                    yield segment.spec


@pytest.mark.parametrize("theme", [LIGHT, DARK])
def test_recorded_agent_charts_compile(recorded, theme):
    specs = list(recorded_specs(recorded))
    assert {s["type"] for s in specs} == {"bar", "line"}
    for spec in specs:
        assert compile_all(build_chart(spec, theme))


def test_mixed_units_split_into_panels_with_one_order(recorded):
    spec = next(s for s in recorded_specs(recorded) if s["y"] == ["season_points", "wins"])
    built = build_chart(spec)
    assert len(built.charts) == 2
    assert any("different scales" in note for note in built.notes)
    orders = [_bar_sort(chart.to_dict()) for chart in built.charts]
    assert orders[0] == orders[1] and orders[0][0] == "Max Verstappen"


def _bar_sort(vl):
    bars = vl["layer"][0]
    return bars["encoding"]["y"]["sort"]


def test_years_on_a_line_chart_become_a_time_axis(recorded):
    spec = next(s for s in recorded_specs(recorded) if s["type"] == "line")
    vl = compile_all(build_chart(spec))[0]
    x = vl["layer"][0]["encoding"]["x"]
    assert x["type"] == "temporal" and x["axis"]["format"] == "%Y"


def test_entity_keeps_its_color_across_charts():
    registry = ColorRegistry()
    first = {"type": "line", "x": "year", "y": "wins", "color": "team", "data": [
        {"year": 2020, "team": "Mercedes", "wins": 13}, {"year": 2020, "team": "Red Bull", "wins": 2},
    ]}
    second = {"type": "bar", "x": "year", "y": "wins", "color": "team", "data": [
        {"year": 2023, "team": "Red Bull", "wins": 21}, {"year": 2023, "team": "Ferrari", "wins": 1},
    ]}
    build_chart(first, LIGHT, registry)
    build_chart(second, LIGHT, registry)
    assert registry.slots == {"Mercedes": 0, "Red Bull": 1, "Ferrari": 2}


SPECS = {
    "vertical bar, monthly dates": {"type": "bar", "x": "month", "y": "revenue", "format": {"revenue": "$,.0f"},
        "data": [{"month": f"2024-{m:02d}-01", "revenue": 1000 * m} for m in range(1, 7)]},
    "grouped bar": {"type": "bar", "x": "region", "y": "orders", "color": "channel", "stack": False,
        "data": [{"region": r, "channel": c, "orders": i} for i, (r, c) in enumerate(
            [(r, c) for r in ["EMEA", "AMER"] for c in ["Web", "Store"]])]},
    "stacked bar": {"type": "bar", "x": "quarter", "y": "revenue", "color": "product",
        "data": [{"quarter": q, "product": p, "revenue": 10} for q in ["Q1", "Q2"] for p in "ABC"]},
    "two measures, same scale": {"type": "line", "x": "day", "y": ["revenue", "cost"],
        "data": [{"day": f"2024-01-{d:02d}", "revenue": 100 + d, "cost": 80 + d} for d in range(1, 9)]},
    "single area": {"type": "area", "x": "week", "y": "signups",
        "data": [{"week": f"2024-02-{d:02d}", "signups": d * 3} for d in range(1, 20)]},
    "stacked area": {"type": "area", "x": "year", "y": "users", "color": "plan",
        "data": [{"year": y, "plan": p, "users": y - 2000} for y in range(2018, 2024) for p in ["Free", "Pro"]]},
    "scatter": {"type": "scatter", "x": "points", "y": "wins", "label": "driver", "color": "team",
        "data": [{"driver": f"D{i}", "team": f"T{i % 5}", "points": i * 10, "wins": i} for i in range(10)]},
    "pie": {"type": "pie", "x": "segment", "y": "share",
        "data": [{"segment": f"S{i}", "share": 10 - i} for i in range(9)]},
    "heatmap": {"type": "heatmap", "x": "weekday", "y": "hour", "value": "orders",
        "data": [{"weekday": d, "hour": h, "orders": h * 2} for d in ["Mon", "Tue"] for h in range(8, 12)]},
    "field names with dots": {"type": "bar", "x": "st.name", "y": "total.amount",
        "data": [{"st.name": "St. Louis", "total.amount": 5}, {"st.name": "Boston", "total.amount": 3}]},
}


@pytest.mark.parametrize("name", SPECS)
def test_every_chart_type_compiles(name):
    built = build_chart(SPECS[name], DARK)
    assert compile_all(built)


def test_too_many_groups_fold_into_other():
    scatter = build_chart(SPECS["scatter"])
    pie = build_chart(SPECS["pie"])
    assert any("combined" in note for note in scatter.notes)
    assert any("combined" in note for note in pie.notes)
    color = scatter.charts[0].to_dict()["layer"][0]["encoding"]["color"]
    assert color["scale"]["domain"][-1] == "Other" and len(color["scale"]["domain"]) == 4


def test_scatter_labels_only_the_standout_points():
    chart = build_chart(SPECS["scatter"]).charts[0]
    labels = chart.layer[2].data
    assert sorted(labels["driver"]) == ["D7", "D8", "D9"]  # top 3 by wins, and the most points


def test_unknown_columns_raise_a_readable_error():
    with pytest.raises(ChartError, match="'revenue'"):
        build_chart({"type": "bar", "x": "month", "y": "revenue", "data": [{"month": "Jan", "sales": 1}]})


def test_bad_formats_are_dropped_not_rendered():
    spec = {"type": "bar", "x": "k", "y": "v", "horizontal": True, "format": {"v": "{{evil}}"},
            "data": [{"k": "a", "v": 1}]}
    vl = compile_all(build_chart(spec))[0]
    assert vl["layer"][0]["encoding"]["x"]["axis"]["format"] == ",~f"
