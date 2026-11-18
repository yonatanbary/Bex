"""The code editor drawn on each BEX Wrangle node (Blender side).

`BexWrangleNode.draw_buttons` reserves an empty block at the bottom of the
node body (`reserve()`); a Node Editor draw handler paints the script into it
with syntax highlighting. Clicking that block starts `BEX_OT_code_edit`, a modal
operator that feeds keys to a `TextBuffer`, writes every change back to the
script Text and recompiles. Nothing polls: compiles come from these events.

While a line is being typed, ch*() names on it don't create inputs yet (see the
compiler's `pending_line`). The inputs appear once the cursor leaves the line
(Enter, arrows, a click) or editing ends.
"""

import json
import sys
import textwrap
import time

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .editor.buffer import TextBuffer, is_word_char
from .editor.complete import suggest
from .editor.highlight import highlight
from .upstream import incoming_attributes
from .lang.compiler import BUILTINS
from .editor.keys import EXIT, ZOOM_IN, ZOOM_OUT, ZOOM_RESET, View, handle_key, thumb, thumb_first

FONT_ID = 1             # Blender's built-in monospace font
FONT_SIZE = 11.0        # all sizes are node-editor units at zoom 1, before UI scale
LINE_H = 15.0
PAD = 5.0
MIN_ROWS = 10           # an empty box still has room to write
MAX_ROWS = 16           # longer scripts scroll inside the box
UNIT_Y = 20.0           # height of one layout row (Blender's UI_UNIT_Y)
CLICK_SLOP = 5.0        # px (× UI scale): presses closer than this count as the same spot
DEBUG_CLICKS = False    # show each click's count / position / selection in the status bar
ZOOM_STEP = 0.1
BAR_W = 5.0             # scrollbar thickness
BAR_MARGIN = 3.0        # gap between a scrollbar and the box edge
BAR_BOTTOM = 6.0        # gap under the horizontal scrollbar (a bit more, easier to grab)
BAR_HIT = 4.0           # extra grab area around a scrollbar (kept inside the box)
BAR_MIN = 12.0          # shortest scrollbar thumb
BAR_STRIP = BAR_BOTTOM + BAR_W + BAR_MARGIN  # room under the code for the horizontal bar
GUTTER_PAD = 0.8        # line-number column: space on each side of the numbers, in characters
MIN_DIGITS = 2          # the column is never narrower than this many digits

# The info strip under the code (attributes read / written). It sits inside the
# reserved block, so it needs no layout measuring of its own.
INFO_FONT = 9.0
INFO_LINE_H = 12.0
INFO_LINES = 3         # input, reads, writes
ERROR_MAX_LINES = 4     # a long error wraps onto at most this many lines
INFO_GAP = 4.0

# A calm palette: mostly neutral text, colour only where it helps you read.
PALETTE = {
    "back": (0.105, 0.105, 0.11, 1.0),
    "gutter": (0.135, 0.135, 0.14, 1.0),       # line-number column
    "gutter_current": (0.175, 0.175, 0.18, 1.0),  # its row of the cursor line
    "line_number": (0.42, 0.42, 0.43, 1.0),
    "line_number_current": (0.72, 0.72, 0.73, 1.0),
    "focus": (0.36, 0.52, 0.78, 1.0),
    "evaluated": (0.36, 0.62, 0.42, 0.85),
    "error": (0.80, 0.36, 0.34, 0.9),
    "warning": (0.86, 0.72, 0.38, 1.0),
    "text": (0.80, 0.80, 0.80, 1.0),
    "dim": (0.52, 0.52, 0.53, 1.0),
    "comment": (0.44, 0.47, 0.44, 1.0),
    "keyword": (0.56, 0.67, 0.84, 1.0),
    "attr": (0.96, 0.78, 0.55, 1.0),       # light orange: @attributes
    "number": (0.93, 0.58, 0.30, 1.0),     # orange
    "string": (0.55, 0.82, 0.45, 1.0),     # green
    "function": (0.88, 0.86, 0.78, 1.0),
    "selection": (0.30, 0.42, 0.62, 0.55),
    "cursor": (0.90, 0.90, 0.90, 1.0),
    "ghost": (0.50, 0.50, 0.52, 0.85),
    "bar_track": (0.45, 0.52, 0.64, 0.10),
    "bar": (0.50, 0.58, 0.72, 0.40),
    "bar_active": (0.36, 0.52, 0.78, 0.85),
}
_COLOR_OF = {
    "comment": "comment", "string": "string", "number": "number", "constant": "number",
    "attr": "attr", "function": "function", "channel": "function",
    "type": "keyword", "keyword": "keyword", "operator": "dim", "punct": "dim",
}
_TYPE_PREFIX = {"float": "f", "int": "i", "vector": "v", "bool": "b"}

