"""Standalone, bounded Decimal arithmetic; no host imports or code execution.

Tool results encode Decimal values as canonical strings to preserve their precision.
Positions are zero-based Unicode character offsets in the original expression.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from typing import Any, Callable


PLUGIN_ID = "arithmetic"
_CONFIG = {
    "precision": (50, 2, 100),
    "max_expression_length": (4096, 1, 16384),
    "max_tokens": (1024, 1, 4096),
    "max_depth": (64, 1, 128),
    "max_number_digits": (128, 1, 256),
}
_MAX_ADJUSTED_EXPONENT = 512
_MAX_OPERAND_CHARACTERS = 1024
_OPERAND_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_OPERATIONS = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide"}
_TOOL_REMINDER = (
    "Arithmetic tasks MUST use arithmetic tools; do not calculate mentally and "
    "do not substitute browser_eval or any Python/JavaScript evaluation. "
    "Results are exact decimal strings where representable, otherwise rounded "
    "to the configured significant-digit precision using ROUND_HALF_EVEN. "
)


class ArithmeticFailure(Exception):
    """Expected, JSON-serializable business failure, not a host exception."""

    def __init__(self, code: str, message: str, position: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.position = position


def _canonical(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class ArithmeticEngine:
    """The same four functions serve both direct tools and expression steps."""

    def __init__(self, **settings: int):
        unknown = set(settings) - set(_CONFIG)
        if unknown:
            raise ValueError("Unknown arithmetic setting")
        for key, (default, minimum, maximum) in _CONFIG.items():
            value = settings.get(key, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{key} must be an integer in [{minimum}, {maximum}]")
            setattr(self, key, value)
        self.context = Context(
            prec=self.precision,
            rounding=ROUND_HALF_EVEN,
            Emin=-_MAX_ADJUSTED_EXPONENT,
            Emax=_MAX_ADJUSTED_EXPONENT,
            traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
        )

    def _finite(self, value: Decimal, position: int | None = None) -> Decimal:
        if not value.is_finite():
            raise ArithmeticFailure("non_finite_number", "Only finite decimal numbers are allowed", position)
        if not value.is_zero() and abs(value.adjusted()) > _MAX_ADJUSTED_EXPONENT:
            raise ArithmeticFailure("numeric_limit", "Decimal magnitude exceeds the supported bound", position)
        return value

    def number(self, raw: Any, position: int | None = None) -> Decimal:
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            raise ArithmeticFailure("invalid_arguments", "Operands must be finite numbers or decimal strings", position)
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ArithmeticFailure("non_finite_number", "Only finite decimal numbers are allowed", position)
        if isinstance(raw, Decimal) and not raw.is_finite():
            raise ArithmeticFailure("non_finite_number", "Only finite decimal numbers are allowed", position)
        if isinstance(raw, int) and raw.bit_length() > self.max_number_digits * 4:
            raise ArithmeticFailure("numeric_limit", "Operand has too many digits", position)
        if isinstance(raw, str) and len(raw) > _MAX_OPERAND_CHARACTERS:
            raise ArithmeticFailure("numeric_limit", "Operand text exceeds the supported bound", position)
        text = str(raw).strip()
        if len(text) > _MAX_OPERAND_CHARACTERS:
            raise ArithmeticFailure("numeric_limit", "Operand text exceeds the supported bound", position)
        if text.lower().lstrip("+-") in {"nan", "snan", "inf", "infinity"}:
            raise ArithmeticFailure("non_finite_number", "Only finite decimal numbers are allowed", position)
        if not _OPERAND_PATTERN.fullmatch(text):
            raise ArithmeticFailure("invalid_number", "Operand must be a decimal literal", position)
        mantissa = re.split("[eE]", text, maxsplit=1)[0]
        if sum("0" <= char <= "9" for char in mantissa) > self.max_number_digits:
            raise ArithmeticFailure("numeric_limit", "Operand has too many digits", position)
        try:
            value = Decimal(text)
        except (DecimalException, ValueError):
            raise ArithmeticFailure("invalid_number", "Invalid decimal literal", position) from None
        # Bound the exponent even for zero, where adjusted() alone is insufficient.
        if abs(value.as_tuple().exponent) > _MAX_ADJUSTED_EXPONENT:
            raise ArithmeticFailure("numeric_limit", "Decimal exponent exceeds the supported bound", position)
        return self._finite(value, position)

    def _calculate(self, operation: str, a: Decimal, b: Decimal) -> Decimal:
        self._finite(a)
        self._finite(b)
        if operation == "divide" and b.is_zero():
            raise ArithmeticFailure("division_by_zero", "Division by zero is not allowed")
        try:
            with localcontext(self.context):
                if operation == "add":
                    result = a + b
                elif operation == "subtract":
                    result = a - b
                elif operation == "multiply":
                    result = a * b
                elif operation == "divide":
                    result = a / b
                else:
                    raise ArithmeticFailure("invalid_operation", "Unknown arithmetic operation")
        except DecimalException:
            raise ArithmeticFailure("numeric_limit", "Arithmetic result exceeds decimal bounds") from None
        return self._finite(result)

    def add(self, a: Decimal, b: Decimal) -> Decimal:
        return self._calculate("add", a, b)

    def subtract(self, a: Decimal, b: Decimal) -> Decimal:
        return self._calculate("subtract", a, b)

    def multiply(self, a: Decimal, b: Decimal) -> Decimal:
        return self._calculate("multiply", a, b)

    def divide(self, a: Decimal, b: Decimal) -> Decimal:
        return self._calculate("divide", a, b)

    def apply(self, operation: str, a: Decimal, b: Decimal) -> Decimal:
        if operation not in {"add", "subtract", "multiply", "divide"}:
            raise ArithmeticFailure("invalid_operation", "Unknown arithmetic operation")
        return getattr(self, operation)(a, b)

    def parse(self, expression: str) -> dict[str, Any]:
        if not isinstance(expression, str):
            raise ArithmeticFailure("invalid_arguments", "expression must be a string")
        root = _Parser(self, expression).parse()
        steps: list[dict[str, Any]] = []
        result = self._evaluate(root, steps)
        return {
            "type": "expression",
            "syntax_type": root.kind,
            "ast": root.to_dict(),
            "result": _canonical(result),
            "steps": steps,
            "precision": self.precision,
            "rounding": "ROUND_HALF_EVEN",
        }

    def _evaluate(self, node: _Node, steps: list[dict[str, Any]]) -> Decimal:
        if node.kind == "number":
            return node.value
        if node.kind == "unary":
            a = Decimal(0)
            b = self._evaluate(node.right, steps)
        else:
            a = self._evaluate(node.left, steps)
            b = self._evaluate(node.right, steps)
        operation = _OPERATIONS[node.operator]
        try:
            result = self.apply(operation, a, b)
        except ArithmeticFailure as error:
            if error.position is None:
                error.position = node.position
            raise
        steps.append({
            "index": len(steps) + 1,
            "type": node.kind,
            "operation": operation,
            "operator": node.operator,
            "operands": [_canonical(a), _canonical(b)],
            "result": _canonical(result),
            "position": node.position,
        })
        return result


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    position: int


@dataclass(frozen=True)
class _Node:
    kind: str
    position: int
    depth: int = 1
    value: Decimal | None = None
    operator: str | None = None
    left: _Node | None = None
    right: _Node | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind, "position": self.position}
        if self.kind == "number":
            result["value"] = _canonical(self.value)
        elif self.kind == "unary":
            result.update(operator=self.operator, operand=self.right.to_dict())
        else:
            result.update(operator=self.operator, left=self.left.to_dict(), right=self.right.to_dict())
        return result


class _Parser:
    """expression := term ((+|-) term)*; term := unary ((*|/) unary)*."""

    def __init__(self, engine: ArithmeticEngine, expression: str):
        self.engine = engine
        self.expression = expression
        self.tokens = self._tokenize()
        self.index = 0

    def _tokenize(self) -> list[_Token]:
        text = self.expression
        if len(text) > self.engine.max_expression_length:
            raise ArithmeticFailure("expression_too_long", "Expression exceeds the character limit", self.engine.max_expression_length)
        tokens: list[_Token] = []
        position = 0
        while position < len(text):
            char = text[position]
            if char.isspace():
                position += 1
                continue
            start = position
            if "0" <= char <= "9" or char == ".":
                digits = 0
                while position < len(text) and "0" <= text[position] <= "9":
                    digits += 1
                    position += 1
                if position < len(text) and text[position] == ".":
                    position += 1
                    while position < len(text) and "0" <= text[position] <= "9":
                        digits += 1
                        position += 1
                if not digits:
                    raise ArithmeticFailure("syntax_error", "A decimal point must have digits", start)
                if digits > self.engine.max_number_digits:
                    raise ArithmeticFailure("numeric_limit", "Numeric literal has too many digits", start)
                token = _Token("number", text[start:position], start)
            elif char in "+-*/()×÷":
                token = _Token({"×": "*", "÷": "/"}.get(char, char), char, start)
                position += 1
            else:
                raise ArithmeticFailure("invalid_token", "Only decimal numbers, whitespace, parentheses and arithmetic operators are allowed", start)
            tokens.append(token)
            if len(tokens) > self.engine.max_tokens:
                raise ArithmeticFailure("complexity_limit", "Expression exceeds the token limit", start)
        tokens.append(_Token("end", "", len(text)))
        return tokens

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _take(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _check_depth(self, depth: int, position: int) -> None:
        if depth > self.engine.max_depth:
            raise ArithmeticFailure("complexity_limit", "Expression exceeds the nesting or syntax-tree depth limit", position)

    def _binary(self, operator: _Token, left: _Node, right: _Node) -> _Node:
        depth = max(left.depth, right.depth) + 1
        self._check_depth(depth, operator.position)
        return _Node("binary", operator.position, depth, operator=operator.kind, left=left, right=right)

    def parse(self) -> _Node:
        node = self._expression(0)
        if self.current.kind != "end":
            raise ArithmeticFailure("syntax_error", "Unexpected token after expression", self.current.position)
        return node

    def _expression(self, nesting: int) -> _Node:
        node = self._term(nesting)
        while self.current.kind in {"+", "-"}:
            operator = self._take()
            node = self._binary(operator, node, self._term(nesting))
        return node

    def _term(self, nesting: int) -> _Node:
        node = self._unary(nesting)
        while self.current.kind in {"*", "/"}:
            operator = self._take()
            node = self._binary(operator, node, self._unary(nesting))
        return node

    def _unary(self, nesting: int) -> _Node:
        self._check_depth(nesting, self.current.position)
        if self.current.kind in {"+", "-"}:
            operator = self._take()
            operand = self._unary(nesting + 1)
            depth = operand.depth + 1
            self._check_depth(depth, operator.position)
            return _Node("unary", operator.position, depth, operator=operator.kind, right=operand)
        return self._primary(nesting)

    def _primary(self, nesting: int) -> _Node:
        token = self.current
        if token.kind == "number":
            self._take()
            return _Node("number", token.position, value=self.engine.number(token.text, token.position))
        if token.kind == "(":
            self._take()
            self._check_depth(nesting + 1, token.position)
            node = self._expression(nesting + 1)
            if self.current.kind != ")":
                raise ArithmeticFailure("syntax_error", "Expected closing parenthesis", self.current.position)
            self._take()
            return node
        raise ArithmeticFailure("syntax_error", "Expected a number, unary sign or opening parenthesis", token.position)


def _handler(engine: ArithmeticEngine, operation: str) -> Callable[..., str]:
    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        execution = {"tool": f"arithmetic_{operation}", "precision": engine.precision}
        try:
            expected = {"expression"} if operation == "parse" else {"a", "b"}
            if not isinstance(args, dict) or set(args) != expected:
                raise ArithmeticFailure("invalid_arguments", "Arguments must contain exactly the tool's declared fields")
            if operation == "parse":
                payload = engine.parse(args["expression"])
            else:
                a = engine.number(args["a"])
                b = engine.number(args["b"])
                result = engine.apply(operation, a, b)
                payload = {
                    "operation": operation,
                    "operands": [_canonical(a), _canonical(b)],
                    "result": _canonical(result),
                    "precision": engine.precision,
                    "rounding": "ROUND_HALF_EVEN",
                }
            payload["execution"] = execution
        except ArithmeticFailure as error:
            detail: dict[str, Any] = {"code": error.code, "message": error.message, "execution": execution}
            if error.position is not None:
                detail["position"] = error.position
            payload = {"error": detail}
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return handler


def register(ctx: Any) -> None:
    """Only ctx.get_config and ctx.register_tool are required by this plugin."""
    settings = {key: ctx.get_config(key, spec[0]) for key, spec in _CONFIG.items()}
    engine = ArithmeticEngine(**settings)
    operand_schema = {
        "type": ["number", "string"],
        "maxLength": _MAX_OPERAND_CHARACTERS,
        "description": "Finite decimal number; prefer a string to preserve all input digits.",
    }
    for operation in ("add", "subtract", "multiply", "divide", "parse"):
        name = f"arithmetic_{operation}"
        if operation == "parse":
            description = _TOOL_REMINDER + (
                "Safely parse and calculate a decimal expression using recursive descent, "
                "not code execution. Supports () + - * / × ÷, precedence and unary +/-; "
                "returns syntax type, AST and postorder steps using the same four arithmetic "
                "functions. No scientific notation in expressions. Errors have zero-based "
                "Unicode character positions."
            )
            properties = {"expression": {"type": "string", "maxLength": engine.max_expression_length}}
            required = ["expression"]
        else:
            description = _TOOL_REMINDER + f"Compute a {operation} b with bounded finite Decimal arithmetic."
            properties = {"a": operand_schema, "b": operand_schema}
            required = ["a", "b"]
        schema = {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        ctx.register_tool(
            name=name,
            toolset=PLUGIN_ID,
            schema=schema,
            handler=_handler(engine, operation),
            check_fn=None,
            requires_env=[],
            is_async=False,
            description=description,
            emoji="",
            override=False,
        )
