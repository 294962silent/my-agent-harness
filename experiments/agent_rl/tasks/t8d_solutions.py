"""t8-009/010/011 的参考解法（单模块复杂度对照实验）。"""

EASY = {
    "mod/fn.py": '"""加法"""\n\n\ndef add(a, b):\n    return a + b\n',
}

MEDIUM = {
    "mod/fn.py": '''"""解析 ISO8601 时长"""

import re

_UNITS = {
    "Y": 31536000,
    "M": 2592000,
    "W": 604800,
    "D": 86400,
    "H": 3600,
    "S": 1,
}

_PATTERN = re.compile(
    r"^P(?:(?P<years>\\d+)Y)?(?:(?P<months>\\d+)M)?(?:(?P<weeks>\\d+)W)?"
    r"(?:(?P<days>\\d+)D)?(?:T(?:(?P<hours>\\d+)H)?(?:(?P<minutes>\\d+)M)?(?:(?P<seconds>\\d+)S)?)?$"
)


def parse_duration(text):
    if not isinstance(text, str) or not text.startswith("P"):
        raise ValueError(f"invalid duration: {text!r}")
    match = _PATTERN.match(text)
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    groups = match.groupdict()
    if not any(v is not None for v in groups.values()):
        raise ValueError(f"empty duration: {text!r}")

    total = 0
    if groups["years"]:
        total += int(groups["years"]) * _UNITS["Y"]
    if groups["months"]:
        total += int(groups["months"]) * _UNITS["M"]
    if groups["weeks"]:
        total += int(groups["weeks"]) * _UNITS["W"]
    if groups["days"]:
        total += int(groups["days"]) * _UNITS["D"]
    if groups["hours"]:
        total += int(groups["hours"]) * _UNITS["H"]
    if groups["minutes"]:
        total += int(groups["minutes"]) * 60
    if groups["seconds"]:
        total += int(groups["seconds"]) * _UNITS["S"]
    return total
''',
}

HARD = {
    "mod/fn.py": '''"""递归下降表达式求值"""

import re

_TOKEN = re.compile(r"\\s*(\\d+(?:\\.\\d+)?|\\*\\*|[-+*/%()])")


def _tokenize(expr):
    tokens = []
    pos = 0
    length = len(expr)
    while pos < length:
        # 跳过空白（包括结尾空白）
        while pos < length and expr[pos].isspace():
            pos += 1
        if pos >= length:
            break
        match = _TOKEN.match(expr, pos)
        if not match:
            raise ValueError(f"unexpected char at {pos}: {expr[pos]!r}")
        tokens.append(match.group(1))
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        tok = self.peek()
        if tok is None:
            raise ValueError("unexpected end of expression")
        self.pos += 1
        return tok

    def parse(self):
        value = self.expr()
        if self.pos != len(self.tokens):
            raise ValueError("trailing tokens")
        return value

    def expr(self):
        value = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self):
        value = self.unary()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            rhs = self.unary()
            if op == "*":
                value = value * rhs
            elif op == "/":
                if rhs == 0:
                    raise ZeroDivisionError("division by zero")
                value = value / rhs
            else:
                if rhs == 0:
                    raise ZeroDivisionError("modulo by zero")
                value = value % rhs
        return value

    def unary(self):
        if self.peek() == "-":
            self.take()
            return -self.unary()
        if self.peek() == "+":
            self.take()
            return self.unary()
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek() == "**":
            self.take()
            exponent = self.unary()
            return base**exponent
        return base

    def atom(self):
        tok = self.take()
        if tok == "(":
            value = self.expr()
            if self.take() != ")":
                raise ValueError("missing closing paren")
            return value
        if tok in ("+", "-", "*", "/", "%", "**", ")"):
            raise ValueError(f"unexpected token {tok!r}")
        try:
            return float(tok) if "." in tok else int(tok)
        except ValueError as exc:
            raise ValueError(f"bad number {tok!r}") from exc


def evaluate(expr):
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("empty expression")
    tokens = _tokenize(expr)
    return _Parser(tokens).parse()
''',
}

T8D_SOLUTIONS = {
    "t8-009": EASY,
    "t8-010": MEDIUM,
    "t8-011": HARD,
}