# Where the reserved block sits relative to the node's bottom edge. Below it come
# the input sockets (one row each) and the node's bottom padding. Measured from
# screenshots of Blender 5.2.
SOCKET_ROW = 22.0
BOTTOM_PAD = 7.0
SIDE = 10.5

MAC = sys.platform == "darwin"

_session = None          # the Session being edited, if any
_draw_handle = None
_highlight_cache = {}    # text -> highlighted spans
_buffers = {}            # Text.session_uid -> TextBuffer: each script's undo history, kept between sessions
_views = {}              # (tree name, node name) -> View: each code box's scroll, kept between sessions


def view_for(node):
    """The scroll position of a wrangle's code box (kept while Blender runs)."""
    key = (node.id_data.name, node.name)
    view = _views.get(key)
    if view is None:
        view = _views[key] = View()
    return view


def scroll_code(node, region, event):
    """Scroll a code box from a wheel or trackpad event. False when it doesn't
    apply (nothing to scroll that way), so the event can go to the Node Editor."""
    if event.ctrl or event.oskey or event.alt:
        return False
    lines = node.script.as_string().split("\n") if not is_editing(node) else _session.buf.lines
    view = view_for(node)
    rows = visible_rows(len(lines))
    longest = max(map(len, lines)) + 1
    cols = _session.cols if is_editing(node) else max(1, longest)
    if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE"):
        step = -3 if event.type == "WHEELUPMOUSE" else 3
        if event.shift:  # Shift+wheel scrolls sideways
            if not is_editing(node):
                return False
            view.left = max(0, min(view.left + step, max(view.left, longest - cols)))
            return True
        if len(lines) <= rows:
            return False
        view.scroll(step, len(lines), rows)
        return True
    if event.type == "TRACKPADPAN":
        line_h, char_w = _metrics(box_rect(node, region)[4], node.code_scale)
        dx, dy = event.mouse_x - event.mouse_prev_x, event.mouse_y - event.mouse_prev_y
        if abs(dy) >= abs(dx):
            if len(lines) <= rows:
                return False
            view.pan_y += dy / line_h
            whole = int(view.pan_y)
            view.pan_y -= whole
            view.scroll(whole, len(lines), rows)
        else:
            if not is_editing(node) or longest <= cols:
                return False
            view.pan_x -= dx / char_w
            whole = int(view.pan_x)
            view.pan_x -= whole
            view.left = max(0, min(view.left + whole, max(view.left, longest - cols)))
        return True
    return False


def buffer_for(text):
    """The editing buffer of a Text datablock, created on first use.

    Undo history lives here, not in the Session, so clicking back into a wrangle
    still undoes what was typed before (like Houdini). Changes made elsewhere
    (Text Editor, Blender's undo, disk reload) come in through `set_text` as
    undo steps of their own.
    """
    buf = _buffers.get(text.session_uid)
    if buf is None:
        buf = _buffers[text.session_uid] = TextBuffer(text.as_string())
    else:
        buf.set_text(text.as_string())
    return buf


def forget_buffers():
    """Drop every script's history and every box's scroll (a new file was loaded)."""
    _buffers.clear()
    _views.clear()


# -- geometry ------------------------------------------------------------------
def visible_rows(line_count):
    return min(max(line_count, MIN_ROWS), MAX_ROWS)


def box_height(line_count, scale=1.0):
    """The code rows, padding on top and, at the bottom, a strip that is always
    kept for the horizontal scrollbar, so it never covers the last line."""
    return visible_rows(line_count) * LINE_H * scale + PAD + BAR_STRIP


def _wrap(node, text):
    """`text` wrapped to the code box width (in the strip's font)."""
    blf.size(FONT_ID, INFO_FONT)
    char_w = blf.dimensions(FONT_ID, "M" * 10)[0] / 10 or INFO_FONT * 0.6
    width = max(10, int((node.width - 2 * SIDE - 2 * PAD) / char_w))
    lines = textwrap.wrap(text, width) or [text]
    if len(lines) > ERROR_MAX_LINES:
        lines = lines[:ERROR_MAX_LINES]
        lines[-1] = lines[-1][:width - 1] + "…"
    return lines


def note_lines(node):
    """[(line, palette key)]: the node's error in red, then its warning in orange."""
    notes = []
    if node.error:
        notes += [(line, "error") for line in _wrap(node, node.error)]
    if node.warning:
        notes += [(line, "warning") for line in _wrap(node, node.warning)]
    return notes


def info_height(node):
    """The strip under the code: the error / warning (if any), then reads / writes."""
    return INFO_GAP + (len(note_lines(node)) + INFO_LINES) * INFO_LINE_H


