"""Edge-condition language. Hand-written tokenizer and parser; no eval.

    expr    := and_expr ("or" and_expr)*
    and_expr:= compare ("and" compare)*
    compare := operand OP operand          OP: == != < <= > >=
    operand := identifier | number | "string" | 'string' | true | false | null

Ordering operators need two numbers or two strings. Unknown variables, type
mismatches and any other syntax are errors.
"""

import re

from .errors import FlowstateError

KEYWORDS = {"and", "or", "true", "false", "null"}
OPS = ("==", "!=", ">=", "<=", ">", "<")
TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<dq>"(?:[^"\\]|\\.)*")
  | (?P<sq>'(?:[^'\\]|\\.)*')
  | (?P<op>==|!=|>=|<=|>|<)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


class ConditionError(FlowstateError):
    def __init__(self, message: str, expr: str):
        super().__init__("condition_error", message, {"condition": expr})


def _unquote(raw: str) -> str:
    body = raw[1:-1]
    return re.sub(r"\\(.)", lambda m: m.group(1), body)


def _tokens(expr: str) -> list[tuple[str, object]]:
    out, pos = [], 0
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m:
            raise ConditionError(f"unexpected character {expr[pos]!r} at {pos}", expr)
        pos = m.end()
        kind = m.lastgroup
        text = m.group(kind)
        if kind == "ws":
            continue
        if kind == "num":
            out.append(("lit", float(text) if "." in text else int(text)))
        elif kind in ("dq", "sq"):
            out.append(("lit", _unquote(text)))
        elif kind == "op":
            out.append(("op", text))
        elif text in ("and", "or"):
            out.append((text, text))
        elif text in ("true", "false", "null"):
            out.append(("lit", {"true": True, "false": False, "null": None}[text]))
        else:
            out.append(("var", text))
    return out


def parse(expr: str):
    toks = _tokens(expr)
    if not toks:
        raise ConditionError("empty condition", expr)
    pos = 0

    def operand():
        nonlocal pos
        if pos >= len(toks) or toks[pos][0] not in ("var", "lit"):
            raise ConditionError("expected a variable or literal", expr)
        tok = toks[pos]
        pos += 1
        return tok

    def compare():
        nonlocal pos
        left = operand()
        if pos >= len(toks) or toks[pos][0] != "op":
            raise ConditionError("expected a comparison operator", expr)
        op = toks[pos][1]
        pos += 1
        return ("cmp", op, left, operand())

    def chain(kind, sub):
        nonlocal pos
        node = sub()
        while pos < len(toks) and toks[pos][0] == kind:
            pos += 1
            node = (kind, node, sub())
        return node

    tree = chain("or", lambda: chain("and", compare))
    if pos != len(toks):
        raise ConditionError(f"unexpected token {toks[pos][1]!r}", expr)
    return tree


def identifiers(tree) -> set[str]:
    if tree[0] == "cmp":
        return {t[1] for t in tree[2:] if t[0] == "var"}
    return identifiers(tree[1]) | identifiers(tree[2])


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evaluate(tree, variables: dict, expr: str = "") -> bool:
    kind = tree[0]
    if kind == "and":
        return evaluate(tree[1], variables, expr) and evaluate(tree[2], variables, expr)
    if kind == "or":
        return evaluate(tree[1], variables, expr) or evaluate(tree[2], variables, expr)

    def value(tok):
        if tok[0] == "lit":
            return tok[1]
        if tok[1] not in variables:
            raise ConditionError(f"unknown variable {tok[1]!r}", expr)
        return variables[tok[1]]

    _, op, left_tok, right_tok = tree
    left, right = value(left_tok), value(right_tok)
    if op == "==":
        return left == right and type(left) is type(right) or (_is_number(left) and _is_number(right) and left == right)
    if op == "!=":
        return not evaluate(("cmp", "==", ("lit", left), ("lit", right)), variables, expr)
    if not ((_is_number(left) and _is_number(right)) or (isinstance(left, str) and isinstance(right, str))):
        raise ConditionError(f"cannot compare {type(left).__name__} {op} {type(right).__name__}", expr)
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
