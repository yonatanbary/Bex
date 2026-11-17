"""Recursive-descent parser for BEX's Python-style syntax."""

from .lexer import BexError, tokenize

TYPES = ("float", "int", "vector", "bool")
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=", "//=", "**=")
COMPONENTS = {"x": 0, "y": 1, "z": 2, "r": 0, "g": 1, "b": 2}
COMPARE_OPS = ("<", "<=", ">", ">=", "==", "!=")
KEYWORDS = ("if", "elif", "else", "for", "in", "while", "pass", "and", "or", "not", "True", "False",
            "break", "continue", "def", "return", "import", "from", "class", "lambda")
UNSUPPORTED = {"break", "continue", "def", "return", "import", "from", "class", "lambda"}

# Arithmetic precedence levels, loosest first (comparisons and and/or/not sit above these).
_ARITH = [("+", "-"), ("*", "/", "//", "%")]


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
        if self.at("for"):
            self.next()
            var = self.next()
            if var.kind != "ident" or var.val in KEYWORDS:
                raise BexError("expected a loop variable name after 'for'", var.line)
            self.expect("in")
            r = self.peek()
            if not (r.kind == "ident" and r.val == "range" and self.at("(", 1)):
                raise BexError("for-loops go over range(...), e.g. for i in range(8):", r.line)
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
        """Assignment, `name: type = value`, `pass`, or a bare expression."""
        t = self.peek()
        if t.kind == "ident" and t.val in UNSUPPORTED:
            raise BexError(f"'{t.val}' is not supported in BEX", t.line)
        if self.accept("pass"):
            return N("block", t.line, body=[])
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
        e = self.expr()
        op = self.peek()
        if op.kind == "op" and op.val in ASSIGN_OPS:
            self.next()
            self._check_lvalue(e)
            return N("assign", t.line, target=e, op=op.val, value=self.expr(), annot=None)
        return N("expr", t.line, expr=e)

    def _check_lvalue(self, e):
        target = e.obj if e.kind == "member" else e
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
        while self.peek().kind == "op" and self.peek().val in COMPARE_OPS:
            op = self.next()
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
        if t.kind == "op" and t.val in ("-", "+"):
            self.next()
            operand = self.unary()
            return operand if t.val == "+" else N("unop", t.line, op="-", a=operand)
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
                if name.kind != "ident" or name.val not in COMPONENTS:
                    raise BexError("expected a component (.x .y .z or .r .g .b)", line)
                e = N("member", line, obj=e, comp=COMPONENTS[name.val])
            elif self.at("["):
                line = self.next().line
                neg = self.accept("-")
                idx = self.next()
                if idx.kind != "num" or idx.val not in (0, 1, 2) or (neg and idx.val == 0):
                    raise BexError("component index must be 0, 1 or 2 (or -1, -2, -3)", line)
                self.expect("]")
                e = N("member", line, obj=e, comp=-idx.val % 3 if neg else int(idx.val))
            else:
                return e

    def call_args(self):
        self.expect("(")
        args = []
        if not self.at(")"):
            args.append(self.expr())
            while self.accept(","):
                if self.at(")"):
                    break
                args.append(self.expr())
        self.expect(")")
        return args

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
            return N("attr", t.line, name=t.val[0], type=t.val[1])
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
            items = [self.expr()]
            while self.accept(","):
                if self.at(close):
                    break
                items.append(self.expr())
            self.expect(close)
            if t.val == "(" and len(items) == 1:
                return items[0]
            if len(items) != 3:
                raise BexError("a vector needs three components: (x, y, z)", t.line)
            return N("call", t.line, name="set", args=items)
        self.next()
        raise BexError(f"unexpected {self._text(t)}", t.line)


def parse(src):
    return Parser(src).program()