def reserve(layout, node):
    """Empty block in the node body the size of the code box plus the info strip."""
    col = layout.column()
    col.scale_y = (box_height(len(node.script.lines), node.code_scale) + info_height(node)) / UNIT_Y
    col.label(text="")


def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _zoom(region):
    v2d = region.view2d
    return v2d.view_to_region(1000, 0, clip=False)[0] - v2d.view_to_region(0, 0, clip=False)[0]


def box_rect(node, region):
    """(x0, y0, x1, y1, px) of the code box in region pixels; px = pixels per unit.
    The info strip is the `info_height(node)` just below y0."""
    s = _ui_scale()
    px = _zoom(region) / 1000.0 * s
    loc = node.location_absolute
    inputs = sum(1 for sock in node.inputs if sock.enabled and not sock.hide)
    left_v, top_v = loc.x * s, loc.y * s
    bottom_v = top_v - node.dimensions.y
    v2d = region.view2d
    x0, y_bottom_node = v2d.view_to_region(left_v, bottom_v, clip=False)
    x1 = v2d.view_to_region(left_v + node.width * s, 0, clip=False)[0]
    y0 = y_bottom_node + (BOTTOM_PAD + inputs * SOCKET_ROW + info_height(node)) * px
    y1 = y0 + box_height(len(node.script.lines), node.code_scale) * px
    return x0 + SIDE * px, y0, x1 - SIDE * px, y1, px


def wrangle_at(context, x, y):
    """The wrangle whose code box is under region point (x, y), and that box."""
    tree = getattr(context.space_data, "edit_tree", None)
    if tree is None:
        return None, None
    for node in reversed(tree.nodes[:]):
        if _shows_code(node):
            rect = box_rect(node, context.region)
            if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                return node, rect
    return None, None


def scrollbars(node, rect, lines, view):
    """The code box's scrollbars: {"v"/"h": (track, thumb, total, visible)}, where
    track and thumb are (x0, y0, x1, y1) in region pixels. A bar is left out when
    everything fits that way."""
    x0, y0, x1, y1, px = rect
    line_h, char_w = _metrics(px, node.code_scale)
    rows = visible_rows(len(lines))
    cols = max(1, int((x1 - text_left(x0, len(lines), char_w, px) - PAD * px) / char_w))
    v_total, h_total = len(lines), max(map(len, lines)) + 1  # +1: the cursor after the last char
    has_v, has_h = v_total > rows, h_total > cols
    w, m, mb = BAR_W * px, BAR_MARGIN * px, BAR_BOTTOM * px
    bars = {}
    if has_v:
        top, bottom = y1 - m, y0 + (mb + w + m if has_h else m)
        t = thumb(v_total, rows, view.top, top - bottom, BAR_MIN * px)
        if t:
            track = (x1 - m - w, bottom, x1 - m, top)
            bars["v"] = (track, (track[0], top - t[0] - t[1], track[2], top - t[0]), v_total, rows)
    if has_h:
        left, right = x0 + gutter_width(len(lines), char_w) + m, x1 - m - (w + m if has_v else 0)
        t = thumb(h_total, cols, view.left, right - left, BAR_MIN * px)
        if t:
            track = (left, y0 + mb, right, y0 + mb + w)
            bars["h"] = (track, (left + t[0], track[1], left + t[0] + t[1], track[3]), h_total, cols)
    return bars


def _shows_code(node):
    return (node.bl_idname == "GeometryNodeBexWrangle" and node.script is not None
            and node.show_options and not node.hide and node.dimensions.y > 0)


def _metrics(px, scale):
    blf.size(FONT_ID, FONT_SIZE * scale * px)
    return LINE_H * scale * px, blf.dimensions(FONT_ID, "M" * 10)[0] / 10


def gutter_width(line_count, char_w):
    """Width of the line-number column: wide enough for the last line's number."""
    return (max(MIN_DIGITS, len(str(line_count))) + 2 * GUTTER_PAD) * char_w


def text_left(x0, line_count, char_w, px):
    """Region x where column 0 of the code starts (after the line numbers)."""
    return x0 + gutter_width(line_count, char_w) + PAD * px


