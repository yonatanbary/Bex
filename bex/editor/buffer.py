"""Multi-line text editing model for the on-node editor (no bpy).

Positions are ``(row, col)`` tuples, 0-based, col counted in characters.
The selection runs between ``anchor`` and ``cursor`` when ``anchor`` is set.
``version`` increments on every content change.
"""

INDENT = "    "
HISTORY_LIMIT = 200


_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@")


def is_word_char(ch):
    return ch in _WORD_CHARS


def _char_class(ch):
    if ch.isspace():
        return 0
    return 1 if is_word_char(ch) else 2


_OPEN, _CLOSE = "([{", ")]}"
_PAIRS = {"(": ")", "[": "]", "{": "}", ")": "(", "]": "[", "}": "{"}
_OPERATOR_CHARS = frozenset("+-*/%<>=!&|?:")


def _char_kind(ch):
    if ch.isspace():
        return "space"
    if is_word_char(ch):
        return "word"
    return "op" if ch in _OPERATOR_CHARS else "punct"


def _grow_number(line, start, end):
    """Widen a selection of digits to the whole number, e.g. 0.25 rather than 0 or 25."""
    def digits(a, b):
        return a < b and line[a:b].isdigit()

    if line[start:end] == ".":
        if not (digits(start - 1, start) or digits(end, end + 1)):
            return start, end
    elif not digits(start, end):
        return start, end
    while start > 0 and (line[start - 1].isdigit() or line[start - 1] == "."):
        start -= 1
    while end < len(line) and (line[end].isdigit() or line[end] == "."):
        end += 1
    return start, end


