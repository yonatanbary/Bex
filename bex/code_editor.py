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
import time

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .editor.buffer import TextBuffer, is_word_char
from .editor.complete import suggest
from .editor.highlight import highlight
from .lang.compiler import BUILTINS
from .editor.keys import EXIT, ZOOM_IN, ZOOM_OUT, ZOOM_RESET, View, handle_key

FONT_ID = 1             # Blender's built-in monospace font
FONT_SIZE = 11.0        # all sizes are node-editor units at zoom 1, before UI scale
LINE_H = 15.0
PAD = 5.0
MIN_ROWS = 3
MAX_ROWS = 40           # longer scripts scroll inside the box
UNIT_Y = 20.0           # height of one layout row (Blender's UI_UNIT_Y)
TRIPLE_CLICK = 0.45     # seconds after a double-click in which a click selects the line
ZOOM_STEP = 0.1

# The info strip under the code (attributes read / written). It sits inside the
# reserved block, so it needs no layout measuring of its own.
INFO_FONT = 9.0
INFO_LINE_H = 12.0
INFO_LINES = 2
INFO_GAP = 4.0

# A calm palette: mostly neutral text, colour only where it helps you read.
PALETTE = {
    "back": (0.105, 0.105, 0.11, 1.0),
    "focus": (0.36, 0.52, 0.78, 1.0),
    "evaluated": (0.36, 0.62, 0.42, 0.85),
    "error": (0.80, 0.36, 0.34, 0.9),
    "text": (0.80, 0.80, 0.80, 1.0),
    "dim": (0.52, 0.52, 0.53, 1.0),
    "comment": (0.44, 0.47, 0.44, 1.0),
    "keyword": (0.56, 0.67, 0.84, 1.0),
    "attr": (0.55, 0.78, 0.74, 1.0),
    "number": (0.84, 0.70, 0.55, 1.0),
    "string": (0.67, 0.78, 0.58, 1.0),
    "function": (0.88, 0.86, 0.78, 1.0),
    "selection": (0.30, 0.42, 0.62, 0.55),
    "cursor": (0.90, 0.90, 0.90, 1.0),
    "ghost": (0.50, 0.50, 0.52, 0.85),
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


# -- geometry ------------------------------------------------------------------
def visible_rows(line_count):
    return min(max(line_count, MIN_ROWS), MAX_ROWS)


def box_height(line_count, scale=1.0):
    return visible_rows(line_count) * LINE_H * scale + 2 * PAD


def info_height():
    return INFO_GAP + INFO_LINES * INFO_LINE_H


def reserve(layout, node):
    """Empty block in the node body the size of the code box plus the info strip."""
    col = layout.column()
    col.scale_y = (box_height(len(node.script.lines), node.code_scale) + info_height()) / UNIT_Y
    col.label(text="")


def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _zoom(region):
    v2d = region.view2d
    return v2d.view_to_region(1000, 0, clip=False)[0] - v2d.view_to_region(0, 0, clip=False)[0]


def box_rect(node, region):
    """(x0, y0, x1, y1, px) of the code box in region pixels; px = pixels per unit.
    The info strip is the `info_height()` just below y0."""
    s = _ui_scale()
    px = _zoom(region) / 1000.0 * s
    loc = node.location_absolute
    inputs = sum(1 for sock in node.inputs if sock.enabled and not sock.hide)
    left_v, top_v = loc.x * s, loc.y * s
    bottom_v = top_v - node.dimensions.y
    v2d = region.view2d
    x0, y_bottom_node = v2d.view_to_region(left_v, bottom_v, clip=False)
    x1 = v2d.view_to_region(left_v + node.width * s, 0, clip=False)[0]
    y0 = y_bottom_node + (BOTTOM_PAD + inputs * SOCKET_ROW + info_height()) * px
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


def _shows_code(node):
    return (node.bl_idname == "GeometryNodeBexWrangle" and node.script is not None
            and node.show_code and node.show_options and not node.hide and node.dimensions.y > 0)


def _metrics(px, scale):
    blf.size(FONT_ID, FONT_SIZE * scale * px)
    return LINE_H * scale * px, blf.dimensions(FONT_ID, "M" * 10)[0] / 10


# -- session -------------------------------------------------------------------
class Session:
    """One wrangle being edited."""

    def __init__(self, node):
        self.tree_name = node.id_data.name
        self.node_name = node.name
        self.buf = TextBuffer(node.script.as_string())
        self.written = self.buf.text   # what we last wrote to the Text
        self.view = View()
        self.cols = 40                 # visible columns, updated on draw
        self.dragging = False
        self.last_double = None        # (time, row) of the last double-click, for triple-click
        self.suggestion = ""           # inline completion shown after the cursor

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


def _draw_node(node, region, vx, vy):
    x0, y0, x1, y1, px = box_rect(node, region)
    info_y0 = y0 - info_height() * px
    if x1 < 0 or y1 < 0 or x0 > region.width or info_y0 > region.height or x1 - x0 < 4:
        return
    editing = is_editing(node)
    if editing:
        lines, view = _session.buf.lines, _session.view
    else:
        lines, view = node.script.as_string().split("\n"), View()
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
    if edge:
        w = max(1.0, px)
        for r in ((x0, y0, x1, y0 + w), (x0, y1 - w, x1, y1), (x0, y0, x0 + w, y1), (x1 - w, y0, x1, y1)):
            _rect(*r, edge)
    if FONT_SIZE * scale * px < 3:  # too far out to read
        return
    _draw_info(node, x0, info_y0, x1, y0, px, vx, vy)
    line_h, char_w = _metrics(px, scale)
    pad = PAD * px
    left, top = x0 + pad, y1 - pad
    rows = visible_rows(len(lines))
    cols = max(1, int((x1 - x0 - 2 * pad) / char_w))
    if editing:
        _session.cols = cols
        view.clamp(len(lines), rows)
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(vx + x0), int(vy + y0), int(x1 - x0) + 1, int(y1 - y0) + 1)

    def col_x(col):
        return left + (col - view.left) * char_w

    first, last = view.top, min(len(lines), view.top + rows)
    if editing:
        sel = _session.buf.selection_range()
        for row in range(first, last):
            y_top = top - (row - first) * line_h
            if sel and sel[0][0] <= row <= sel[1][0]:
                c0 = sel[0][1] if row == sel[0][0] else 0
                c1 = sel[1][1] if row == sel[1][0] else len(lines[row]) + 1
                _rect(col_x(c0), y_top - line_h, col_x(c1), y_top, PALETTE["selection"])
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
    if editing:
        row, col = _session.buf.cursor
        if first <= row < last:
            y_top = top - (row - first) * line_h
            w = max(1.0, 1.5 * px)
            _rect(col_x(col), y_top - line_h, col_x(col) + w, y_top, PALETTE["cursor"])


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
    """The two lines under the code: attributes read from the incoming geometry,
    and attributes written plus the node count."""
    try:
        info = json.loads(node.info) if node.info else {}
    except ValueError:
        info = {}

    def names(pairs):
        return "  ".join(_attr_label(n, t) for n, t in pairs) or "–"

    reads = names(info.get("reads", []))
    writes = names(info.get("writes", []))
    count = info.get("nodes")
    tail = f"   ·  {count} nodes" if count is not None else ""
    return [("reads", reads), ("writes", writes + tail)]