# -- session -------------------------------------------------------------------
class Session:
    """One wrangle being edited."""

    def __init__(self, node):
        self.tree_name = node.id_data.name
        self.node_name = node.name
        self.buf = buffer_for(node.script)
        self.written = self.buf.text   # what we last wrote to the Text
        self.start_text = self.written # to tell whether the session changed anything
        self.view = view_for(node)
        self.cols = 40                 # visible columns, updated on draw
        self.dragging = False
        self.clicks = 0                # presses in the current multi-click run (1, 2, 3)
        self.last_press = None         # (time, x, y) of the last press, to count multi-clicks
        self.suggestion = ""           # inline completion shown after the cursor
        self.bar = None                # ("v" | "h", grab offset) while dragging a scrollbar

    def node(self):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or node.bl_idname != "GeometryNodeBexWrangle" or node.script is None:
            return None
        return node

    def pull(self, node):
        """Take in changes made to the Text elsewhere (Text Editor, file reload)."""
        src = node.script.as_string()
        if src != self.written:
            self.buf.set_text(src)
            self.written = src

    def push(self, node):
        if self.buf.text != self.written:
            self.written = self.buf.text
            node.script.from_string(self.written)

    def follow(self):
        self.view.follow(self.buf.cursor, visible_rows(len(self.buf.lines)), self.cols)


def is_editing(node):
    return _session is not None and _session.tree_name == node.id_data.name and _session.node_name == node.name


def _redraw(context):
    if context.area:
        context.area.tag_redraw()


# -- drawing -------------------------------------------------------------------
def _rect(x0, y0, x1, y1, color):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]},
                             indices=[(0, 1, 2), (0, 2, 3)])
    shader.uniform_float("color", color)
    batch.draw(shader)


def _spans(lines):
    key = "\n".join(lines)
    spans = _highlight_cache.get(key)
    if spans is None:
        if len(_highlight_cache) > 64:
            _highlight_cache.clear()
        spans = _highlight_cache[key] = highlight(lines)
    return spans


def _draw():
    context = bpy.context
    tree = getattr(context.space_data, "edit_tree", None)
    if tree is None or tree.bl_idname != "GeometryNodeTree":
        return
    gpu.state.blend_set("ALPHA")
    old_scissor = gpu.state.scissor_get()
    vx, vy = gpu.state.viewport_get()[:2]
    try:
        for node in tree.nodes:
            if _shows_code(node):
                _draw_node(node, context.region, vx, vy)
    finally:
        gpu.state.scissor_set(*old_scissor)
        gpu.state.scissor_test_set(False)
        gpu.state.blend_set("NONE")


def _clip(region, vx, vy, x0, y0, x1, y1):
    """Clip drawing to a box, clamped to the region. Metal (macOS) drops the whole
    draw when the scissor reaches outside the region, e.g. a node half off-screen.
    Returns False when nothing of the box is visible."""
    cx0, cy0 = max(0, int(x0)), max(0, int(y0))
    cx1, cy1 = min(region.width, int(x1) + 1), min(region.height, int(y1) + 1)
    if cx1 <= cx0 or cy1 <= cy0:
        return False
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(vx) + cx0, int(vy) + cy0, cx1 - cx0, cy1 - cy0)
    return True


