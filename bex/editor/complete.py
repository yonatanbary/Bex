"""Autocomplete for the on-node editor (no bpy).

``complete()`` lists every name matching the word before the cursor, best first:
the exact name, names starting with it, names containing it (``lerp`` finds
``slerp``), then names holding its letters in order. The editor shows them in a
menu under the cursor. ``suggest()`` is the rest of the best name when it starts
with the word: the ghost text after the cursor.
"""

import re
from collections import namedtuple

from ..lang.lexer import ALIASES
from ..lang.compiler import BUILTINS, CHANNELS, COUNT_ATTRS, FUNCTION_NAMES, KNOWN_TYPES
from .buffer import is_word_char
from .highlight import CONSTANTS, KEYWORDS, TYPES, highlight

MAX_ITEMS = 50   # the menu lists at most this many names

# One menu entry: the name, what it is (function, channel, keyword, type,
# constant, variable, attribute) and the indexes of its characters the word matched.
Item = namedtuple("Item", "name kind match")
# `start`: the column where the word being completed begins (after an @).
Completion = namedtuple("Completion", "start partial items")

_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_]\w*)", re.M)

_ATTR_RE = re.compile(r"([fivbpu34]?)@([A-Za-z_]\w*)$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*$")
_SCRIPT_ATTR_RE = re.compile(r"[fivbpu34]?@([A-Za-z_]\w*)")

# Language names, in the order they are offered when the script itself has no match.
_LANGUAGE = sorted(set(FUNCTION_NAMES) | set(CHANNELS) | KEYWORDS | TYPES | CONSTANTS,
                   key=lambda n: (len(n), n))
_BUILTIN_ATTRS = list(BUILTINS) + list(COUNT_ATTRS) + list(ALIASES) + [n for n in KNOWN_TYPES if n not in BUILTINS]


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


def _language_kind(name):
    if name in CHANNELS:
        return "channel"
    if name in FUNCTION_NAMES:
        return "function"
    if name in KEYWORDS:
        return "keyword"
    if name in TYPES:
        return "type"
    return "constant"


def match(partial, name):
    """(rank, matched indexes) of `name` for the typed `partial`, or None.
    Rank 0: the same name, 1: starts with it, 2: contains it, 3: holds its
    letters in order (only for two letters or more). Case is ignored."""
    if not partial:
        return 1, ()
    low, part = name.lower(), partial.lower()
    at = low.find(part)
    if at >= 0:
        rank = 0 if low == part else 1 if at == 0 else 2
        return rank, tuple(range(at, at + len(part)))
    if len(part) < 2:
        return None
    hits, i = [], 0
    for ch in part:
        i = low.find(ch, i)
        if i < 0:
            return None
        hits.append(i)
        i += 1
    return 3, tuple(hits)


def complete(lines, row, col, attributes=(), force=False, limit=MAX_ITEMS):
    """The names matching the word ending at (row, col), as a `Completion`, or
    None (mid-word, not a name, or nothing typed yet unless `force`).
    `attributes`: extra attribute names to offer (e.g. ones found on the
    incoming geometry)."""
    line = lines[row]
    if col < len(line) and is_word_char(line[col]):
        return None  # in the middle of a word
    start = col
    while start > 0 and is_word_char(line[start - 1]):
        start -= 1
    word = line[start:col]
    idents, script_attrs = _script_words(lines, row, col)
    m = _ATTR_RE.match(word)
    if m:
        partial = m.group(2)
        pools = [(script_attrs, "attribute"), (list(attributes), "attribute"), (_BUILTIN_ATTRS, "attribute")]
    elif "@" not in word and (_IDENT_RE.match(word) or not word):
        partial = word
        defs = set(_DEF_RE.findall("\n".join(lines)))
        own = [(n, "function" if n in defs or n in FUNCTION_NAMES else "variable") for n in idents]
        pools = [(own, None), ([(n, _language_kind(n)) for n in _LANGUAGE], None)]
    else:
        return None
    if not partial and not force:
        return None
    found, seen = [], set()
    for source, (pool, kind) in enumerate(pools):
        for entry in pool:
            name, k = (entry, kind) if kind else entry
            if name in seen:
                continue
            hit = match(partial, name)
            if hit is None:
                continue
            seen.add(name)
            found.append(((hit[0], source, len(name), name), Item(name, k, hit[1])))
    found.sort(key=lambda f: f[0])
    return Completion(col - len(partial), partial, [item for _key, item in found[:limit]])


def suggest(lines, row, col, attributes=()):
    """Ghost text for the word ending at (row, col): the rest of the best match
    when it starts with the word, else "" (also when the word is already a
    complete name)."""
    comp = complete(lines, row, col, attributes)
    if comp is None or not comp.items:
        return ""
    best = comp.items[0].name
    return best[len(comp.partial):] if best.startswith(comp.partial) else ""
