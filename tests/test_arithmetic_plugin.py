"""Contract tests for the standalone arithmetic plugin, without host imports."""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from decimal import Decimal, getcontext, localcontext
from pathlib import Path

import pytest
import yaml


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "arithmetic"
SPEC = importlib.util.spec_from_file_location("standalone_arithmetic_test", PLUGIN_ROOT / "__init__.py")
arithmetic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = arithmetic
SPEC.loader.exec_module(arithmetic)


class PublicContext:
    """Expose exactly the declared public registration protocol, nothing else."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools = {}
        self.config_reads = []

    def get_config(self, key, default=None):
        self.config_reads.append((key, default))
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="", emoji="", override=False):
        assert name not in self.tools
        self.tools[name] = {
            "name": name, "toolset": toolset, "schema": schema, "handler": handler,
            "check_fn": check_fn, "requires_env": requires_env, "is_async": is_async,
            "description": description, "emoji": emoji, "override": override,
        }


@pytest.fixture
def context():
    ctx = PublicContext()
    arithmetic.register(ctx)
    return ctx


def call(ctx, operation, arguments):
    raw = ctx.tools[f"arithmetic_{operation}"]["handler"](
        arguments, session_id="test-session", task_id="test-task", user_task="calculate",
    )
    assert isinstance(raw, str)
    return json.loads(raw)


def parse(ctx, expression):
    return call(ctx, "parse", {"expression": expression})


def error_code(result):
    assert "result" not in result
    assert set(result) == {"error"}
    assert isinstance(result["error"]["message"], str)
    assert result["error"]["execution"]["tool"].startswith("arithmetic_")
    return result["error"]["code"]


def test_manifest_matches_registration_and_public_protocol(context):
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())
    assert manifest["manifest_version"] == 2
    assert manifest["name"] == "arithmetic"
    assert manifest["kind"] == "standalone"
    assert manifest["provides_hooks"] == []
    assert manifest["requires_env"] == []
    assert set(manifest["provides_tools"]) == set(context.tools)
    assert len(context.tools) == 5
    assert {key for key, _ in context.config_reads} == set(manifest["config_schema"])
    for name, tool in context.tools.items():
        assert tool["toolset"] == "arithmetic"
        assert tool["is_async"] is False
        assert tool["requires_env"] == []
        assert tool["override"] is False
        assert tool["schema"]["name"] == name
        assert tool["schema"]["parameters"]["additionalProperties"] is False
        assert tool["schema"]["description"] == tool["description"]
        assert "MUST" in tool["description"]
        assert "mentally" in tool["description"]
        assert "browser_eval" in tool["description"]


@pytest.mark.parametrize("operation,a,b,expected", [
    ("add", "0.1", "0.2", "0.3"),
    ("subtract", 10, 3, "7"),
    ("multiply", "35", "7", "245"),
    ("divide", "117", "20", "5.85"),
    ("add", "-0", "0.000", "0"),
    ("subtract", "-4.5", "2.25", "-6.75"),
    ("multiply", "-1.20", "-2", "2.4"),
    ("divide", 0, 5, "0"),
    ("add", 0.1, 0.2, "0.3"),
    ("add", "1e-5", ".00002", "0.00003"),
    ("add", "1e3", "-999", "1"),
    ("multiply", "999999999999999999999999999999999999", "1", "999999999999999999999999999999999999"),
])
def test_direct_operations(context, operation, a, b, expected):
    result = call(context, operation, {"a": a, "b": b})
    assert result["result"] == expected
    assert result["operation"] == operation
    assert result["precision"] == 50
    assert result["rounding"] == "ROUND_HALF_EVEN"
    assert len(result["operands"]) == 2


@pytest.mark.parametrize("expression,expected,syntax_type", [
    ("(35 × 7 - 128) ÷ (12 + 8)", "5.85", "binary"),
    ("2 + 3 * 4", "14", "binary"),
    ("(2 + 3) * 4", "20", "binary"),
    ("8 / 4 / 2", "1", "binary"),
    ("10 - 3 - 2", "5", "binary"),
    (" -2 * -3 + +4 ", "10", "binary"),
    ("--2", "2", "unary"),
    ("+-+2", "-2", "unary"),
    ("-(2+3)×+.5", "-2.5", "binary"),
    ("1.", "1", "number"),
    (".5 + .25", "0.75", "binary"),
    ("0.1 + 0.2", "0.3", "binary"),
    ("(  ( 35 ) )", "35", "number"),
    ("\t1\n+\r2\u2003×3", "7", "binary"),
    ("0", "0", "number"),
    ("-0.0000", "0", "unary"),
    ("2*-3", "-6", "binary"),
    ("2--3", "5", "binary"),
])
def test_parse_expression(context, expression, expected, syntax_type):
    result = parse(context, expression)
    assert result["result"] == expected
    assert result["type"] == "expression"
    assert result["syntax_type"] == syntax_type
    assert result["ast"]["type"] == syntax_type
    assert result["execution"]["tool"] == "arithmetic_parse"


def test_example_postorder_steps_reuse_four_functions(context, monkeypatch):
    used = []
    for operation in ("add", "subtract", "multiply", "divide"):
        original = getattr(arithmetic.ArithmeticEngine, operation)
        def wrapper(self, a, b, _original=original, _operation=operation):
            used.append((_operation, str(a), str(b)))
            return _original(self, a, b)
        monkeypatch.setattr(arithmetic.ArithmeticEngine, operation, wrapper)
    result = parse(context, "(35 × 7 - 128) ÷ (12 + 8)")
    assert [step["operation"] for step in result["steps"]] == ["multiply", "subtract", "add", "divide"]
    assert [step["result"] for step in result["steps"]] == ["245", "117", "20", "5.85"]
    assert [step["index"] for step in result["steps"]] == [1, 2, 3, 4]
    assert [step["operands"] for step in result["steps"]] == [["35", "7"], ["245", "128"], ["12", "8"], ["117", "20"]]
    assert [name for name, _, _ in used] == ["multiply", "subtract", "add", "divide"]
    assert result["ast"]["operator"] == "/"
    assert result["steps"][-1]["position"] == 15
    call(context, "add", {"a": "1", "b": "2"})
    assert used[-1][0] == "add"


def test_unary_steps_also_reuse_basic_operations(context):
    result = parse(context, "-+2")
    assert [(step["type"], step["operation"], step["operands"]) for step in result["steps"]] == [
        ("unary", "add", ["0", "2"]),
        ("unary", "subtract", ["0", "2"]),
    ]
    assert parse(context, "123")["steps"] == []


@pytest.mark.parametrize("expression,position", [
    ("", 0), ("   ", 3), ("1+", 2), ("*1", 0),
    ("(1+2", 4), ("1+2)", 3), ("()", 1), ("1 2", 2),
    ("1..2", 2), ("2(3)", 1), ("1/**2", 2), (".", 0),
    ("((1)", 4), ("+", 1), ("1++", 3), ("1 // 2", 3),
    ("(1)(2)", 3), (")1(", 0), ("1×÷2", 2), ("1 + (2 *)", 8),
])
def test_malformed_syntax_has_position(context, expression, position):
    result = parse(context, expression)
    assert error_code(result) == "syntax_error"
    assert result["error"]["position"] == position


@pytest.mark.parametrize("expression,position", [
    ("1a", 1), ("NaN", 0), ("Infinity", 0), ("1e3", 1),
    ("2^3", 1), ("3%2", 1), ("1,2", 1), ("[1]", 0),
    ("０+1", 0), ("1−2", 1), ("1=2", 1), ("1;2", 1),
])
def test_invalid_tokens_are_explicit(context, expression, position):
    result = parse(context, expression)
    assert error_code(result) == "invalid_token"
    assert result["error"]["position"] == position


@pytest.mark.parametrize("zero", ["0", "-0", "0.000", 0, -0.0])
def test_division_by_zero_direct(context, zero):
    assert error_code(call(context, "divide", {"a": "1", "b": zero})) == "division_by_zero"


@pytest.mark.parametrize("expression,position", [("1/0", 1), ("0÷0", 1), ("1/(2-2)", 1), ("5 / -0.00", 2)])
def test_division_by_zero_parse(context, expression, position):
    result = parse(context, expression)
    assert error_code(result) == "division_by_zero"
    assert result["error"]["position"] == position


@pytest.mark.parametrize("operand", ["NaN", "sNaN", "Infinity", "-Inf", float("nan"), float("inf"), Decimal("NaN")])
def test_non_finite_numbers_rejected(context, operand):
    assert error_code(call(context, "add", {"a": operand, "b": 1})) == "non_finite_number"


@pytest.mark.parametrize("operation,args", [
    ("parse", {}), ("parse", {"expression": 3}),
    ("parse", {"expression": "1", "extra": True}),
    ("parse", None), ("parse", []),
    ("add", {"a": 1}), ("add", {"a": 1, "b": 2, "extra": 0}),
    ("add", {"a": True, "b": 2}), ("add", {"a": None, "b": 2}),
    ("add", {"a": [1], "b": 2}), ("add", {"a": {}, "b": 2}),
])
def test_invalid_argument_shapes_are_business_errors(context, operation, args):
    assert error_code(call(context, operation, args)) == "invalid_arguments"


@pytest.mark.parametrize("operand", ["", " ", "1+2", "0x10", "1_000", "1.2.3", "__import__('os')"])
def test_invalid_direct_numbers(context, operand):
    assert error_code(call(context, "add", {"a": operand, "b": "1"})) == "invalid_number"


def test_precision_rounding_and_global_context_isolation():
    ctx = PublicContext({"precision": 6})
    arithmetic.register(ctx)
    with localcontext() as global_ctx:
        global_ctx.prec = 2
        assert call(ctx, "divide", {"a": "1", "b": "3"})["result"] == "0.333333"
        assert parse(ctx, "1/3")["result"] == "0.333333"
        assert getcontext().prec == 2
    tie_ctx = PublicContext({"precision": 2})
    arithmetic.register(tie_ctx)
    assert call(tie_ctx, "add", {"a": "1.25", "b": 0})["result"] == "1.2"
    assert call(tie_ctx, "add", {"a": "1.35", "b": 0})["result"] == "1.4"


@pytest.mark.parametrize("settings", [
    {"precision": 1}, {"precision": 101}, {"precision": True}, {"precision": "50"},
    {"max_depth": 129}, {"max_tokens": 0}, {"max_expression_length": 16385},
    {"max_number_digits": 257},
])
def test_invalid_configuration_fails_before_registration(settings):
    ctx = PublicContext(settings)
    with pytest.raises(ValueError):
        arithmetic.register(ctx)
    assert ctx.tools == {}


def test_expression_and_operand_length_bounds(context):
    assert error_code(parse(context, " " * 4097)) == "expression_too_long"
    assert error_code(parse(context, "9" * 129)) == "numeric_limit"
    assert error_code(parse(context, "0." + "0" * 128)) == "numeric_limit"
    assert error_code(call(context, "add", {"a": "9" * 129, "b": 1})) == "numeric_limit"
    assert error_code(call(context, "add", {"a": "9" * 10000, "b": 1})) == "numeric_limit"
    assert error_code(call(context, "add", {"a": " " * 10000 + "1", "b": 1})) == "numeric_limit"
    assert error_code(call(context, "add", {"a": 10 ** 10000, "b": 1})) == "numeric_limit"
    assert error_code(call(context, "add", {"a": "1e513", "b": 1})) == "numeric_limit"
    assert error_code(call(context, "add", {"a": "0e-513", "b": 1})) == "numeric_limit"
    assert error_code(call(context, "multiply", {"a": "1e512", "b": "10"})) == "numeric_limit"
    assert error_code(call(context, "divide", {"a": "1e-512", "b": "1e512"})) == "numeric_limit"
    assert error_code(call(context, "divide", {"a": "1e-512", "b": "10"})) == "numeric_limit"
    assert call(context, "divide", {"a": "1e-512", "b": "1"})["result"] == "0." + "0" * 511 + "1"


def test_token_nesting_unary_and_ast_depth_bounds(context):
    assert error_code(parse(context, "(" * 65 + "1" + ")" * 65)) == "complexity_limit"
    assert error_code(parse(context, "-" * 65 + "1")) == "complexity_limit"
    assert error_code(parse(context, "+".join(["1"] * 65))) == "complexity_limit"
    assert error_code(parse(context, "1+" * 512 + "1")) == "complexity_limit"
    assert parse(context, "(" * 64 + "1" + ")" * 64)["result"] == "1"
    assert parse(context, "+".join(["1"] * 64))["result"] == "64"
    limited = PublicContext({"max_expression_length": 10, "max_tokens": 3, "max_depth": 2, "max_number_digits": 2})
    arithmetic.register(limited)
    assert parse(limited, "1+2")["result"] == "3"
    assert error_code(parse(limited, "1+2+3")) == "complexity_limit"
    assert error_code(parse(limited, "123")) == "numeric_limit"
    assert error_code(parse(limited, " " * 11)) == "expression_too_long"


@pytest.mark.parametrize("template", [
    "__import__('pathlib').Path({path!r}).write_text('bad')",
    "open({path!r}, 'w').write('bad')",
    "exec(\"open({path!r}, 'w')\")",
    "1; touch {path}",
    "(() => {{ document.location = {path!r}; }})()",
    "{{'x': 1}}", "[x for x in range(10)]", "lambda: 1",
])
def test_code_injection_is_rejected_without_side_effects(context, tmp_path, template):
    sentinel = tmp_path / "must-not-exist"
    expression = template.format(path=str(sentinel))
    result = parse(context, expression)
    assert error_code(result) in {"invalid_token", "syntax_error"}
    assert 0 <= result["error"]["position"] < len(expression)
    assert not sentinel.exists()


def test_implementation_does_not_import_host_or_call_dynamic_code():
    tree = ast.parse((PLUGIN_ROOT / "__init__.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("harness") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("harness")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile", "__import__"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"eval", "exec", "system", "popen"}


def test_hard_maximum_depth_stays_bounded_without_recursion_error():
    ctx = PublicContext({"max_depth": 128})
    arithmetic.register(ctx)
    assert parse(ctx, "(" * 128 + "1" + ")" * 128)["result"] == "1"
    assert error_code(parse(ctx, "(" * 129 + "1" + ")" * 129)) == "complexity_limit"
    assert error_code(parse(ctx, "-" * 129 + "1")) == "complexity_limit"
