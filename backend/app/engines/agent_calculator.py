"""Level 4 ``calculate_contract_pricing`` core: AST-validated arithmetic, no dynamic execution.

Allowed: numeric literals, named variables supplied in ``variables``, ``+ - * /``, unary ``+``/``-`` and parentheses.
Everything else (attribute access, calls, subscripts, ``**``, ``%``, comparisons, names not in ``variables``) is
rejected before evaluation. Length, AST size, nesting depth, variable count and result magnitude are bounded.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
import operator
import re
from typing import Any, Callable, Mapping

MAX_EXPRESSION_CHARS = 300
MAX_AST_NODES = 80
MAX_DEPTH = 12
MAX_VARIABLES = 24
MAX_ABS_VALUE = 1e13
_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,48}")

_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load, *_BINARY, *_UNARY)


class CalculatorError(ValueError):
    """The expression or variables violate the calculator grammar or bounds."""


@dataclass(frozen=True)
class CalcResult:
    value: float
    normalized_expression: str
    substituted_expression: str
    variables: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "value": self.value,
            "normalized_expression": self.normalized_expression,
            "substituted_expression": self.substituted_expression,
            "variables": dict(self.variables),
        }


def _clean_variables(variables: Mapping[str, Any] | None) -> dict[str, float]:
    if variables is None:
        return {}
    if not isinstance(variables, Mapping):
        raise CalculatorError("variables must be an object mapping names to numbers")
    if len(variables) > MAX_VARIABLES:
        raise CalculatorError(f"at most {MAX_VARIABLES} variables are allowed")
    clean: dict[str, float] = {}
    for name, value in variables.items():
        if not isinstance(name, str) or not _VAR_NAME_RE.fullmatch(name) or name.startswith("__"):
            raise CalculatorError(f"invalid variable name {name!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise CalculatorError(f"variable {name!r} must be a finite number")
        if abs(float(value)) > MAX_ABS_VALUE:
            raise CalculatorError(f"variable {name!r} exceeds the magnitude bound {MAX_ABS_VALUE:g}")
        clean[name] = float(value)
    return clean


def _depth(node: ast.AST, level: int = 0) -> int:
    if level > MAX_DEPTH:
        raise CalculatorError(f"expression nesting exceeds depth {MAX_DEPTH}")
    children = list(ast.iter_child_nodes(node))
    return max((_depth(c, level + 1) for c in children), default=level)


def _validate_tree(tree: ast.AST, variables: Mapping[str, float]) -> None:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise CalculatorError(f"expression exceeds {MAX_AST_NODES} AST nodes")
    _depth(tree)
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise CalculatorError(f"forbidden syntax: {type(node).__name__}")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise CalculatorError(f"only numeric literals are allowed, got {type(node.value).__name__}")
        if isinstance(node, ast.Name) and node.id not in variables:
            raise CalculatorError(f"unknown variable {node.id!r} (pass it in variables)")


def _evaluate(node: ast.AST, variables: Mapping[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return variables[node.id]
    if isinstance(node, ast.UnaryOp):
        return _UNARY[type(node.op)](_evaluate(node.operand, variables))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, variables), _evaluate(node.right, variables)
        if isinstance(node.op, ast.Div) and right == 0:
            raise CalculatorError("division by zero")
        value = _BINARY[type(node.op)](left, right)
        if not math.isfinite(value) or abs(value) > MAX_ABS_VALUE:
            raise CalculatorError(f"intermediate value exceeds the magnitude bound {MAX_ABS_VALUE:g}")
        return value
    raise CalculatorError(f"forbidden syntax: {type(node).__name__}")


def _substitute(tree: ast.Expression, variables: Mapping[str, float]) -> str:
    class _Sub(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 - ast API
            return ast.copy_location(ast.Constant(variables[node.id]), node)

    return ast.unparse(_Sub().visit(ast.parse(ast.unparse(tree), mode="eval")))


def evaluate_expression(expression: str, variables: Mapping[str, Any] | None = None) -> CalcResult:
    """Validate then evaluate ``expression``; raises ``CalculatorError`` for anything outside the grammar/bounds."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculatorError(f"expression exceeds {MAX_EXPRESSION_CHARS} characters")
    clean = _clean_variables(variables)
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"invalid arithmetic syntax: {exc.msg}") from exc
    _validate_tree(tree, clean)
    value = _evaluate(tree, clean)
    used = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})
    return CalcResult(
        value=round(value, 6),
        normalized_expression=ast.unparse(tree),
        substituted_expression=_substitute(tree, clean),
        variables=tuple((name, clean[name]) for name in used),
    )


__all__ = ["CalcResult", "CalculatorError", "MAX_EXPRESSION_CHARS", "evaluate_expression"]
