"""Recursive-descent parser for BEX's Python-style syntax."""

import re

from .lexer import BexError, tokenize

TYPES = ("float", "int", "vector", "bool", "quaternion", "matrix")
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=", "//=", "**=", "&=", "|=", "^=", "<<=", ">>=")
COMPONENTS = {"x": 0, "y": 1, "z": 2, "w": 3, "r": 0, "g": 1, "b": 2}
COMPARE_OPS = ("<", "<=", ">", ">=", "==", "!=")
KEYWORDS = ("if", "elif", "else", "for", "in", "while", "pass", "and", "or", "not", "True", "False",
            "break", "continue", "def", "return", "import", "from", "class", "lambda")
UNSUPPORTED = {"import", "from", "class", "lambda"}

GROUP_PREFIX = "group_"  # @group_top == b@top, as in Houdini

# Arithmetic precedence levels, loosest first (comparisons and and/or/not sit above these).
_ARITH = [("|",), ("^",), ("&",), ("<<", ">>"), ("+", "-"), ("*", "/", "//", "%")]
SWIZZLE = re.compile(r"[xyzw]{2,4}|[rgb]{2,3}")  # v.zyx, v.xy (a vector, z = 0), q.wxyz


class N:
    """AST node: `kind`, source `line`, plus kind-specific fields."""

    def __init__(self, kind, line, **fields):
        self.kind = kind
        self.line = line
        self.__dict__.update(fields)

    def __repr__(self):
        return f"N({self.__dict__})"


