"""Keyboard and mouse mapping for the on-node editor (no bpy).

Keys use Blender's event type names (``"LEFT_ARROW"``, ``"A"``, ``"RET"`` …) so
the Blender side can pass events straight through. ``mac`` swaps the platform
modifiers: Cmd is the command key and Option moves by word, as in other macOS
editors; elsewhere Ctrl does both.
"""

EXIT = "exit"
ZOOM_IN, ZOOM_OUT, ZOOM_RESET = "zoom_in", "zoom_out", "zoom_reset"
_ZOOM_KEYS = {
    "EQUAL": ZOOM_IN, "PLUS": ZOOM_IN, "NUMPAD_PLUS": ZOOM_IN,
    "MINUS": ZOOM_OUT, "NUMPAD_MINUS": ZOOM_OUT,
    "ZERO": ZOOM_RESET, "NUMPAD_0": ZOOM_RESET,
}

_MOVES = {
    "LEFT_ARROW": "left", "RIGHT_ARROW": "right", "UP_ARROW": "up", "DOWN_ARROW": "down",
    "HOME": "home", "END": "end", "PAGE_UP": "page_up", "PAGE_DOWN": "page_down",
}


class Clipboard:
    """Stand-in clipboard; the Blender side passes one backed by the window manager."""

    def __init__(self, text=""):
        self.text = text

    def get(self):
        return self.text

    def set(self, text):
        self.text = text


def handle_key(buf, key, text="", ctrl=False, shift=False, alt=False, cmd=False,
               mac=False, page=10, clipboard=None):
    """Apply one key press to `buf`.

    Returns ``EXIT`` when editing should end, ``ZOOM_IN`` / ``ZOOM_OUT`` /
    ``ZOOM_RESET`` for Ctrl or Cmd with + - 0, True if the key did something,
    False if it means nothing to the editor (the caller still swallows it so it
    doesn't trigger node editor shortcuts).
    """
    command = cmd if mac else ctrl    # Cmd+C on macOS, Ctrl+C elsewhere
    word = alt if mac else ctrl       # Option+Left on macOS, Ctrl+Left elsewhere
    clipboard = clipboard if clipboard is not None else Clipboard()

    if key == "ESC" or (key in ("RET", "NUMPAD_ENTER") and (ctrl or cmd)):
        return EXIT

    if (ctrl or cmd) and key in _ZOOM_KEYS:
        return _ZOOM_KEYS[key]

    if key in _MOVES:
        move = _MOVES[key]
        if mac and cmd:  # Cmd+arrows jump to line / document ends
            move = {"left": "home", "right": "end", "up": "doc_start", "down": "doc_end"}.get(move, move)
        elif not mac and ctrl and move in ("home", "end"):
            move = "doc_start" if move == "home" else "doc_end"
        if move in ("left", "right"):
            getattr(buf, "move_" + move)(word=word, select=shift)
        elif move in ("page_up", "page_down"):
            getattr(buf, move)(page, select=shift)
        else:
            getattr(buf, "move_" + move)(select=shift)
        return True

    if key == "BACK_SPACE":
        buf.backspace(word=word or (mac and cmd))
        return True
    if key == "DEL":
        buf.delete(word=word)
        return True
    if key in ("RET", "NUMPAD_ENTER"):
        buf.newline()
        return True
    if key == "TAB":
        buf.dedent() if shift else buf.indent()
        return True

    if command:
        if key == "A":
            buf.select_all()
        elif key in ("C", "X"):
            if buf.has_selection:
                clipboard.set(buf.selected_text())
                if key == "X":
                    buf.backspace()
        elif key == "V":
            pasted = clipboard.get()
            if pasted:
                buf.insert(pasted)
        elif key == "Z":
            buf.redo() if shift else buf.undo()
        elif key == "Y":
            buf.redo()
        elif key in ("SLASH", "NUMPAD_SLASH"):
            buf.toggle_comment()
        else:
            return False
        return True

    if text and text.isprintable() and not (ctrl and not mac):
        buf.insert(text)
        return True
    return False


def thumb(total, visible, first, length, min_size=0.0):
    """Scrollbar thumb `(offset, size)` on a track of `length`, for `visible` of
    `total` rows (or columns) starting at `first`. None when everything fits."""
    if total <= visible or length <= 0:
        return None
    size = min(length, max(min_size, length * visible / total))
    span = total - visible
    offset = (length - size) * min(max(first, 0), span) / span
    return offset, size


def thumb_first(offset, size, total, visible, length):
    """Inverse of `thumb`: the first row (or column) for a thumb at `offset`."""
    if total <= visible or length <= size:
        return 0
    frac = min(max(offset / (length - size), 0.0), 1.0)
    return int(round(frac * (total - visible)))


class View:
    """Which part of the buffer is visible, and the point <-> (row, col) mapping.

    Coordinates are local to the code area: x grows right from its left edge,
    y grows down from its top edge, both in the same units as `line_h` / `char_w`.
    """

    def __init__(self):
        self.top = 0    # first visible row
        self.left = 0   # first visible column
        self.pan_y = 0.0  # trackpad scrolling left over between whole rows
        self.pan_x = 0.0  # ... and columns

    def follow(self, cursor, rows, cols):
        """Scroll just enough to keep the cursor in view."""
        row, col = cursor
        if row < self.top:
            self.top = row
        elif row >= self.top + rows:
            self.top = row - rows + 1
        margin = min(4, max(0, cols // 4))
        if col < self.left + margin:
            self.left = max(0, col - margin)
        elif col > self.left + cols - margin:
            self.left = col - cols + margin

    def clamp(self, line_count, rows):
        self.top = min(max(self.top, 0), max(0, line_count - rows))
        self.left = max(self.left, 0)

    def scroll(self, delta, line_count, rows):
        self.top += delta
        self.clamp(line_count, rows)

    def hit(self, x, y, line_h, char_w):
        """(row, col) under a local point; columns round to the nearest gap."""
        row = self.top + int(y // line_h) if y >= 0 else self.top - 1
        col = self.left + max(0, int(round(x / char_w)))
        return row, col