def _mask_strings(line):
    """`line` with the inside of string literals blanked, so brackets in them are ignored."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(" " if ch != quote else ch)
            if ch == quote:
                quote = None
        else:
            if ch in "\"'":
                quote = ch
            out.append(ch)
    return "".join(out)


def _leading_ws(line):
    return line[: len(line) - len(line.lstrip(" \t"))]


class TextBuffer:
    def __init__(self, text=""):
        self.lines = text.split("\n")
        self.cursor = (0, 0)
        self.anchor = None
        self.version = 0
        self._undo = []
        self._redo = []
        self._group = None      # "type" while consecutive word characters are being typed
        self._pref_col = None   # remembered column across consecutive vertical moves

    # -- content -------------------------------------------------------------
    @property
    def text(self):
        return "\n".join(self.lines)

    def set_text(self, text):
        if text == self.text:
            return
        self._begin_edit()
        self.lines = text.split("\n")
        self.cursor = self._clamp(self.cursor)
        self.anchor = None
        self._changed()

    # -- selection -----------------------------------------------------------
    @property
    def has_selection(self):
        return self.anchor is not None and self.anchor != self.cursor

    def selection_range(self):
        if not self.has_selection:
            return None
        return (min(self.anchor, self.cursor), max(self.anchor, self.cursor))

    def selected_text(self):
        rng = self.selection_range()
        if rng is None:
            return ""
        (r0, c0), (r1, c1) = rng
        if r0 == r1:
            return self.lines[r0][c0:c1]
        parts = [self.lines[r0][c0:]] + self.lines[r0 + 1:r1] + [self.lines[r1][:c1]]
        return "\n".join(parts)

    def select_all(self):
        self._group = self._pref_col = None
        self.anchor = (0, 0)
        self.cursor = self._end()

    def clear_selection(self):
        self.anchor = None

    # -- editing -------------------------------------------------------------
    def insert(self, s):
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", INDENT)
        if not s and not self.has_selection:
            return
        typing = len(s) == 1 and is_word_char(s) and not self.has_selection
        self._begin_edit("type" if typing else None)
        self._delete_selection()
        self._insert_raw(s)
        if s == ":":
            self._dedent_else(self.cursor[0])
        self._changed()

    def _dedent_else(self, row):
        """Typing the ':' of `else:` / `elif ...:` lines it up with its `if`, like VS Code."""
        line = self.lines[row]
        word = line.strip().split(" ")[0].rstrip(":")
        if word not in ("else", "elif"):
            return
        indent = len(_leading_ws(line))
        for r in range(row - 1, -1, -1):
            above = self.lines[r]
            if not above.strip():
                continue
            at = len(_leading_ws(above))
            if at <= indent and above.lstrip().split(" ")[0].rstrip(":") in ("if", "elif"):
                if at < indent:
                    self.lines[row] = above[:at] + line[indent:]
                    self._shift(row, 0, at - indent)
                return
            if at < indent - len(INDENT):
                return

    def newline(self):
        self._begin_edit()
        self._delete_selection()
        row, col = self.cursor
        line = self.lines[row]
        before, after = line[:col], line[col:].lstrip(" ")
        indent = _leading_ws(before)
        code = before.split("#")[0].rstrip()
        opened = code.endswith((":", "(", "["))
        inner = indent + INDENT if opened else indent
        if code.strip() == "pass":  # the block is done: back out one level
            inner = indent[len(INDENT):]
        if code.endswith(("(", "[")) and after[:1] == _PAIRS[code[-1]]:
            new = [before, inner, indent + after]  # (|) -> three lines
        else:
            new = [before, inner + after]
        self.lines[row:row + 1] = new
        self.cursor = (row + 1, len(inner))
        self._changed()

    def backspace(self, word=False):
        if self.has_selection:
            self._begin_edit()
            self._delete_selection()
            self._changed()
            return
        row, col = self.cursor
        if word:
            target = self._word_left(self.cursor)
        elif col > 0 and not self.lines[row][:col].strip(" "):
            target = (row, (col - 1) // len(INDENT) * len(INDENT))  # back to previous indent stop
        else:
            target = self._step_left(self.cursor)
        self._delete_to(target)

    def delete(self, word=False):
        if self.has_selection:
            self._begin_edit()
            self._delete_selection()
            self._changed()
            return
        target = self._word_right(self.cursor) if word else self._step_right(self.cursor)
        self._delete_to(target)

    def indent(self):
        if not self.has_selection:
            self.insert(" " * (len(INDENT) - self.cursor[1] % len(INDENT)))
            return
        rows = [r for r in self._touched_rows() if self.lines[r]]
        if not rows:
            return
        self._begin_edit()
        for r in rows:
            self.lines[r] = INDENT + self.lines[r]
            self._shift(r, 0, len(INDENT))
        self._changed()

    def dedent(self):
        edits = []
        for r in self._touched_rows():
            n = len(self.lines[r]) - len(self.lines[r].lstrip(" "))
            if n:
                edits.append((r, min(n, len(INDENT))))
        if not edits:
            return
        self._begin_edit()
        for r, n in edits:
            self.lines[r] = self.lines[r][n:]
            self._shift(r, 0, -n)
        self._changed()

    def toggle_comment(self):
        rows = [r for r in self._touched_rows() if self.lines[r].strip()]
        if not rows:
            return
        self._begin_edit()
        if all(self.lines[r].lstrip().startswith("#") for r in rows):
            for r in rows:
                line = self.lines[r]
                at = len(_leading_ws(line))
                n = 2 if line[at:at + 2] == "# " else 1
                self.lines[r] = line[:at] + line[at + n:]
                self._shift(r, at, -n)
        else:
            at = min(len(_leading_ws(self.lines[r])) for r in rows)
            for r in rows:
                self.lines[r] = self.lines[r][:at] + "# " + self.lines[r][at:]
                self._shift(r, at, 2)
        self._changed()

    # -- movement ------------------------------------------------------------
    def move_left(self, word=False, select=False):
        if self.has_selection and not select:
            self._move(self.selection_range()[0], False)
        else:
            self._move(self._word_left(self.cursor) if word else self._step_left(self.cursor), select)

    def move_right(self, word=False, select=False):
        if self.has_selection and not select:
            self._move(self.selection_range()[1], False)
        else:
            self._move(self._word_right(self.cursor) if word else self._step_right(self.cursor), select)

    def move_up(self, select=False):
        self._vertical(-1, select)

    def move_down(self, select=False):
        self._vertical(1, select)

    def page_up(self, n, select=False):
        self._vertical(-max(1, n), select)

    def page_down(self, n, select=False):
        self._vertical(max(1, n), select)

    def move_home(self, select=False):
        row, col = self.cursor
        first = len(_leading_ws(self.lines[row]))
        self._move((row, 0 if col == first else first), select)

    def move_end(self, select=False):
        row = self.cursor[0]
        self._move((row, len(self.lines[row])), select)

    def move_doc_start(self, select=False):
        self._move((0, 0), select)

    def move_doc_end(self, select=False):
        self._move(self._end(), select)

    # -- mouse ---------------------------------------------------------------
    def click(self, row, col, select=False):
        self._move(self._clamp((row, col)), select)

    def select_word_at(self, row, col):
        row, col = self._clamp((row, col))
        line = self.lines[row]
        start = end = col
        while start > 0 and is_word_char(line[start - 1]):
            start -= 1
        while end < len(line) and is_word_char(line[end]):
            end += 1
        if start == end and col < len(line):
            end = col + 1  # not on a word: select the single character
        self._group = self._pref_col = None
        self.anchor, self.cursor = (row, start), (row, end)

    def select_at(self, row, col):
        """Double-click selection, like VS Code: the word, number, string, bracket
        pair, operator or run of spaces under (row, col)."""
        row, col = self._clamp((row, col))
        line = self.lines[row]
        if not line:
            self._select(row, 0, row, 0)
            return
        # Just after a word (end of line or before a space): take the word to the left.
        if col > 0 and (col == len(line) or line[col] == " ") and _char_kind(line[col - 1]) == "word":
            col -= 1
        col = min(col, len(line) - 1)
        ch = line[col]
        kind = _char_kind(ch)
        if ch in _OPEN or ch in _CLOSE:
            match = self._match_bracket(row, col)
            if match:
                (r0, c0), (r1, c1) = sorted([(row, col), match])
                self._select(r0, c0, r1, c1 + 1)
                return
        if ch in "\"'":
            end = line.find(ch, col + 1)
            start = line.rfind(ch, 0, col)
            # Which quote is this: an opening one has an even number of quotes before it.
            if line.count(ch, 0, col) % 2 == 0 and end != -1:
                self._select(row, col, row, end + 1)
                return
            if line.count(ch, 0, col) % 2 == 1 and start != -1:
                self._select(row, start, row, col + 1)
                return
        start = end = col
        if kind in ("word", "space", "op"):
            while start > 0 and _char_kind(line[start - 1]) == kind:
                start -= 1
            while end < len(line) and _char_kind(line[end]) == kind:
                end += 1
        else:
            end = col + 1
        start, end = _grow_number(line, start, end)
        self._select(row, start, row, end)

    def select_line(self, row):
        row = self._clamp((row, 0))[0]
        self._group = self._pref_col = None
        self.anchor = (row, 0)
        self.cursor = (row + 1, 0) if row + 1 < len(self.lines) else (row, len(self.lines[row]))

    def _select(self, r0, c0, r1, c1):
        self._group = self._pref_col = None
        self.anchor, self.cursor = (r0, c0), (r1, c1)

    def _match_bracket(self, row, col):
        """Position of the bracket matching the one at (row, col), skipping strings."""
        ch = self.lines[row][col]
        forward = ch in _OPEN
        pair = _PAIRS[ch]
        depth = 0
        r, c = row, col
        while 0 <= r < len(self.lines):
            line = self.lines[r]
            masked = _mask_strings(line)
            rng = range(c, len(line)) if forward else range(c, -1, -1)
            for i in rng:
                if masked[i] == ch:
                    depth += 1
                elif masked[i] == pair:
                    depth -= 1
                    if depth == 0:
                        return (r, i)
            r += 1 if forward else -1
            if 0 <= r < len(self.lines):
                c = 0 if forward else len(self.lines[r]) - 1
        return None

    # -- undo ----------------------------------------------------------------
    def undo(self):
        return self._restore(self._undo, self._redo)

    def redo(self):
        return self._restore(self._redo, self._undo)

    # -- internals -----------------------------------------------------------
    def _snapshot(self):
        return (self.text, self.cursor, self.anchor)

    def _restore(self, source, target):
        if not source:
            return False
        target.append(self._snapshot())
        text, self.cursor, self.anchor = source.pop()
        self.lines = text.split("\n")
        self._group = None
        self._changed()
        return True

    def _begin_edit(self, kind=None):
        """Record an undo snapshot unless this edit continues the current typing group."""
        if not (kind == "type" and self._group == "type"):
            self._undo.append(self._snapshot())
            del self._undo[:-HISTORY_LIMIT]
        self._redo.clear()
        self._group = kind

    def _changed(self):
        self.version += 1
        self._pref_col = None

    def _move(self, pos, select):
        if select:
            if self.anchor is None:
                self.anchor = self.cursor
        else:
            self.anchor = None
        self.cursor = pos
        self._group = self._pref_col = None

    def _vertical(self, delta, select):
        if self.has_selection and not select:
            self.anchor = None
        row, col = self.cursor
        pref = col if self._pref_col is None else self._pref_col
        new_row = row + delta
        if new_row < 0:
            target, pref = (0, 0), 0
        elif new_row >= len(self.lines):
            target = self._end()
            pref = target[1]
        else:
            target = (new_row, min(pref, len(self.lines[new_row])))
        self._move(target, select)
        self._pref_col = pref

    def _end(self):
        return (len(self.lines) - 1, len(self.lines[-1]))

    def _clamp(self, pos):
        row = min(max(pos[0], 0), len(self.lines) - 1)
        return (row, min(max(pos[1], 0), len(self.lines[row])))

    def _touched_rows(self):
        rng = self.selection_range()
        if rng is None:
            return [self.cursor[0]]
        (r0, _), (r1, c1) = rng
        if r1 > r0 and c1 == 0:
            r1 -= 1  # a selection ending at column 0 doesn't touch that line
        return list(range(r0, r1 + 1))

    def _shift(self, row, at, delta):
        """Keep cursor/anchor on the same text after inserting (delta > 0) or removing at (row, at)."""
        def fix(pos):
            if pos is None or pos[0] != row:
                return pos
            r, c = pos
            if c > at or (c == at and delta > 0 and (at > 0 or not self.has_selection)):
                c = max(at, c + delta)
            return (r, c)
        self.cursor, self.anchor = fix(self.cursor), fix(self.anchor)

    def _insert_raw(self, s):
        row, col = self.cursor
        line = self.lines[row]
        parts = s.split("\n")
        parts[0] = line[:col] + parts[0]
        end_col = len(parts[-1])
        parts[-1] += line[col:]
        self.lines[row:row + 1] = parts
        self.cursor = (row + len(parts) - 1, end_col)

    def _delete_range(self, a, b):
        (r0, c0), (r1, c1) = min(a, b), max(a, b)
        self.lines[r0:r1 + 1] = [self.lines[r0][:c0] + self.lines[r1][c1:]]
        self.cursor = (r0, c0)

    def _delete_selection(self):
        if self.has_selection:
            self._delete_range(self.anchor, self.cursor)
        self.anchor = None

    def _delete_to(self, target):
        if target == self.cursor:
            self._group = None
            return
        self._begin_edit()
        self._delete_range(self.cursor, target)
        self.anchor = None
        self._changed()

    def _step_left(self, pos):
        row, col = pos
        if col > 0:
            return (row, col - 1)
        return (row - 1, len(self.lines[row - 1])) if row > 0 else pos

    def _step_right(self, pos):
        row, col = pos
        if col < len(self.lines[row]):
            return (row, col + 1)
        return (row + 1, 0) if row + 1 < len(self.lines) else pos

    def _word_left(self, pos):
        row, col = pos
        if col == 0:
            return self._step_left(pos)
        line = self.lines[row]
        while col > 0 and line[col - 1].isspace():
            col -= 1
        if col > 0:
            cls = _char_class(line[col - 1])
            while col > 0 and _char_class(line[col - 1]) == cls:
                col -= 1
        return (row, col)

    def _word_right(self, pos):
        row, col = pos
        line = self.lines[row]
        if col == len(line):
            return self._step_right(pos)
        cls = _char_class(line[col])
        while col < len(line) and _char_class(line[col]) == cls:
            col += 1
        while col < len(line) and line[col].isspace():
            col += 1
        return (row, col)
