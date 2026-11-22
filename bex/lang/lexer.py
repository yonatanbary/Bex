import re
from dataclasses import dataclass


class BexError(Exception):
    def __init__(self, msg, line=0):
        super().__init__(msg)
        self.msg = msg
        self.line = line

    def __str__(self):
        return f"line {self.line}: {self.msg}" if self.line else self.msg


@dataclass
class Token:
    kind: str  # num | ident | attr | str | op | newline | indent | dedent | eof
    val: object
    line: int


_TOKEN_RE = re.compile(
    r"""
      (?P<ws>[ \t\r]+)
    | (?P<nl>\n)
    | (?P<comment>\#[^\n]*)
    | (?P<cont>\\[ \t\r]*\n)
    | (?P<attr>[fivbpu34]?@[A-Za-z_]\w*)
    | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<ident>[A-Za-z_]\w*)
    | (?P<str>"[^"\n]*"|'[^'\n]*')
    | (?P<op>\*\*=|//=|\*\*|//|[+\-*/%]=|==|!=|<=|>=|[-+*/%<>=:;,.()\[\]])
    | (?P<bad>&&|\|\||\+\+|!|\?|[{}]|.)
    """,
    re.X,
)

_KEYWORDS = frozenset({"if", "elif", "else", "for", "in", "while", "and", "or", "not", "return"})
# p@ is a quaternion (Houdini's vector4 orient), 3@ / 4@ a matrix (always 4x4 in Blender),
# u@ a vector kept as a 2D vector (Blender's UV maps).
_PREFIX_TYPES = {"f": "float", "i": "int", "v": "vector", "b": "bool", "p": "quaternion", "3": "matrix",
                 "4": "matrix", "u": "vector"}
# Houdini attribute names -> the names the rest of Blender reads.
ALIASES = {"v": "velocity", "pscale": "radius", "uv": "UVMap"}
# TODO: s@ string attributes. Blender 5.2 and 5.3 list STRING for Store / Named Attribute
# but refuse it, so geometry nodes can't hold text per element yet. See BLENDER_VERSIONS.md.

# C/VEX habits -> what to write instead.
_HINTS = {
    "&&": "use 'and' instead of '&&'",
    "||": "use 'or' instead of '||'",
    "!": "use 'not' instead of '!'",
    "++": "use '+= 1' instead of '++'",
    "?": "use 'a if condition else b' instead of '? :'",
    "{": "blocks are indented after a ':' (no braces); a vector is (x, y, z)",
    "}": "blocks are indented after a ':' (no braces)",
}
_OPEN, _CLOSE = "([", ")]"
_WS_RE = re.compile(r"[ \t\r]*")


def _indent_width(ws):
    return sum(4 if c == "\t" else 1 for c in ws if c != "\r")


def _is_component(text, src, start, toks):
    """`.1` written right after a name, attribute, `)` or `]` picks a component."""
    if not (text[0] == "." and text[1:].isdigit()) or not toks or start == 0 or src[start - 1] in " \t\r\n":
        return False
    prev = toks[-1]
    if prev.kind == "ident":
        return prev.val not in _KEYWORDS  # `else.5` is still else 0.5
    return prev.kind == "attr" or (prev.kind == "op" and prev.val in (")", "]"))


def tokenize(src):
    """Python-style tokens: a `newline` ends each logical line, and `indent` /
    `dedent` open and close blocks. Inside brackets, line breaks are ignored."""
    toks = []
    indents = [0]
    opened = []  # (bracket, line) still open; line breaks inside them are ignored
    line = 1
    pos, n = 0, len(src)
    at_line_start = True

    def end_line():
        if toks and toks[-1].kind not in ("newline", "indent", "dedent"):
            toks.append(Token("newline", None, toks[-1].line))

    while pos < n:
        if at_line_start and not opened:
            m = _WS_RE.match(src, pos)
            ws, rest = m.group(), m.end()
            if rest >= n or src[rest] in "\n#":  # blank or comment-only line: no tokens
                nl = src.find("\n", rest)
                if nl < 0:
                    break
                pos, line = nl + 1, line + 1
                continue
            width = _indent_width(ws)
            if width > indents[-1]:
                indents.append(width)
                toks.append(Token("indent", None, line))
            while width < indents[-1]:
                indents.pop()
                toks.append(Token("dedent", None, line))
            if width != indents[-1]:
                raise BexError("this line's indentation does not match any block above it", line)
            pos, at_line_start = rest, False
            continue
        m = _TOKEN_RE.match(src, pos)
        kind, text = m.lastgroup, m.group()
        pos = m.end()
        if kind == "nl":
            if not opened:
                end_line()
                at_line_start = True
            line += 1
        elif kind == "cont":
            line += 1
        elif kind in ("ws", "comment"):
            pass
        elif kind == "bad":
            raise BexError(_HINTS.get(text, f"unexpected character {text!r}"), line)
        elif kind == "num" and _is_component(text, src, m.start(), toks):
            toks.append(Token("op", ".", line))  # @P.1 is @P[1], not @P followed by 0.1
            toks.append(Token("num", int(text[1:]), line))
        elif kind == "num":
            is_float = any(c in text for c in ".eE")
            toks.append(Token("num", float(text) if is_float else int(text), line))
        elif kind == "attr":
            prefix, name = text.split("@")
            storage = "FLOAT2" if prefix == "u" else None
            toks.append(Token("attr", (ALIASES.get(name, name), _PREFIX_TYPES.get(prefix), storage), line))
        elif kind == "str":
            toks.append(Token("str", text[1:-1], line))
        else:
            if text in _OPEN:
                opened.append((text, line))
            elif text in _CLOSE and opened:
                opened.pop()
            toks.append(Token(kind, text, line))
    if opened:
        bracket, at = opened[-1]
        raise BexError(f"'{bracket}' is never closed", at)
    end_line()
    for _ in indents[1:]:
        toks.append(Token("dedent", None, line))
    toks.append(Token("eof", None, line))
    return toks