def _attr_label(name, type):
    if name in BUILTINS:
        return "@" + name
    return _TYPE_PREFIX.get(type, "") + "@" + name


def _draw_info(node, x0, y0, x1, y1, px, vx, vy):
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(vx + x0), int(vy + y0), int(x1 - x0) + 1, int(y1 - y0) + 1)
    blf.size(FONT_ID, INFO_FONT * px)
    label_w = blf.dimensions(FONT_ID, "writes  ")[0]
    line_h = INFO_LINE_H * px
    y = y1 - INFO_GAP * px - line_h + (line_h - INFO_FONT * px) * 0.5 + 0.2 * INFO_FONT * px
    for label, value in info_lines(node):
        blf.color(FONT_ID, *PALETTE["comment"])
        blf.position(FONT_ID, x0 + PAD * px, y, 0)
        blf.draw(FONT_ID, label)
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
        self._click(context, node, rect, event)
        context.window_manager.modal_handler_add(self)
        _redraw(context)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        node = _session.node() if _session else None
        if node is None or not node.show_code:
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
            if kind in ("WHEELUPMOUSE", "WHEELDOWNMOUSE"):
                hit, _ = wrangle_at(context, x, y)
                lines = len(_session.buf.lines)
                if hit == node and lines > MAX_ROWS:
                    _session.view.scroll(-3 if kind == "WHEELUPMOUSE" else 3, lines, MAX_ROWS)
                    return None
            return {"PASS_THROUGH"}
        if kind == "MOUSEMOVE":
            if _session.dragging:
                self._click(context, node, box_rect(node, context.region), event, drag=True)
                return None
            return {"PASS_THROUGH"}
        if kind == "LEFTMOUSE":
            if value == "RELEASE":
                _session.dragging = False
                return None
            hit, rect = wrangle_at(context, x, y)
            if hit is None:  # click outside: finish and let the click through
                return self._end(context, node, {"FINISHED", "PASS_THROUGH"})
            if hit != node:
                self._end(context, node, {"FINISHED"})
                self._start_on(context, hit, rect, event)
                return None
            self._click(context, node, rect, event, double=value == "DOUBLE_CLICK")
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
            _session.suggestion = suggest(buf.lines, *buf.cursor)
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

    def _click(self, context, node, rect, event, double=False, drag=False):
        buf = _session.buf
        x0, _y0, _x1, y1, px = rect
        line_h, char_w = _metrics(px, node.code_scale)
        row, col = _session.view.hit(event.mouse_region_x - x0 - PAD * px, y1 - PAD * px - event.mouse_region_y,
                                     line_h, char_w)
        version, row_before = buf.version, buf.cursor[0]
        _session.suggestion = ""
        # Past the end of a line (or below the last one): the click is "outside" the text.
        past_end = row >= len(buf.lines) or col > len(buf.lines[max(row, 0)])
        last = _session.last_double
        triple = (not double and not drag and last is not None
                  and time.monotonic() - last[0] < TRIPLE_CLICK and last[1] == row)
        if double:
            _session.last_double = (time.monotonic(), row)
            if past_end:
                buf.select_line(min(row, len(buf.lines) - 1))
            else:
                buf.select_at(row, col)
        elif triple:
            _session.last_double = None
            buf.select_line(row)
        else:
            if not drag:
                _session.last_double = None
            buf.click(row, col, select=drag or event.shift)
            _session.dragging = not drag or _session.dragging
        self._after_change(node, version, row_before)

    def _start_on(self, context, node, rect, event):
        global _session
        _session = Session(node)
        self._select(context, node)
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
        _session = None
        _redraw(context)
        if node is not None:
            try:
                bpy.ops.ed.undo_push(message="Edit BEX Code")
            except RuntimeError:
                pass
        return result


classes = (BEX_OT_code_edit,)


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
