"""Inline autocomplete for the on-node editor (no bpy).

``suggest()`` returns the text that would complete the word before the cursor
with the best match, or "" when there is nothing to add. The editor shows it as
ghost text after the cursor; each typed letter asks again, and Tab accepts it.
"""

import re

from ..lang.compiler import BUILTINS, CHANNELS, COUNT_ATTRS, FUNCTION_NAMES, KNOWN_TYPES
from .buffer import is_word_char
from .highlight import CONSTANTS, KEYWORDS, TYPES, highlight

_ATTR_RE = re.compile(r"([fivb]?)@([A-Za-z_]\w*)$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*$")
_SCRIPT_ATTR_RE = re.compile(r"[fivb]?@([A-Za-z_]\w*)")

# Language names, in the order they are offered when the script itself has no match.
_LANGUAGE = sorted(set(FUNCTION_NAMES) | set(CHANNELS) | KEYWORDS | TYPES | CONSTANTS,
                   key=lambda n: (len(n), n))
_BUILTIN_ATTRS = list(BUILTINS) + list(COUNT_ATTRS) + [n for n in KNOWN_TYPES if n not in BUILTINS]


def _script_words(lines, row, col):
    """Identifiers and attribute names used in the script, most used first,
    leaving out the word being typed. Comments and strings are skipped."""
    idents, attrs = {}, {}
    for r, spans in enumerate(highlight(lines)):
        line = lines[r]
        for start, end, kind in spans:
            if r == row and start < col <= end:
                continue  # the word being typed
            word = line[start:end]
            if kind in ("ident", "function"):
                idents[word] = idents.get(word, 0) + 1
            elif kind == "attr":
                name = _SCRIPT_ATTR_RE.match(word).group(1)
                attrs[name] = attrs.get(name, 0) + 1

    def ranked(counts):
        return sorted(counts, key=lambda w: (-counts[w], len(w), w))

    return ranked(idents), ranked(attrs)


def suggest(lines, row, col, attributes=()):
    """Completion for the word ending at (row, col). `attributes`: extra attribute
    names to offer (e.g. ones found on the incoming geometry)."""
    line = lines[row]
    if col < len(line) and is_word_char(line[col]):
        return ""  # in the middle of a word
    start = col
    while start > 0 and is_word_char(line[start - 1]):
        start -= 1
    word = line[start:col]
    idents, script_attrs = _script_words(lines, row, col)

    m = _ATTR_RE.match(word)
    if m:
        partial = m.group(2)
        pool = script_attrs + list(attributes) + _BUILTIN_ATTRS
    elif "@" not in word and _IDENT_RE.match(word):
        partial = word
        pool = idents + _LANGUAGE
    else:
        return ""
    if not partial:
        return ""
    for name in pool:
        if name.startswith(partial):
            return name[len(partial):]  # "" when the word is already complete
    return ""