def _draw_node(node, region, vx, vy):
    x0, y0, x1, y1, px = box_rect(node, region)
    info_y0 = y0 - info_height(node) * px
    if x1 < 0 or y1 < 0 or x0 > region.width or info_y0 > region.height or x1 - x0 < 4:
        return
    gpu.state.scissor_test_set(False)  # the previous node's clip must not cut this one
    gpu.state.blend_set("ALPHA")       # nor its text drawing switch blending off
    editing = is_editing(node)
    if editing:
        lines, view = _session.buf.lines, _session.view
    else:
        lines, view = node.script.as_string().split("\n"), view_for(node)
    scale = node.code_scale
    _rect(x0, y0, x1, y1, PALETTE["back"])
    # Stroke: blue while focused, red on an error, green once evaluated and current.
    if editing:
        edge = PALETTE["focus"]
    elif node.error:
        edge = PALETTE["error"]
    elif node.is_up_to_date():
        edge = PALETTE["evaluated"]
    else:
        edge = None

    def outline():  # drawn last, over the line numbers and scrollbars
        if edge:
            w = max(1.0, px)
            gpu.state.scissor_test_set(False)
            for r in ((x0, y0, x1, y0 + w), (x0, y1 - w, x1, y1), (x0, y0, x0 + w, y1), (x1 - w, y0, x1, y1)):
                _rect(*r, edge)

    if FONT_SIZE * scale * px < 3:  # too far out to read
        outline()
        return
    _draw_info(node, region, x0, info_y0, x1, y0, px, vx, vy)
    line_h, char_w = _metrics(px, scale)
    pad = PAD * px
    left, top = text_left(x0, len(lines), char_w, px), y1 - pad
    rows = visible_rows(len(lines))
    cols = max(1, int((x1 - left - pad) / char_w))
    if editing:
        _session.cols = cols
    view.clamp(len(lines), rows)
    if not _clip(region, vx, vy, x0, y0, x1, y1):
        outline()
        return

    def col_x(col):
        return left + (col - view.left) * char_w

    first, last = view.top, min(len(lines), view.top + rows)
    if editing:
        sel = _session.buf.selection_range()
        for row in range(first, last):
            y_top = top - (row - first) * line_h
            if sel and sel[0][0] <= row <= sel[1][0]:
                c0 = sel[0][1] if row == sel[0][0] else 0
                # A row whose line break is selected fills to the box's right edge,
                # so empty lines inside a selection show as full rows.
                right = col_x(sel[1][1]) if row == sel[1][0] else x1 - pad
                _rect(col_x(c0), y_top - line_h, right, y_top, PALETTE["selection"])
    spans = _spans(lines)
    ghost = _session.suggestion if editing else ""
    ghost_row, ghost_col = _session.buf.cursor if editing else (-1, 0)
    font_px = FONT_SIZE * scale * px
    descent = (line_h - font_px) * 0.5 + 0.2 * font_px
    for row in range(first, last):
        y = top - (row - first + 1) * line_h + descent
        line = lines[row]
        pieces = spans[row]
        shift = 0
        if ghost and row == ghost_row:
            pieces = _split_spans(pieces, ghost_col)
            shift = len(ghost)
            blf.color(FONT_ID, *PALETTE["ghost"])
            blf.position(FONT_ID, col_x(ghost_col), y, 0)
            blf.draw(FONT_ID, ghost)
        for start, end, kind in pieces:
            # Text after the cursor moves right to make room for the ghost text.
            offset = shift if shift and start >= ghost_col else 0
            if end + offset <= view.left or start + offset >= view.left + cols + 1:
                continue
            blf.color(FONT_ID, *PALETTE[_COLOR_OF.get(kind, "text")])
            blf.position(FONT_ID, col_x(start + offset), y, 0)
            blf.draw(FONT_ID, line[start:end])
    gpu.state.blend_set("ALPHA")  # blf text drawing turns blending off
    if editing:
        row, col = _session.buf.cursor
        if first <= row < last:
            y_top = top - (row - first) * line_h
            w = max(1.0, 1.5 * px)
            _rect(col_x(col), y_top - line_h, col_x(col) + w, y_top, PALETTE["cursor"])
    # Line numbers, painted last so code scrolled sideways slides under them.
    gutter_r = x0 + gutter_width(len(lines), char_w)
    _rect(x0, y0, gutter_r, y1, PALETTE["gutter"])
    current = _session.buf.cursor[0] if editing else -1
    if first <= current < last:
        y_top = top - (current - first) * line_h
        _rect(x0, y_top - line_h, gutter_r, y_top, PALETTE["gutter_current"])
    blf.size(FONT_ID, font_px)
    for row in range(first, last):
        number = str(row + 1)
        y = top - (row - first + 1) * line_h + descent
        blf.color(FONT_ID, *PALETTE["line_number_current" if row == current else "line_number"])
        blf.position(FONT_ID, gutter_r - GUTTER_PAD * char_w - blf.dimensions(FONT_ID, number)[0], y, 0)
        blf.draw(FONT_ID, number)
    gpu.state.blend_set("ALPHA")
    for axis, (track, knob, _total, _visible) in scrollbars(node, (x0, y0, x1, y1, px), lines, view).items():
        _rect(*track, PALETTE["bar_track"])
        active = editing and _session.bar is not None and _session.bar[0] == axis
        _rect(*knob, PALETTE["bar_active" if active else "bar"])
    outline()


def _split_spans(spans, col):
    """Spans with any span that crosses `col` cut in two there."""
    out = []
    for start, end, kind in spans:
        if start < col < end:
            out += [(start, col, kind), (col, end, kind)]
        else:
            out.append((start, end, kind))
    return out


def info_lines(node):
    """The lines under the code: named attributes on the incoming geometry (a
    static guess, see upstream.py), attributes the script reads, and attributes
    written plus the node count."""
    try:
        info = json.loads(node.info) if node.info else {}
    except ValueError:
        info = {}

    def names(pairs):
        return "  ".join(_attr_label(n, t) for n, t in pairs) or "–"

    incoming = names(incoming_attributes(node))
    reads = names(info.get("reads", []))
    writes = names(info.get("writes", []))
    count = info.get("nodes")
    tail = f"   ·  {count} nodes" if count is not None else ""
    return [("input", incoming), ("reads", reads), ("writes", writes + tail)]


def _attr_label(name, type):
    if name in BUILTINS:
        return "@" + name
    return _TYPE_PREFIX.get(type, "") + "@" + name


def _fit(text, width):
    """`text` cut short with "…" so it is at most `width` wide (current blf size)."""
    if blf.dimensions(FONT_ID, text)[0] <= width:
        return text
    while text and blf.dimensions(FONT_ID, text + "…")[0] > width:
        text = text[:-1]
    return text + "…" if text else ""


