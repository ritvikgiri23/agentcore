import ast
import math
import operator
from collections.abc import Callable
from typing import Annotated

from pydantic import Field

from app.tools.registry import tool

MAX_EXPRESSION_LENGTH = 200
MAX_EXPONENT = 1000
# Bounds every intermediate result, so chained operations cannot exhaust the worker.
MAX_RESULT_DIGITS = 1000
_LOG10_2 = math.log10(2)

type Number = int | float

_BINARY_OPS: dict[type[ast.operator], Callable[[Number, Number], Number]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


class CalculationError(ValueError):
    pass


@tool
def calculator(
    expression: Annotated[
        str,
        Field(
            description="Arithmetic expression using numbers, + - * / // % ** and parentheses",
            max_length=MAX_EXPRESSION_LENGTH,
        ),
    ],
) -> str:
    """Evaluate an arithmetic expression exactly. Use this instead of doing maths yourself."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"Invalid expression: {exc.msg}") from None
    result = _evaluate(tree.body)
    if isinstance(result, int):
        return str(result)
    if not math.isfinite(result):
        raise CalculationError("Result too large")
    return format(result, ".12g")


def _evaluate(node: ast.expr) -> Number:
    """Walk a whitelist of AST nodes; anything else is rejected, never executed."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalculationError(f"Unsupported constant: {node.value!r}")
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise CalculationError("Number too large")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand)
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left), _evaluate(node.right)
        try:
            if isinstance(node.op, ast.Pow):
                result = _power(left, right)
            elif (op := _BINARY_OPS.get(type(node.op))) is not None:
                result = op(left, right)
            else:
                raise CalculationError(f"Unsupported operator: {type(node.op).__name__}")
        except ZeroDivisionError:
            raise CalculationError("Division by zero") from None
        except OverflowError:
            raise CalculationError("Result too large") from None
        if isinstance(result, int) and abs(result).bit_length() * _LOG10_2 > MAX_RESULT_DIGITS:
            raise CalculationError("Result too large")
        return result
    raise CalculationError(f"Unsupported syntax: {type(node).__name__}")


def _power(base: Number, exponent: Number) -> Number:
    if abs(exponent) > MAX_EXPONENT:
        raise CalculationError(f"Exponent too large (limit {MAX_EXPONENT})")
    # Estimate before computing: building a huge power is itself the expensive part.
    if abs(base) not in (0, 1) and abs(exponent) * abs(math.log10(abs(base))) > MAX_RESULT_DIGITS:
        raise CalculationError("Result too large")
    result = base**exponent
    if isinstance(result, complex):
        raise CalculationError("Result is not a real number")
    return result
