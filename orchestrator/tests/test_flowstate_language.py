import pytest

from flowstate import conditions
from flowstate.errors import FlowstateError
from flowstate.templating import placeholders, render


def ev(expr, **variables):
    return conditions.evaluate(conditions.parse(expr), variables, expr)


def test_templating_substitutes_only_identifier_placeholders(tmp_path):
    text = 'Topic: {topic}\nWrite JSON like {\n  "_session_id": "<id>"\n} and {"a":1} to {out}'
    assert placeholders(text) == ({"topic", "out"}, [])
    out = render(text, {"topic": "tides", "out": "/r/x.json"}, tmp_path, "prompt")
    assert out.startswith("Topic: tides") and out.endswith("to /r/x.json")
    assert '{"a":1}' in out and '"_session_id": "<id>"' in out


def test_templating_formats_values_and_rejects_missing(tmp_path):
    assert render("{b} {n} {d}", {"b": True, "n": 3, "d": {"k": 1}}, tmp_path, "t") == 'true 3 {"k": 1}'
    with pytest.raises(FlowstateError) as exc:
        render("{present} {absent}", {"present": "x"}, tmp_path, "prompt")
    assert exc.value.code == "missing_variable" and exc.value.details["variables"] == ["absent"]
    with pytest.raises(FlowstateError):
        render("{nothing}", {"nothing": None}, tmp_path, "prompt")  # never an empty string


def test_templating_includes_stay_inside_flow(tmp_path):
    (tmp_path / "guide.md").write_text("GUIDE {not_rendered}")
    assert render("A {include:guide.md} B", {}, tmp_path, "p") == "A GUIDE {not_rendered} B"
    with pytest.raises(FlowstateError) as exc:
        render("{include:../secret.txt}", {}, tmp_path, "p")
    assert exc.value.code == "include_outside_flow"


@pytest.mark.parametrize("expr,variables,expected", [
    ('status == "ok"', {"status": "ok"}, True),
    ("status != 'ok'", {"status": "ok"}, False),
    ("count > 0", {"count": 3}, True),
    ("count >= 3.5", {"count": 3}, False),
    ("0 < count", {"count": 1}, True),
    ("flag == true", {"flag": True}, True),
    ("flag == 1", {"flag": True}, False),      # booleans are not numbers
    ("n == 2.0", {"n": 2}, True),
    ("x == null", {"x": None}, True),
    ('a == "x" and b > 1 or c == "y"', {"a": "x", "b": 0, "c": "y"}, True),
    ('a == "x" and (b > 1)', None, None),      # parentheses are not part of the language
])
def test_conditions(expr, variables, expected):
    if expected is None:
        with pytest.raises(FlowstateError):
            conditions.parse(expr)
    else:
        assert ev(expr, **variables) is expected


@pytest.mark.parametrize("expr", [
    "__import__('os').system('x')", "count", "count >", "a = 1", "len(items) > 0", "a == b == c",
    "", "status == ok and", "1 +", "a === 1",
])
def test_conditions_reject_anything_else(expr):
    with pytest.raises(FlowstateError):
        conditions.parse(expr)


def test_condition_runtime_errors():
    with pytest.raises(FlowstateError, match="unknown variable"):
        ev("missing > 0")
    with pytest.raises(FlowstateError, match="cannot compare"):
        ev('count > "3"', count=3)
    assert conditions.identifiers(conditions.parse('a == 1 or b != "z"')) == {"a", "b"}
