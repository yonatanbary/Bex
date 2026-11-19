"""Syntax highlighting for BEX source (no bpy).

``highlight_line`` returns spans ``(start, end, kind)`` that cover the line contiguously.
BEX is line-based like Python (``#`` comments, no block comments), so lines are independent.
"""

import re

from ..lang.compiler import CHANNELS, RAMPS, CONSTANTS as _CONSTANT_VALUES, FUNCTION_NAMES

KINDS = ("comment", "string", "number", "attr", "type", "keyword", "function",
         "channel", "constant", "operator", "punct", "ident", "text")

TYPES = frozenset({"float", "int", "vector", "bool"})
KEYWORDS = frozenset({"if", "elif", "else", "for", "in", "while", "pass", "and", "or", "not", "True", "False"})
CONSTANTS = frozenset(_CONSTANT_VALUES)

_TOKEN_RE = re.compile(
    r"""
      (?P<comment>\#.*)
    | (?P<string>"[^"]*"?|'[^']*'?)
    | (?P<text>\s+)
    | (?P<attr>[fivb]?@[A-Za-z_][A-Za-z0-9_]*)
    | (?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<operator>\*\*=|//=|\*\*|//|[+\-*/%]=|==|!=|<=|>=|[-+*/%<>=])
    | (?P<punct>[:;,.()\[\]])
    """,
    re.X,
)
_CALL_RE = re.compile(r"\s*\(")


def _ident_kind(line, word, end):
    if word in TYPES:
        return "type"
    if word in KEYWORDS:
        return "keyword"
    if word in CHANNELS or word in RAMPS:
        return "channel"
    if word in CONSTANTS:
        return "constant"
    if word in FUNCTION_NAMES and _CALL_RE.match(line, end):
        return "function"
    return "ident"


def highlight_line(line):
    """Spans ``(start, end, kind)`` for one line. Never raises."""
    spans = []

    def add(start, end, kind):
        if spans and spans[-1][2] == kind and spans[-1][1] == start:
            spans[-1] = (spans[-1][0], end, kind)
        elif end > start:
            spans.append((start, end, kind))

    pos, n = 0, len(line)
    while pos < n:
        m = _TOKEN_RE.match(line, pos)
        if m is None:
            add(pos, pos + 1, "text")
            pos += 1
            continue
        kind, end = m.lastgroup, m.end()
        if kind == "number" and m.group()[0] == "." and m.group()[1:].isdigit() and pos and \
                (line[pos - 1].isalnum() or line[pos - 1] in "_)]"):
            add(pos, pos + 1, "punct")  # @P.1 is a component, like @P.y
            add(pos + 1, end, "ident")
            pos = end
            continue
        if kind == "ident":
            kind = _ident_kind(line, m.group(), end)
        add(pos, end, kind)
        pos = end
    return spans


def highlight(lines):
    """Highlight a whole buffer: one span list per line."""
    return [highlight_line(line) for line in lines]
