from analyst.protocol import (
    RESPONSE_FORMAT,
    TextSegment,
    VizError,
    VizSegment,
    build_first_message,
    build_follow_up_message,
    escape_dollars,
    loads_lenient,
    parse_message,
)


def kinds(segments):
    return [
        s.spec["type"] if isinstance(s, VizSegment) else type(s).__name__ for s in segments
    ]


def test_recorded_answers_parse_into_text_and_visuals(recorded):
    first = parse_message(recorded["01-constructor-wins"]["turn"][0]["content"])
    assert kinds(first) == ["TextSegment", "bar", "line", "TextSegment"]
    bar = first[1].spec
    assert bar["data"][0] == {"constructor_name": "Mercedes", "wins": 116}
    assert len(first[2].spec["data"]) == 33

    second = parse_message(recorded["02-red-bull-2023"]["turn"][0]["content"])
    assert kinds(second) == ["TextSegment", "metrics", "bar", "TextSegment"]
    assert [i["value"] for i in second[1].spec["items"]] == [22, 21, 30, 2, 860, 1]


def test_text_between_blocks_is_kept_in_order():
    message = 'Intro\n\n```viz\n{"type": "table", "data": [{"a": 1}]}\n```\n\nOutro'
    segments = parse_message(message)
    assert [type(s) for s in segments] == [TextSegment, VizSegment, TextSegment]
    assert segments[0].markdown == "Intro"
    assert segments[2].markdown == "Outro"


def test_lenient_json_forgives_common_llm_slips():
    raw = '{"type": "table", "sql": "select 1\nfrom t", "data": [{"a": NaN, "b": 2,},],}'
    obj = loads_lenient(raw)
    assert obj["sql"] == "select 1\nfrom t"
    assert obj["data"] == [{"a": None, "b": 2}]


def test_broken_viz_becomes_an_error_not_a_crash():
    segments = parse_message('```viz\n{"type": "bar", "data": [\n```')
    assert isinstance(segments[0], VizError)
    assert "Invalid JSON" in segments[0].error


def test_viz_needs_rows_and_a_known_type():
    no_rows = parse_message('```viz\n{"type": "bar", "data": []}\n```')[0]
    unknown = parse_message('```viz\n{"type": "sankey", "data": [{"a": 1}]}\n```')[0]
    assert isinstance(no_rows, VizError) and isinstance(unknown, VizError)
    assert no_rows.error == "The visual has no rows."


def test_type_aliases_are_normalised():
    donut = parse_message('```viz\n{"type": "donut", "data": [{"a": 1}]}\n```')[0]
    kpi = parse_message('```viz\n{"type": "kpi", "items": [{"label": "x", "value": 1}]}\n```')[0]
    assert donut.spec["type"] == "pie" and kpi.spec["type"] == "metrics"


def test_plain_json_stays_code_unless_it_is_clearly_a_viz():
    config = parse_message('```json\n{"threshold": 3}\n```')
    viz = parse_message('```json\n{"type": "line", "x": "d", "y": "v", "data": [{"d": 1, "v": 2}]}\n```')
    assert isinstance(config[0], TextSegment) and "threshold" in config[0].markdown
    assert isinstance(viz[0], VizSegment)


def test_vega_lite_blocks_pass_through():
    segment = parse_message('```vega-lite\n{"mark": "bar"}\n```')[0]
    assert segment.spec == {"type": "vega-lite", "spec": {"mark": "bar"}}


def test_unclosed_fence_is_left_as_text():
    segments = parse_message('Answer\n```viz\n{"type": "bar"')
    assert len(segments) == 1 and isinstance(segments[0], TextSegment)


def test_dollars_are_escaped_outside_code_only():
    text = "Revenue grew from $1.2M to $3M. Run `echo $HOME`.\n```sql\nselect '$x'\n```"
    escaped = escape_dollars(text)
    assert "\\$1.2M to \\$3M" in escaped
    assert "`echo $HOME`" in escaped and "select '$x'" in escaped


def test_messages_carry_the_contract_once():
    first = build_first_message("How many races?", context="Fiscal year starts in April.")
    assert first.startswith(RESPONSE_FORMAT)
    assert "<business_context>\nFiscal year starts in April.\n</business_context>" in first
    assert first.endswith("<question>\nHow many races?\n</question>")
    follow_up = build_follow_up_message("And in 2022?")
    assert follow_up.startswith("And in 2022?") and RESPONSE_FORMAT not in follow_up


def test_the_status_label_is_the_first_line_of_prose():
    from analyst.render import headline

    assert headline("**Checking race results since 2014.**\n\nMore detail.") == "Checking race results since 2014."
    assert headline('```viz\n{"type": "table", "data": [{"a": 1}]}\n```') == "DinoAI is working"
    assert headline("word " * 40).endswith("…") and len(headline("word " * 40)) <= 82


def test_compact_rows_expand_into_records_in_column_order():
    block = '```viz\n{"type": "table", "columns": ["driver", "wins"], "rows": [["Hamilton", 9], ["Vettel", 5]]}\n```'
    spec = parse_message(block)[0].spec
    assert spec["data"] == [{"driver": "Hamilton", "wins": 9}, {"driver": "Vettel", "wins": 5}]
    assert spec["columns"] == ["driver", "wins"]


def test_compact_rows_must_match_the_columns():
    block = '```viz\n{"type": "bar", "columns": ["a", "b"], "rows": [[1, 2], [3]]}\n```'
    segment = parse_message(block)[0]
    assert isinstance(segment, VizError) and "Row 2 has 1 values for 2 columns" in segment.error