class Parser:
    def __init__(self, src):
        self.toks = tokenize(src)
        self.i = 0

    # -- token helpers -----------------------------------------------------
    def peek(self, offset=0):
        return self.toks[min(self.i + offset, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def at(self, val, offset=0):
        t = self.peek(offset)
        return t.kind in ("op", "ident") and t.val == val

    def accept(self, val):
        if self.at(val):
            return self.next()
        return None

    def expect(self, val):
        if not self.at(val):
            t = self.peek()
            raise BexError(f"expected '{val}' but found {self._text(t)}", t.line)
        return self.next()

    @staticmethod
    def _text(t):
        if t.kind == "eof":
            return "end of script"
        if t.kind == "newline":
            return "end of line"
        if t.kind in ("indent", "dedent"):
            return "a change of indentation"
        if t.kind == "attr":
            return repr("@" + t.val[0])
        return repr(str(t.val))

    def end_of_statement(self):
        t = self.peek()
        if t.kind == "newline":
            self.next()
        elif t.kind not in ("eof", "dedent"):
            prev = self.toks[self.i - 1]
            raise BexError(f"expected the end of the line but found {self._text(t)}", prev.line)

    # -- statements --------------------------------------------------------
    def program(self):
        stmts = []
        while self.peek().kind != "eof":
            if self.peek().kind == "indent":
                raise BexError("unexpected indentation", self.peek().line)
            stmts.extend(self.statement())
        return stmts

    def statement(self):
        """One line (or compound statement). Returns a list: `a = 1; b = 2` is two."""
        t = self.peek()
        if self.at("if"):
            return [self.if_stmt()]
        if self.at("def"):
            return [self.def_stmt()]
        if self.at("for"):
            self.next()
            var = self.next()
            if var.kind != "ident" or var.val in KEYWORDS:
                raise BexError("expected a loop variable name after 'for'", var.line)
            self.expect("in")
            r = self.peek()
            if not (r.kind == "ident" and r.val == "range" and self.at("(", 1)):
                # for x in (1, 2, 3): / for c in @P: a range() loop picking one item or component a pass
                return self.foreach(t, var.val, self.expr())
            self.next()
            args = self.call_args()
            if not 1 <= len(args) <= 3:
                raise BexError(f"range() takes 1 to 3 arguments, got {len(args)}", r.line)
            return [N("for", t.line, var=var.val, args=args, body=self.block(t))]
        if self.at("while"):
            self.next()
            cond = self.expr()
            return [N("while", t.line, cond=cond, body=self.block(t))]
        if self.at("elif") or self.at("else"):
            raise BexError(f"'{t.val}' without an 'if' above it at the same indentation", t.line)
        stmts = [self.simple()]
        while self.accept(";"):
            if self.peek().kind in ("newline", "eof", "dedent"):
                break
            stmts.append(self.simple())
        self.end_of_statement()
        return stmts

    def foreach(self, t, var, seq):
        """`for x in seq:` as `for $iN in range(len): x = item $iN`, so loops need nothing new."""
        self.loop_count = getattr(self, "loop_count", 0) + 1
        i = N("var", t.line, name=f"$i{self.loop_count}")
        before = []
        if seq.kind == "tuple" or (seq.kind == "call" and seq.name == "set"):
            items = seq.items if seq.kind == "tuple" else seq.args
            count, pick = N("num", t.line, value=len(items)), N("call", t.line, name="$pick", args=items + [i])
        else:  # a vector or quaternion: its components
            held = N("var", t.line, name=f"$seq{self.loop_count}")
            before = [N("assign", t.line, target=held, op="=", value=seq, annot=None)]
            count, pick = N("call", t.line, name="$len", args=[held]), N("index", t.line, obj=held, index=i)
        body = self.block(t)
        body.body.insert(0, N("assign", t.line, target=N("var", t.line, name=var), op="=", value=pick, annot=None))
        return before + [N("for", t.line, var=i.name, args=[count], body=body)]

    def if_stmt(self):
        t = self.next()  # 'if' or 'elif'
        cond = self.expr()
        then = self.block(t)
        orelse = None
        if self.at("elif"):
            orelse = N("block", self.peek().line, body=[self.if_stmt()])
        elif self.at("else"):
            e = self.next()
            orelse = self.block(e)
        return N("if", t.line, cond=cond, then=then, orelse=orelse)

    def block(self, head):
        """`:` then an indented block, or statements on the same line (`if x: y = 1`)."""
        self.expect(":")
        if self.peek().kind != "newline":
            return N("block", head.line, body=self.statement())
        self.next()
        if self.peek().kind != "indent":
            raise BexError(f"expected an indented block after '{head.val}:'", self.peek().line)
        self.next()
        body = []
        while self.peek().kind not in ("dedent", "eof"):
            body.extend(self.statement())
        self.next()
        return N("block", head.line, body=body)

    def simple(self):
        """Assignment, `name: type = value`, `pass`, `break`, `continue`, or a bare expression."""
        t = self.peek()
        if t.kind == "ident" and t.val in UNSUPPORTED:
            raise BexError(f"'{t.val}' is not supported in BEX", t.line)
        if self.accept("pass"):
            return N("block", t.line, body=[])
        for word in ("break", "continue"):
            if self.accept(word):
                return N(word, t.line)
        if self.accept("return"):
            ends = self.peek().kind in ("newline", "eof", "dedent") or self.at(";")
            return N("return", t.line, value=None if ends else self.expr_list())
        if t.kind == "ident" and t.val in TYPES and self.peek(1).kind == "ident" and not self.at("(", 1):
            raise BexError(f"no declarations needed: write {self.peek(1).val} = ... "
                           f"(or {self.peek(1).val}: {t.val} = ... to fix the type)", t.line)
        if t.kind == "ident" and self.at(":", 1):  # x: float = 1
            self.next()
            self.next()
            ty = self.next()
            if ty.kind != "ident" or ty.val not in TYPES:
                raise BexError(f"expected a type ({', '.join(TYPES)}) after '{t.val}:'", ty.line)
            self.expect("=")
            return N("assign", t.line, target=N("var", t.line, name=t.val), op="=", value=self.expr(), annot=ty.val)
        e = self.expr_list()
        op = self.peek()
        if op.kind == "op" and op.val in ASSIGN_OPS:
            self.next()
            if e.kind == "tuple":  # a, b = b, a
                if op.val != "=":
                    raise BexError(f"'{op.val}' needs a single target, not several", op.line)
                for item in e.items:
                    self._check_lvalue(item)
            else:
                self._check_lvalue(e)
            return N("assign", t.line, target=e, op=op.val, value=self.expr_list(), annot=None)
        if e.kind == "tuple":
            raise BexError("several values on their own do nothing (to set several variables: a, b = 1, 2)", t.line)
        return N("expr", t.line, expr=e)

    def expr_list(self):
        """`a` or `a, b, c` (a tuple without brackets, as in `return a, b` or `a, b = b, a`)."""
        e = self.expr()
        if not self.at(","):
            return e
        items = [e]
        while self.accept(","):
            if self.peek().kind in ("newline", "eof", "dedent") or self.at("=") or self.at(";"):
                break
            items.append(self.expr())
        return N("tuple", e.line, items=items)

    def _check_lvalue(self, e):
        target = e
        while target.kind in ("member", "index"):  # v.x, m[3][0]
            target = target.obj
        if target.kind not in ("var", "attr"):
            raise BexError("can only assign to a variable, an @attribute or one of its components", e.line)

    # -- expressions -------------------------------------------------------
    def expr(self):
        """`a if cond else b`, or a plain or-expression."""
        a = self.or_expr()
        if self.at("if"):
            line = self.next().line
            cond = self.or_expr()
            self.expect("else")
            return N("ternary", line, cond=cond, a=a, b=self.expr())
        return a

    def or_expr(self):
        left = self.and_expr()
        while self.at("or"):
            line = self.next().line
            left = N("binop", line, op="or", a=left, b=self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.at("and"):
            line = self.next().line
            left = N("binop", line, op="and", a=left, b=self.not_expr())
        return left

    def not_expr(self):
        if self.at("not"):
            line = self.next().line
            return N("unop", line, op="not", a=self.not_expr())
        return self.comparison()

    def comparison(self):
        """Chains like `0 < x <= 1` mean `0 < x and x <= 1`, as in Python."""
        left = self.arith(0)
        result = None
        while (self.peek().kind == "op" and self.peek().val in COMPARE_OPS) or self.at("in") or \
                (self.at("not") and self.at("in", 1)):
            op = self.next()
            if op.val == "not":
                self.next()
                op.val = "not in"
            right = self.arith(0)
            cmp = N("binop", op.line, op=op.val, a=left, b=right)
            result = cmp if result is None else N("binop", op.line, op="and", a=result, b=cmp)
            left = right
        return left if result is None else result

    def arith(self, level):
        if level == len(_ARITH):
            return self.unary()
        left = self.arith(level + 1)
        while self.peek().kind == "op" and self.peek().val in _ARITH[level]:
            op = self.next()
            left = N("binop", op.line, op=op.val, a=left, b=self.arith(level + 1))
        return left

    def unary(self):
        t = self.peek()
        if t.kind == "op" and t.val in ("-", "+", "~"):
            self.next()
            operand = self.unary()
            return operand if t.val == "+" else N("unop", t.line, op=t.val, a=operand)
        return self.power()

    def power(self):
        base = self.postfix()
        if self.at("**"):
            line = self.next().line
            return N("binop", line, op="**", a=base, b=self.unary())  # right-assoc, -x binds looser
        return base

    def postfix(self):
        e = self.primary()
        while True:
            if self.at("."):
                line = self.next().line
                name = self.next()
                if name.kind == "num" and name.val in (0, 1, 2, 3):  # @P.1 == @P.y == @P[1]
                    comp = name.val
                elif name.kind == "ident" and name.val in COMPONENTS:
                    comp = COMPONENTS[name.val]
                elif name.kind == "ident" and SWIZZLE.fullmatch(name.val):
                    e = N("swizzle", line, obj=e, comps=[COMPONENTS[c] for c in name.val])
                    continue
                else:
                    raise BexError("expected a component (.x .y .z .w, .r .g .b or .0 .1 .2 .3)", line)
                e = N("member", line, obj=e, comp=comp)
            elif self.at("["):
                line = self.next().line
                idx = self.expr()
                self.expect("]")
                lit = idx.a if idx.kind == "unop" and idx.op == "-" else idx
                if lit.kind == "num":  # v[1], v[-1]: a fixed component (the compiler checks the size)
                    k = -lit.value if lit is not idx else lit.value
                    if not isinstance(k, int) or not -4 <= k <= 3:
                        raise BexError("component index must be 0, 1, 2 (3 for a quaternion), or negative", line)
                    e = N("member", line, obj=e, comp=k)
                else:  # v[i]: chosen when the script runs
                    e = N("index", line, obj=e, index=idx)
            else:
                return e

    def call_args(self):
        """`(a, b, name=c)`: expressions, keyword arguments as `kwarg` nodes."""
        self.expect("(")
        args = []
        while not self.at(")"):
            t = self.peek()
            if t.kind == "ident" and self.at("=", 1):
                self.next()
                self.next()
                args.append(N("kwarg", t.line, name=t.val, value=self.expr()))
            elif args and args[-1].kind == "kwarg":
                raise BexError("positional arguments go before keyword arguments", t.line)
            else:
                args.append(self.expr())
            if not self.accept(","):
                break
        self.expect(")")
        return args

    def def_stmt(self):
        """`def name(a, b=1):` and an indented body."""
        t = self.next()
        name = self.next()
        if name.kind != "ident" or name.val in KEYWORDS:
            raise BexError("expected a function name after 'def'", name.line)
        self.expect("(")
        params, seen = [], set()
        while not self.at(")"):
            p = self.next()
            if p.kind != "ident" or p.val in KEYWORDS:
                raise BexError("expected a parameter name", p.line)
            if p.val in seen:
                raise BexError(f"parameter '{p.val}' appears twice", p.line)
            seen.add(p.val)
            default = self.expr() if self.accept("=") else None
            if default is None and params and params[-1][1] is not None:
                raise BexError(f"parameter '{p.val}' needs a default, like the ones before it", p.line)
            params.append((p.val, default))
            if not self.accept(","):
                break
        self.expect(")")
        return N("def", t.line, name=name.val, params=params, body=self.block(t))

    def primary(self):
        t = self.peek()
        if t.kind == "num":
            self.next()
            return N("num", t.line, value=t.val)
        if t.kind == "str":
            self.next()
            return N("str", t.line, value=t.val)
        if t.kind == "attr":
            self.next()
            name, type, storage = t.val
            if name.startswith(GROUP_PREFIX) and len(name) > len(GROUP_PREFIX):
                # Houdini spelling: @group_top is the boolean attribute "top".
                if type not in (None, "bool"):
                    raise BexError(f"@{name} is a group, i.e. a bool attribute", t.line)
                name, type = name[len(GROUP_PREFIX):], "bool"
            return N("attr", t.line, name=name, type=type, storage=storage)
        if t.kind == "ident":
            if t.val in ("True", "False"):
                self.next()
                return N("bool", t.line, value=t.val == "True")
            if t.val in ("true", "false"):
                raise BexError(f"write {t.val.capitalize()} (capitalised, as in Python)", t.line)
            if self.at("(", 1):
                self.next()
                return N("call", t.line, name=t.val, args=self.call_args())
            if t.val in TYPES or t.val in KEYWORDS:
                raise BexError(f"unexpected '{t.val}'", t.line)
            self.next()
            return N("var", t.line, name=t.val)
        if t.kind == "op" and t.val in ("(", "["):
            close = ")" if t.val == "(" else "]"
            self.next()
            items, comma = [self.expr()], False
            while self.accept(","):
                comma = True
                if self.at(close):
                    break
                items.append(self.expr())
            self.expect(close)
            if t.val == "(" and len(items) == 1 and not comma:  # (1,) is a tuple, as in Python
                return items[0]
            if len(items) not in (3, 4):  # only usable in `x in (...)`, `for x in (...)`, `a, b = (...)`
                return N("tuple", t.line, items=items)
            return N("call", t.line, name="set", args=items)
        self.next()
        raise BexError(f"unexpected {self._text(t)}", t.line)


def parse(src):
    return Parser(src).program()


# One word of a Houdini group pattern: an optional ! (not) or ^ (remove), then
# * (all), an element number or range (5, 0-9, 0-100:2), a group (bool
# attribute) name, or an ad-hoc expression without spaces (@P.y>0, b@top).
_PATTERN_WORD = re.compile(r"([!^]?)(\*|\d+(?:-\d+(?::\d+)?)?|[A-Za-z_]\w*|[A-Za-z]?@\S+)")
_RANGE = re.compile(r"(\d+)(?:-(\d+)(?::(\d+))?)?")


def parse_group(src):
    """The wrangle's Group field: a Houdini-style pattern, else a BEX expression.

    A pattern is space-separated words, read left to right: `top bottom` is the
    union, `!top` adds everything not in top, `^top` removes top from what came
    before (`* ^top`), `*` is every element, `0-9` / `0-100:2` are element numbers.
    """
    words = src.split()
    matches = [_PATTERN_WORD.fullmatch(w) for w in words]
    if not words or not all(matches) or any(m.group(2) in KEYWORDS for m in matches):
        return parse_expression(src)
    result = None
    for m in matches:
        sign, body = m.groups()
        term = _pattern_term(body)
        if sign == "^":
            result = N("bool", 1, value=False) if result is None else \
                N("binop", 1, op="and", a=result, b=N("unop", 1, op="not", a=term))
            continue
        if sign == "!":
            term = N("unop", 1, op="not", a=term)
        result = term if result is None else N("binop", 1, op="or", a=result, b=term)
    return result


def _pattern_term(body):
    if body == "*":
        return N("bool", 1, value=True)
    if "@" in body:
        return parse_expression(body)
    rng = _RANGE.fullmatch(body)
    if rng is None:
        return N("var", 1, name=body)  # a group: in the Group field a bare name is a bool attribute
    index = N("attr", 1, name="elemnum", type=None)
    first, last, step = rng.groups()
    num = lambda v: N("num", 1, value=int(v))  # noqa: E731
    if last is None:
        return N("binop", 1, op="==", a=index, b=num(first))
    lo, hi = sorted((int(first), int(last)))
    term = N("binop", 1, op="and", a=N("binop", 1, op=">=", a=index, b=num(lo)),
             b=N("binop", 1, op="<=", a=index, b=num(hi)))
    if step and int(step) > 1:
        offset = N("binop", 1, op="-", a=index, b=num(first))
        every = N("binop", 1, op="==", a=N("binop", 1, op="%", a=offset, b=num(step)), b=num(0))
        term = N("binop", 1, op="and", a=term, b=every)
    return term


def parse_expression(src):
    """One expression on its own (the wrangle's Group field)."""
    p = Parser(src)
    e = p.expr()
    while p.peek().kind == "newline":
        p.next()
    t = p.peek()
    if t.kind != "eof":
        raise BexError(f"expected the end of the expression but found {p._text(t)}", t.line)
    return e