def _draw_info(node, region, x0, y0, x1, y1, px, vx, vy):
    if not _clip(region, vx, vy, x0, y0, x1, y1):
        return
    blf.size(FONT_ID, INFO_FONT * px)
    label_w = blf.dimensions(FONT_ID, "writes  ")[0]
    line_h = INFO_LINE_H * px
    y = y1 - INFO_GAP * px - line_h + (line_h - INFO_FONT * px) * 0.5 + 0.2 * INFO_FONT * px
    for line, kind in note_lines(node):
        blf.color(FONT_ID, *PALETTE[kind][:3], 1.0)
        blf.size(FONT_ID, INFO_FONT * px)
        blf.position(FONT_ID, x0 + PAD * px, y, 0)
        blf.draw(FONT_ID, line)
        y -= line_h
    blf.size(FONT_ID, INFO_FONT * px)
    status = ""
    if is_editing(node):  # where the cursor is, right-aligned on the first line
        row, col = _session.buf.cursor
        status = f"Line {row + 1}, Col {col + 1}"
        status_w = blf.dimensions(FONT_ID, status)[0]
        blf.color(FONT_ID, *PALETTE["dim"])
        blf.position(FONT_ID, x1 - PAD * px - status_w, y, 0)
        blf.draw(FONT_ID, status)
    for label, value in info_lines(node):
        blf.color(FONT_ID, *PALETTE["comment"])
        blf.position(FONT_ID, x0 + PAD * px, y, 0)
        blf.draw(FONT_ID, label)
        if status:  # leave room for it
            value = _fit(value, x1 - x0 - 2 * PAD * px - label_w - status_w - 2 * INFO_FONT * px)
            status = ""
        blf.color(FONT_ID, *PALETTE["dim"])
        blf.position(FONT_ID, x0 + PAD * px + label_w, y, 0)
        blf.draw(FONT_ID, value)
        y -= line_h
    gpu.state.scissor_test_set(False)


# -- operator ------------------------------------------------------------------
class _WMClipboard:
    def __init__(self, wm):
        self.wm = wm

    def get(self):
        return self.wm.clipboard

    def set(self, text):
        self.wm.clipboard = text


_PASS = {
    "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM", "INBETWEEN_MOUSEMOVE",
    "WINDOW_DEACTIVATE", "TIMER", "TIMER0", "TIMER1", "TIMER2", "TIMER_JOBS", "TIMER_AUTOSAVE",
    "TIMER_REPORT", "TIMERREGION", "NONE",
}
_MODIFIER_KEYS = {"LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_CTRL", "RIGHT_CTRL", "LEFT_ALT", "RIGHT_ALT", "OSKEY", "HYPER", "APP"}


