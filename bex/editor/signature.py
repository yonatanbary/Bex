"""Signature help for the on-node editor (no bpy).

``signature_at()`` finds the call the cursor is inside and which of its
arguments is being typed, so the editor can show that function's parameters.
"""

from ..lang.compiler import SIGNATURES

_OPEN, _CLOSE = "([", ")]"


class Signature:
    """The call around the cursor: `name`, its `params` and `summary` (from
    SIGNATURES), the `active` argument index, and where the name starts
    (`row`, `col`)."""

    __slots__ = ("name", "params", "summary", "active", "row", "col")

    def __init__(self, name, active, row, col):
        self.name, self.active, self.row, self.col = name, active, row, col
        self.params, self.summary = SIGNATURES[name]

    def parts(self):
        """The signature as `(text, role)` pieces, role being "name", "punct",
        "param" or "active" (the argument being typed)."""
        out = [(self.name, "name"), ("(", "punct")]
        for i, param in enumerate(self.params):
            if i:
                out.append((", ", "punct"))
            out.append((param, "active" if i == self.active else "param"))
        out.append((")", "punct"))
        return out

    def __repr__(self):
        return f"Signature({self.name!r}, active={self.active}, at={self.row},{self.col})"


def signature_at(lines, row, col):
    """The Signature of the innermost known function call whose parentheses
    contain the cursor, or None. Strings and comments are skipped, brackets
    nest, and a call may continue over several lines. Plain parentheses and
    brackets are looked through (`fit((a|` is still fit's first argument); an
    unknown function hides the help."""
    stack = []  # [name or None, comma count, row, col] per open bracket
    for r in range(min(row, len(lines) - 1) + 1):
        line = lines[r]
        end = col if r == row else len(line)
        i, word, word_at = 0, "", 0
        while i < end:
            ch = line[i]
            if ch.isalnum() or ch in "_@":
                if not word:
                    word_at = i
                word += ch
                i += 1
                continue
            name = word if word and not word[0].isdigit() and "@" not in word else None
            word = ""
            if ch == "#":
                break
            if ch in "\"'":
                close = line.find(ch, i + 1)
                while close > 0 and line[close - 1] == "\\":
                    close = line.find(ch, close + 1)
                if close < 0 or close >= end:
                    break  # an open string reaches the cursor or the line end
                i = close + 1
                continue
            if ch in _OPEN:
                stack.append([name if ch == "(" else None, 0, r, word_at])
            elif ch in _CLOSE:
                if stack:
                    stack.pop()
            elif ch == "," and stack:
                stack[-1][1] += 1
            i += 1
    for name, commas, r, c in reversed(stack):
        if name is None:
            continue
        return Signature(name, commas, r, c) if name in SIGNATURES else None
    return None