class BEX_OT_code_edit(bpy.types.Operator):
    """Click the code on a BEX Wrangle to type in it. Esc, Ctrl+Enter or a click outside finishes."""

    bl_idname = "bex.code_edit"
    bl_label = "Edit BEX Code"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR"

    def invoke(self, context, event):
        global _session
        if _session is not None:
            return {"PASS_THROUGH"}
        node, rect = wrangle_at(context, event.mouse_region_x, event.mouse_region_y)
        if node is None:
            return {"PASS_THROUGH"}
        _session = Session(node)
        area = context.area
        self._area = (area.x, area.y, area.width, area.height)
        self._select(context, node)
        if not self._bar_press(node, rect, event):
            self._click(context, node, rect, event)
        context.window_manager.modal_handler_add(self)
        _redraw(context)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        node = _session.node() if _session else None
        if node is None:
            return self._end(context, None, {"CANCELLED"})
        _session.pull(node)
        result = self._event(context, node, event)
        if result is not None:
            return result
        _redraw(context)
        return {"RUNNING_MODAL"}

    def _event(self, context, node, event):
        kind, value = event.type, event.value
        x, y = event.mouse_region_x, event.mouse_region_y
        if kind in _PASS:
            if kind in ("WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"):
                hit, _ = wrangle_at(context, x, y)
                if hit is not None and scroll_code(hit, context.region, event):
                    return None
            return {"PASS_THROUGH"}
        if kind == "MOUSEMOVE":
            if _session.bar is not None:
                self._bar_drag(node, box_rect(node, context.region), event)
                return None
            if _session.dragging:
                self._click(context, node, box_rect(node, context.region), event, drag=True)
                return None
            return {"PASS_THROUGH"}
        if kind == "LEFTMOUSE":
            if value == "RELEASE":
                _session.dragging = False
                _session.bar = None
                return None
            hit, rect = wrangle_at(context, x, y)
            if hit is None:  # click outside: finish and let the click through
                return self._end(context, node, {"FINISHED", "PASS_THROUGH"})
            if hit != node:
                self._end(context, node, {"FINISHED"})
                self._start_on(context, hit, rect, event)
                return None
            if self._bar_press(node, rect, event):
                return None
            # Blender's DOUBLE_CLICK is treated as a plain press: we count clicks ourselves.
            self._click(context, node, rect, event)
            return None
        if kind == "RIGHTMOUSE" and value == "PRESS":
            return self._end(context, node, {"FINISHED", "PASS_THROUGH"})
        if kind in _MODIFIER_KEYS or value not in ("PRESS", "NOTHING"):
            return None
        ax, ay, aw, ah = self._area
        if not (ax <= event.mouse_x < ax + aw and ay <= event.mouse_y < ay + ah):
            # Typing over another editor (e.g. the 3D Viewport) goes to that editor.
            return self._end(context, node, {"FINISHED", "PASS_THROUGH"})
        return self._key(context, node, event)

    def _key(self, context, node, event):
        buf = _session.buf
        version, row_before = buf.version, buf.cursor[0]
        if event.type == "TAB" and not event.shift and _session.suggestion and not buf.has_selection:
            buf.insert(_session.suggestion)  # accept the inline completion
            _session.suggestion = ""
            self._after_change(node, version, row_before)
            return None
        typed = event.type == "TEXTINPUT" or (
            event.unicode and event.unicode.isprintable() and not (event.ctrl or event.oskey))
        if event.type == "TEXTINPUT":
            if event.unicode:
                buf.insert(event.unicode)
            done = True
        else:
            done = handle_key(
                buf, event.type, text=event.unicode, ctrl=event.ctrl, shift=event.shift,
                alt=event.alt, cmd=event.oskey, mac=MAC, page=visible_rows(len(buf.lines)) - 1,
                clipboard=_WMClipboard(context.window_manager),
            )
        if done == EXIT:
            return self._end(context, node, {"FINISHED"})
        # Typing a word character (or deleting one) asks for a fresh completion;
        # anything else hides it.
        changed = buf.version != version
        if changed and (typed and is_word_char(event.unicode[-1:]) or event.type == "BACK_SPACE"):
            incoming = [name for name, _type in incoming_attributes(node)]
            _session.suggestion = suggest(buf.lines, *buf.cursor, attributes=incoming)
        else:
            _session.suggestion = ""
        if done in (ZOOM_IN, ZOOM_OUT, ZOOM_RESET):
            step = {ZOOM_IN: ZOOM_STEP, ZOOM_OUT: -ZOOM_STEP}.get(done)
            node.code_scale = 1.0 if step is None else round(node.code_scale + step, 2)
            _session.follow()
            return None
        if done is False and (event.ctrl or event.oskey or event.type.startswith("F") and event.type[1:].isdigit()):
            return {"PASS_THROUGH"}  # Cmd+S, F3 … keep working while editing
        self._after_change(node, version, row_before)
        return None

    def _after_change(self, node, version, row_before):
        buf = _session.buf
        if buf.version != version:
            _session.push(node)   # keep the Text current; compiling waits for focus loss
        _session.follow()

    def _click(self, context, node, rect, event, drag=False):
        buf = _session.buf
        x0, _y0, _x1, y1, px = rect
        mx, my = event.mouse_region_x, event.mouse_region_y
        line_h, char_w = _metrics(px, node.code_scale)
        code_x = text_left(x0, len(buf.lines), char_w, px)
        row, col = _session.view.hit(mx - code_x, y1 - PAD * px - my, line_h, char_w)
        version, row_before = buf.version, buf.cursor[0]
        _session.suggestion = ""
        if drag:
            view = _session.view
            left = view.left
            buf.click(row, col, select=True)
            self._after_change(node, version, row_before)
            # Sideways, a drag follows the mouse, not the cursor: on a short row the
            # cursor stops at the line end, but the view still scrolls toward the
            # mouse once it passes the box edge (up to the longest line).
            cols = _session.cols
            longest = max(map(len, buf.lines)) + 1
            # `hit` clamps to the first visible column; here the mouse may be left of it.
            mouse_col = left + int(round((mx - code_x) / char_w))
            if mouse_col > left + cols:
                left = min(mouse_col - cols, max(left, longest - cols))
            elif mouse_col < left:
                left = max(0, mouse_col)
            view.left = left
            return
        # Count clicks: a press soon after the last one, at about the same spot, continues the run.
        now = time.monotonic()
        last = _session.last_press
        double_time = context.preferences.inputs.mouse_double_click_time / 1000.0
        slop = CLICK_SLOP * px
        if last is not None and now - last[0] < 0.01:
            return  # the same press reported twice (PRESS + DOUBLE_CLICK)
        near = last is not None and abs(mx - last[1]) <= slop and abs(my - last[2]) <= slop
        _session.clicks = _session.clicks % 3 + 1 if near and now - last[0] < double_time else 1
        _session.last_press = (now, mx, my)
        # Past the end of a line (or below the last one): the click is "outside" the text.
        past_end = row >= len(buf.lines) or col > len(buf.lines[max(row, 0)])
        line_row = min(max(row, 0), len(buf.lines) - 1)
        if event.shift and _session.clicks == 1:
            buf.click(row, col, select=True)
        elif _session.clicks == 1:
            buf.click(row, col)
            _session.dragging = True
        elif _session.clicks == 2 and not past_end:
            buf.select_at(row, col)
        else:  # double-click past the end, or triple-click: the whole line
            buf.select_line(line_row)
        if DEBUG_CLICKS:
            msg = (f"BEX click x{_session.clicks} [{event.value}] row {row} col {col}"
                   f"{' past-end' if past_end else ''} -> {buf.selection_range()!r} "
                   f"{buf.selected_text()!r}  (double-click time {double_time:.2f}s)")
            print(msg)
            context.workspace.status_text_set(msg)
        self._after_change(node, version, row_before)

    def _bar_press(self, node, rect, event):
        """A press on a scrollbar: grab the thumb, or jump there when on the track."""
        x, y = event.mouse_region_x, event.mouse_region_y
        hit = BAR_HIT * rect[4]
        for axis, (track, knob, _total, _visible) in scrollbars(node, rect, _session.buf.lines, _session.view).items():
            # The grab area is bigger than the drawn bar, but stays inside the box.
            hx0, hy0 = max(track[0] - hit, rect[0]), max(track[1] - hit, rect[1])
            hx1, hy1 = min(track[2] + hit, rect[2]), min(track[3] + hit, rect[3])
            if hx0 <= x <= hx1 and hy0 <= y <= hy1:
                if axis == "v":
                    y = min(max(y, track[1]), track[3])
                    on = knob[1] <= y <= knob[3]
                    grab = knob[3] - y if on else (knob[3] - knob[1]) / 2
                else:
                    x = min(max(x, track[0]), track[2])
                    on = knob[0] <= x <= knob[2]
                    grab = x - knob[0] if on else (knob[2] - knob[0]) / 2
                _session.bar = (axis, grab)
                _session.suggestion = ""
                self._bar_drag(node, rect, event)
                return True
        return False

    def _bar_drag(self, node, rect, event):
        axis, grab = _session.bar
        view, lines = _session.view, _session.buf.lines
        bar = scrollbars(node, rect, lines, view).get(axis)
        if bar is None:
            return
        track, knob, total, visible = bar
        if axis == "v":
            size = knob[3] - knob[1]
            view.top = thumb_first(track[3] - event.mouse_region_y - grab, size, total, visible, track[3] - track[1])
        else:
            size = knob[2] - knob[0]
            view.left = thumb_first(event.mouse_region_x - track[0] - grab, size, total, visible, track[2] - track[0])

    def _start_on(self, context, node, rect, event):
        global _session
        _session = Session(node)
        self._select(context, node)
        if not self._bar_press(node, rect, event):
            self._click(context, node, rect, event)

    def _select(self, context, node):
        tree = node.id_data
        for n in tree.nodes:
            n.select = n == node
        tree.nodes.active = node

    def _end(self, context, node, result):
        global _session
        if node is not None:
            _session.push(node)
            node.sync()  # losing focus is when the code is evaluated
        changed = node is not None and _session.written != _session.start_text
        _session.view.left = 0  # sideways scrolling is for editing; the resting box starts at column 0
        _session = None
        if DEBUG_CLICKS:
            context.workspace.status_text_set(None)
        _redraw(context)
        if changed:  # the whole session is one step in Blender's undo
            try:
                bpy.ops.ed.undo_push(message="Edit BEX Code")
            except RuntimeError:
                pass
        return result


class BEX_OT_code_scroll(bpy.types.Operator):
    """Wheel / trackpad over a wrangle's code scrolls it; anywhere else it passes through."""

    bl_idname = "bex.code_scroll"
    bl_label = "Scroll BEX Code"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR" and _session is None

    def invoke(self, context, event):
        node, _rect = wrangle_at(context, event.mouse_region_x, event.mouse_region_y)
        if node is None or not scroll_code(node, context.region, event):
            return {"PASS_THROUGH"}
        _redraw(context)
        return {"FINISHED"}


classes = (BEX_OT_code_edit, BEX_OT_code_scroll)


def register():
    global _draw_handle
    for cls in classes:
        bpy.utils.register_class(cls)
    _draw_handle = bpy.types.SpaceNodeEditor.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _draw_handle, _session
    _session = None
    if _draw_handle is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
