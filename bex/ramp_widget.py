"""chramp() ramps on the node, Blender side: the data, the curve it drives, and the
Houdini-style widget (header buttons, a custom-drawn graph, the point's fields).

The keys live on the wrangle node (`node.bex_ramps`, see `BexRamp`), so undo,
duplicating and saving just work. The node graph evaluates each ramp with a Float
Curve inside the wrangle's group: `write_curve` fills it with `ramp.polyline()`
whenever a key changes, so dragging updates the result live.
"""

import math

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import builder, code_editor as ce
from . import ramp as R
from . import ramp_icons

# -- sizes (node-editor units at zoom 1, before UI scale) -----------------------
GRAPH_H = 110.0      # the graph, unfolded
STRIP_H = 34.0       # folded: a strip, still editable by dragging
MARKER_H = 16.0      # the row of position markers under the graph
MARKER_GAP = 4.0     # between the graph's bottom edge and a marker's tip
KEY_R = 4.0          # key dot radius
ARROW_W = 3.0        # half width of the triangle drawn for a point whose value is past 0-1
HIT_R = 8.0          # how close a press must be to grab a key or marker
MARKER_W = 5.0       # half width of a marker
TOGGLES_W = 40.0     # the 2x2 button grid left of the graph (add / delete, point fields / fold)
TOGGLES_GAP = 4.0    # between that column and the graph
# Where the graph goes is counted down from the node's top edge, in Blender pixels
# rounded the way Blender rounds them (see `_row_sizes`), so nothing below a graph
# (its point fields, the code box) can move it. Python can't read widget positions.
BODY_TOP = 16.7      # Blender px: the gap between the output socket row and the first field (measured, 5.2)
RAMP_LIMIT = 16      # remembered ramps no longer in the script, per node
GRID = 4             # grid divisions, the same both ways: cells are square only when the graph is
# The ramp's name is drawn by us, in bold: Blender's layout and blf can't set a font
# weight from Python. Measured against Blender's own labels (5.2).
LABEL_POINTS = 11.0  # Blender's UI text size (points)
LABEL_X = 0.8        # Blender px from the body's left edge to the text
LABEL_BASE = 13.8    # Blender px from the row's bottom to the text baseline
LABEL_BOLD = 1.4     # faux bold: the text is smeared this far to the right (Blender px)

PALETTE = {
    "back": (0.20, 0.20, 0.205, 1.0),
    "grid": (0.255, 0.255, 0.26, 1.0),
    "fill": (0.36, 0.36, 0.355, 1.0),
    "line": (0.80, 0.79, 0.72, 1.0),
    "key": (0.86, 0.92, 0.70, 1.0),
    "key_edge": (0.10, 0.10, 0.10, 1.0),
    "selected": (0.98, 0.76, 0.20, 1.0),
    "marker": (0.75, 0.75, 0.75, 1.0),
    "label": (0.94, 0.94, 0.94, 1.0),
}

_quiet = False     # set while many keys change at once; one curve write at the end
_previews = None   # preset thumbnails (bpy.utils.previews)


# -- data ----------------------------------------------------------------------
def _owner(key_or_ramp):
    """(node, ramp) owning a BexRampKey or BexRamp. RNA can't make paths into a
    custom node's properties, so it is looked up by pointer (a tree has few wrangles)."""
    want = key_or_ramp.as_pointer()
    for node in key_or_ramp.id_data.nodes:
        for ramp in getattr(node, "bex_ramps", ()):
            if ramp.as_pointer() == want or any(k.as_pointer() == want for k in ramp.keys):
                return node, ramp
    return None, None


def _key_changed(self, _context):
    if not _quiet:
        node, ramp = _owner(self)
        if ramp is not None:
            write_curve(node, ramp)


class BexRampKey(bpy.types.PropertyGroup):
    position: bpy.props.FloatProperty(name="Position", min=0.0, max=1.0, update=_key_changed, options=set(),
                                      description="Where the point sits along the ramp, 0 to 1")
    value: bpy.props.FloatProperty(name="Value", soft_min=0.0, soft_max=1.0, update=_key_changed, options=set(),
                                   description="The ramp's value at this point")
    interpolation: bpy.props.EnumProperty(name="Interpolation", items=R.INTERPOLATIONS, default=R.DEFAULT_INTERPOLATION,
                                          update=_key_changed, options=set(),
                                          description="How the ramp runs from this point to the next")
    select: bpy.props.BoolProperty(options=set())  # part of a Shift-click range


def _preset_items(_self, _context):
    icons = _preset_icons()
    return [(name, name, "", icons.get(name, 0), i) for i, name in enumerate(R.PRESETS)]


def _get_preset(self):
    return self.preset_index


def _set_preset(self, index):
    self.preset_index = index
    node, _ = _owner(self)
    if node is not None:
        set_keys(node, self, R.PRESETS[list(R.PRESETS)[index]])


def _get_point(self):
    return self.selected + 1


def _set_point(self, number):
    if self.keys:
        select_only(self, max(0, min(len(self.keys) - 1, number - 1)))


_INTERP_ITEMS = []  # kept alive: Blender doesn't copy a dynamic enum's strings


def _interp_items(_self, _context):
    if not _INTERP_ITEMS:
        _INTERP_ITEMS.extend((mode, label, desc + " (every point)", ramp_icons.icon("INTERP_" + mode), i)
                             for i, (mode, label, desc) in enumerate(R.INTERPOLATIONS))
    return _INTERP_ITEMS


_INTERP_IDS = [mode for mode, _l, _d in R.INTERPOLATIONS]


def _get_interp(self):
    """The selected point's interpolation (when every point shares one, that one)."""
    if not self.keys:
        return _INTERP_IDS.index(R.DEFAULT_INTERPOLATION)
    return _INTERP_IDS.index(self.keys[min(self.selected, len(self.keys) - 1)].interpolation)


def _set_interp(self, index):
    node, _ = _owner(self)
    if node is not None:
        set_keys(node, self, [(p, v, _INTERP_IDS[index]) for p, v, _i in keys_of(self)])


class BexRamp(bpy.types.PropertyGroup):
    # `name` (built in) is the chramp() name.
    keys: bpy.props.CollectionProperty(type=BexRampKey)
    selected: bpy.props.IntProperty(min=0, options=set())
    expanded: bpy.props.BoolProperty(name="Unfold", default=False, options=set(),
                                     description="Show the full graph instead of a strip")
    show_point: bpy.props.BoolProperty(name="Point", default=False, options=set(),
                                       description="Show the selected point's position, value and interpolation")
    point: bpy.props.IntProperty(name="Point No.", min=1, get=_get_point, set=_set_point, options=set(),
                                 description="The selected point")
    preset_index: bpy.props.IntProperty(default=list(R.PRESETS).index("Linear"), options=set())
    preset: bpy.props.EnumProperty(name="Preset", items=_preset_items, get=_get_preset, set=_set_preset,
                                   options=set(), description="Replace the ramp with a preset shape")
    interpolation: bpy.props.EnumProperty(name="Interpolation", items=_interp_items, get=_get_interp,
                                          set=_set_interp, options=set(),
                                          description="Set how the ramp runs between points, for every point")


def ui_label(name):
    """How a script name is shown on the node: first letter upper case, like Houdini."""
    return name[:1].upper() + name[1:]


def keys_of(ramp):
    return [(k.position, k.value, k.interpolation) for k in ramp.keys]


def set_keys(node, ramp, keys):
    """Replace a ramp's keys (one curve write). Nothing is selected afterwards."""
    global _quiet
    _quiet = True
    try:
        ramp.keys.clear()
        for p, v, interp in keys:
            k = ramp.keys.add()
            k.position, k.value, k.interpolation = p, v, interp
        ramp.selected = min(ramp.selected, max(0, len(ramp.keys) - 1))
    finally:
        _quiet = False
    write_curve(node, ramp)


# -- selection: the selected points (`key.select`, highlighted; Delete removes them)
# and the active one (`ramp.selected`: shown in the point fields, the anchor of a
# Shift-click range). A click anywhere else clears the selection, not the active point.
def selected_indices(ramp):
    return [i for i, k in enumerate(ramp.keys) if k.select]


def deselect(ramp):
    for k in ramp.keys:
        if k.select:
            k.select = False


def deselect_tree(tree, keep=None):
    """Clear every ramp's selection in `tree` but `keep`'s; True if any was selected."""
    found = False
    for node in tree.nodes:
        for ramp in getattr(node, "bex_ramps", ()):
            if ramp != keep and selected_indices(ramp):
                deselect(ramp)
                found = True
    return found


def select_only(ramp, index):
    for i, k in enumerate(ramp.keys):
        k.select = i == index
    ramp.selected = index


def select_range(ramp, index):
    """Select every point from the active one to `index`, in position order (the
    active point stays the anchor for the next Shift-click). With nothing selected
    it starts a new range at `index`."""
    if not selected_indices(ramp):
        select_only(ramp, index)
        return
    order = sorted(range(len(ramp.keys)), key=lambda i: ramp.keys[i].position)
    anchor = min(ramp.selected, len(ramp.keys) - 1)
    a, b = sorted((order.index(anchor), order.index(index)))
    for i, k in enumerate(ramp.keys):
        k.select = i in order[a:b + 1]


def delete_selected(node, ramp, doomed=None):
    """Delete the selected points (or `doomed`); a ramp keeps at least one."""
    doomed = selected_indices(ramp) if doomed is None else doomed
    if len(doomed) >= len(ramp.keys):
        doomed = [i for i in doomed if i != min(ramp.selected, len(ramp.keys) - 1)]
    if not doomed:
        return
    survivor = next(i for i in range(len(ramp.keys)) if i not in doomed)
    keys = [k for i, k in enumerate(keys_of(ramp)) if i not in doomed]
    ramp.selected = survivor - sum(1 for i in doomed if i < survivor)
    set_keys(node, ramp, keys)


def ensure_ramps(node, names):
    """A BexRamp for every chramp() name (a new one starts straight); ramps no
    longer used are kept for a while, so a renamed ramp comes back with its shape."""
    ramps = node.bex_ramps
    for name in names:
        if ramps.get(name) is None:
            r = ramps.add()
            r.name = name
            set_keys(node, r, R.DEFAULT_KEYS)
    stale = [i for i, r in enumerate(ramps) if r.name not in names]
    for i in reversed(stale[:max(0, len(stale) - RAMP_LIMIT)]):
        ramps.remove(i)


def polylines(node, names):
    """{name: polyline} for the builder."""
    return {n: R.polyline(keys_of(node.bex_ramps[n])) for n in names if node.bex_ramps.get(n) is not None}


def write_curve(node, ramp):
    """Put the ramp's shape into its Float Curve and re-evaluate."""
    curve = builder.ramp_node(node.node_tree, ramp.name)
    if curve is None:
        return
    builder.set_curve(curve, R.polyline(keys_of(ramp)))
    node.node_tree.update_tag()
    ce._redraw(bpy.context)


# -- layout --------------------------------------------------------------------
def graph_height(ramp):
    return (GRAPH_H if ramp.expanded else STRIP_H) + MARKER_H


def draw_layout(layout, node, ramp, field):
    """The ramp's header row, the space the graph is drawn into, and the
    selected point's fields. `field(layout, node, label, width)` is node._field."""
    row = layout.row(align=True)
    row.label(text="")  # the name is drawn in bold by draw_node
    tools = row.row(align=True)
    tools.alignment = "RIGHT"
    tools.emboss = "PULLDOWN_MENU"  # one look for menus and buttons: no background until hovered
    # Like Houdini: the interpolation menu (its icon is the current mode's shape),
    # the two flips, then the presets.
    # A menu button without text: square, no arrow; it opens below, left-aligned.
    tools.context_pointer_set("bex_ramp", ramp)
    tools.menu(NODE_MT_bex_ramp_interpolation.bl_idname, text="",
               icon_value=ramp_icons.icon("INTERP_" + ramp.interpolation))
    for action, icon in (("REVERSE", "FLIP_X"), ("INVERT", "FLIP_Y")):
        op = tools.operator("bex.ramp_action", text="", icon_value=ramp_icons.icon(icon))
        op.ramp, op.action = ramp.name, action
    tools.template_icon_view(ramp, "preset", show_labels=True, scale=1.0, scale_popup=4.0)
    # Left of the graph, under the label: a 2x2 grid of buttons, add / delete over
    # the point-fields and fold toggles. The split is by fraction, so it comes from
    # the width the code box also spans.
    body = layout.split(factor=min(0.5, TOGGLES_W / max(1.0, node.width - 2 * ce.SIDE)))
    grid = body.column(align=True)
    edit = grid.row(align=True)
    for action, icon in (("ADD", "ADD"), ("DELETE", "X")):
        op = edit.operator("bex.ramp_action", text="", icon=icon)
        op.ramp, op.action = ramp.name, action
    toggles = grid.row(align=True)
    toggles.emboss = "PULLDOWN_MENU"  # no background until hovered (their icons show the state)
    toggles.prop(ramp, "show_point", text="", icon="DISCLOSURE_TRI_DOWN" if ramp.show_point else "DISCLOSURE_TRI_RIGHT")
    toggles.prop(ramp, "expanded", text="", icon_value=ramp_icons.icon("FOLD" if ramp.expanded else "UNFOLD"))
    space = body.column()
    space.scale_y = graph_height(ramp) / ce.UNIT_Y
    space.label(text="")
    if ramp.show_point and ramp.keys:
        key = ramp.keys[min(ramp.selected, len(ramp.keys) - 1)]
        field(layout, node, "Point No.", 80).prop(ramp, "point", text="")
        fields = layout.column()
        fields.enabled = len(selected_indices(ramp)) < 2  # with several points selected the fields are read-only
        field(fields, node, "Position").prop(key, "position", text="", slider=True)
        field(fields, node, "Value").prop(key, "value", text="", slider=True)
        field(fields, node, "Interpolation", 130).prop(key, "interpolation", text="")


def _row_sizes():
    """(row, gap) in Blender pixels: the height of one row of buttons (U.widget_unit)
    and the space the layout puts between rows (style buttonspacey). Blender rounds
    both to whole pixels (round and truncate), so at UI scale 1.1 a row is 44 + 10,
    not 1.1 x (20 + 5)."""
    system = bpy.context.preferences.system
    scale = system.ui_scale or 1.0  # includes the Retina pixel size; 0 without a window (background)
    row = math.floor(18.0 * scale + 0.5 + 1e-4) + 2 * system.pixel_size
    return row, int(5.0 * scale + 1e-4)


def ramp_rects(node, region):
    """{name: (graph rect, marker rect, label baseline (x, y))} in region pixels, rects (x0, y0, x1, y1);
    and px. Counted down from the node's top, following `draw_buttons`: header and
    output socket, Group, Run Over, the ch*() column (aligned: no gaps), then each
    ramp's label row, graph row and point fields."""
    x0, _y0, x1, _y1, px = ce.box_rect(node, region)
    top, zoom = ce.node_top(node, region)
    row, gap = _row_sizes()
    unit = row / ce.UNIT_Y  # Blender px per layout unit: a label scaled by h / UNIT_Y is h * unit tall
    gx0 = x0 + (TOGGLES_W + TOGGLES_GAP) * px  # right of the toggle buttons
    y = 2 * row + BODY_TOP + 2 * (row + gap)  # down to below Run Over
    chans = len(node.channels())
    if chans:
        y += chans * row + gap
    rects = {}
    for ramp in shown_ramps(node):
        label = (x0 + LABEL_X * zoom, top - (y + row - LABEL_BASE) * zoom)  # x0: the body's left edge
        y += row + gap  # the label row
        h = graph_height(ramp) * unit
        graph_top = top - y * zoom
        markers = (gx0, graph_top - h * zoom, x1, graph_top - (h - MARKER_H * unit) * zoom)
        rects[ramp.name] = ((gx0, markers[3], x1, graph_top), markers, label)
        y += max(2 * row, h) + gap + (4 * (row + gap) if ramp.show_point else 0)
    return rects, px


def shown_ramps(node):
    return [node.bex_ramps[n] for n in node.ramps()]


# -- drawing -------------------------------------------------------------------
# The graph rect is exactly the 0-1 square, like Houdini: position 0 / value 0 is
# its bottom-left corner. Dots and markers at the ends hang over its edges.
def _to_px(graph, x, y):
    return graph[0] + x * (graph[2] - graph[0]), graph[1] + y * (graph[3] - graph[1])


def _from_px(graph, mx, my):
    w, h = max(1.0, graph[2] - graph[0]), max(1.0, graph[3] - graph[1])
    return (mx - graph[0]) / w, (my - graph[1]) / h


def _tris(points, color):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", ce._ink(color))
    batch_for_shader(shader, "TRIS", {"pos": points}).draw(shader)


def _line(region, points, color, width):
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", ce._ink(color))
    batch_for_shader(shader, "LINE_STRIP", {"pos": points}).draw(shader)


def _dot(x, y, r, color, segments=12):
    import math
    ring = [(x + r * math.cos(2 * math.pi * i / segments), y + r * math.sin(2 * math.pi * i / segments))
            for i in range(segments + 1)]
    _tris([p for a, b in zip(ring, ring[1:]) for p in ((x, y), a, b)], color)


def _arrow(x, y, direction, px, color):
    """A point whose value is off the graph: a triangle with its tip on the edge at
    (x, y), pointing up (direction 1, above 1) or down (-1, below 0), outlined like a dot."""
    def tri(w, inset):
        tip = y - direction * inset
        base = tip - direction * 2 * w
        return [(x, tip), (x - w, base), (x + w, base)]
    _tris(tri((ARROW_W + 1.0) * px, 0.0), PALETTE["key_edge"])
    _tris(tri(ARROW_W * px, 1.0 * px), color)


def _draw_label(region, text, at, px):
    """The ramp's name at baseline `at`, in the UI font, drawn twice for a bold look."""
    x, y = at
    size = LABEL_POINTS * px
    if not (-size < y < region.height + size and x < region.width):
        return
    blf.size(0, size)
    blf.color(0, *ce._ink(PALETTE["label"]))
    bold = LABEL_BOLD * px / (ce._ui_scale() or 1.0)  # px / ui_scale = zoom
    for dx in (0.0, bold / 2, bold):
        blf.position(0, x + dx, y, 0)
        blf.draw(0, text)


def draw_node(node, region, vx, vy):
    """Paint every ramp graph of a wrangle (called from code_editor's draw handler)."""
    if not node.bex_ramps:
        return
    rects, px = ramp_rects(node, region)
    for ramp in shown_ramps(node):
        graph, markers, label = rects[ramp.name]
        _draw_label(region, ui_label(ramp.name), label, px)
        if graph[2] < 0 or graph[0] > region.width or markers[1] > region.height or graph[3] < 0:
            continue
        gpu.state.blend_set("ALPHA")
        ce._rect(*graph, PALETTE["back"])
        if not ce._clip(region, vx, vy, *graph):
            continue
        if ramp.expanded:  # the 0-1 square divided the same both ways, so it squashes with the graph
            for i in range(1, GRID):
                gx = graph[0] + (graph[2] - graph[0]) * i / GRID
                gy = graph[1] + (graph[3] - graph[1]) * i / GRID
                ce._rect(round(gx), graph[1], round(gx) + 1, graph[3], PALETTE["grid"])
                ce._rect(graph[0], round(gy), graph[2], round(gy) + 1, PALETTE["grid"])
        keys = keys_of(ramp)
        pts = [_to_px(graph, x, y) for x, y in R.polyline(keys)]  # spans x = 0..1
        _tris([p for a, b in zip(pts, pts[1:]) for p in (a, b, (a[0], graph[1]), b, (b[0], graph[1]), (a[0], graph[1]))],
              PALETTE["fill"])
        _line(region, pts, PALETTE["line"], max(1.0, 1.2 * px))
        picked = [k.select for k in ramp.keys]
        # Dots may hang over the graph's edges (a key at 0 or 1).
        gpu.state.scissor_test_set(False)
        r = (KEY_R + 1) * px
        if not ce._clip(region, vx, vy, graph[0] - r, graph[1] - r, graph[2] + r, graph[3] + r):
            continue
        for i, (x, y, _interp) in enumerate(keys):
            cx, cy = _to_px(graph, x, _clamp01(y))
            color = PALETTE["selected" if picked[i] else "key"]
            if 0.0 <= y <= 1.0:
                _dot(cx, cy, (KEY_R + 1) * px, PALETTE["key_edge"])
                _dot(cx, cy, KEY_R * px, color)
            else:  # off the graph: a triangle on its edge, pointing to where the point is
                _arrow(cx, cy, 1.0 if y > 1.0 else -1.0, px, color)
        # Position markers under the graph.
        gpu.state.scissor_test_set(False)
        w = MARKER_W * px
        if ce._clip(region, vx, vy, markers[0] - w, markers[1], markers[2] + w, markers[3]):
            for i, (x, _y, _interp) in enumerate(keys):
                cx = _to_px(graph, x, 0)[0]
                top, bottom = markers[3] - MARKER_GAP * px, markers[1] + 1 * px
                mid = top - w
                color = PALETTE["selected" if picked[i] else "marker"]
                _tris([(cx, top), (cx - w, mid), (cx + w, mid)], color)
                ce._rect(cx - w, bottom, cx + w, mid, color)
        gpu.state.scissor_test_set(False)


# -- presets thumbnails --------------------------------------------------------

def _preset_icons():
    """{preset name: icon id}: thumbnails drawn from the same ramp maths."""
    global _previews
    if _previews is None:
        import bpy.utils.previews
        _previews = bpy.utils.previews.new()
    icons = {}
    for name, keys in R.PRESETS.items():
        pv = _previews.get(name)
        if pv is None:
            pv = _previews.new(name)
            size = ramp_icons.THUMB
            pv.image_size = (size, size)
            pv.image_pixels_float = ramp_icons.thumbnail(keys, size)
        icons[name] = pv.icon_id
    return icons


# -- editing -------------------------------------------------------------------
def ramp_at(context, x, y):
    """(node, ramp, graph rect, marker rect, px) of the ramp under region point (x, y)."""
    tree = getattr(context.space_data, "edit_tree", None)
    if tree is None:
        return None
    for node in reversed(tree.nodes[:]):
        if not ce._shows_code(node) or node.mute or not node.bex_ramps:
            continue
        rects, px = ramp_rects(node, context.region)
        for ramp in shown_ramps(node):
            graph, markers, _label = rects[ramp.name]
            reach = HIT_R * px  # end keys hang over the edges
            if graph[0] - reach <= x <= graph[2] + reach and markers[1] <= y <= graph[3] + reach:
                return node, ramp, graph, markers, px
    return None


def _clamp01(v):
    return min(max(v, 0.0), 1.0)


def _inside(rect, x, y):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _segment_interpolation(keys, x):
    """The interpolation of the stretch containing x (a new key there keeps the shape's style)."""
    before = [k for k in sorted(keys) if k[0] <= x]
    return before[-1][2] if before else (sorted(keys)[0][2] if keys else R.DEFAULT_INTERPOLATION)


class BEX_OT_ramp_edit(bpy.types.Operator):
    """Drag a ramp's points (Shift-click selects a range, which drags as one); click
    empty space to add a point, Ctrl+click a point to delete it"""

    bl_idname = "bex.ramp_edit"
    bl_label = "Edit BEX Ramp"
    bl_options = {"INTERNAL", "UNDO"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR"

    def invoke(self, context, event):
        x, y = event.mouse_region_x, event.mouse_region_y
        hit = ramp_at(context, x, y)
        tree = getattr(context.space_data, "edit_tree", None)
        if hit is None:
            # A click outside the node clears the points' selection. On the node it
            # is kept: its buttons (delete, the point fields) act on the selection.
            if tree is not None:
                keep = [r for n in tree.nodes for r in getattr(n, "bex_ramps", ())
                        if selected_indices(r) and _inside(ce.node_rect(n, context.region), x, y)]
                if deselect_tree(tree, keep=keep[0] if keep else None):
                    ce._redraw(context)
            return {"PASS_THROUGH"}
        node, ramp, graph, markers, px = hit
        deselect_tree(node.id_data, keep=ramp)
        self.tree, self.node, self.ramp = node.id_data.name, node.name, ramp.name
        keys = keys_of(ramp)
        # A dot under the press wins, even where it hangs over the marker row
        # (a key at value 0); otherwise a press in the marker row takes the
        # nearest marker and drags only the position.
        grab, best = None, HIT_R * px
        for i, (kx, ky, _interp) in enumerate(keys):
            cx, cy = _to_px(graph, kx, _clamp01(ky))
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= best:
                grab, best = i, d
        on_markers = grab is None and y < markers[3]
        if on_markers:
            best = HIT_R * px
            for i, (kx, _ky, _interp) in enumerate(keys):
                d = abs(x - _to_px(graph, kx, 0)[0])
                if d <= best:
                    grab, best = i, d
        if grab is not None and event.ctrl:
            select_only(ramp, grab)
            delete_selected(node, ramp)
            return {"FINISHED"}
        if grab is None:
            if on_markers:
                deselect(ramp)
                ce._redraw(context)
                return {"FINISHED"}
            nx, ny = _from_px(graph, x, y)
            nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
            k = ramp.keys.add()
            k.interpolation = _segment_interpolation(keys, nx)
            k.position, k.value = nx, ny
            grab = len(ramp.keys) - 1
            select_only(ramp, grab)
        elif event.shift:
            select_range(ramp, grab)
        elif not ramp.keys[grab].select or len(selected_indices(ramp)) < 2:
            select_only(ramp, grab)
        # else: a press on a point of the selection drags them all; a click without
        # moving selects that point alone (on release).
        self.index, self.only_x = grab, on_markers
        self.collapse = not event.shift
        self.graph = graph
        self.press = _from_px(graph, x, y)
        self.start = {i: (ramp.keys[i].position, ramp.keys[i].value) for i in selected_indices(ramp)}
        self.moved = False
        context.window_manager.modal_handler_add(self)
        ce._redraw(context)
        return {"RUNNING_MODAL"}

    def _target(self):
        tree = bpy.data.node_groups.get(self.tree)
        node = tree.nodes.get(self.node) if tree else None
        ramp = node.bex_ramps.get(self.ramp) if node else None
        return node, ramp

    def modal(self, context, event):
        node, ramp = self._target()
        if ramp is None or self.index >= len(ramp.keys):
            return {"CANCELLED"}
        if event.type in ("MOUSEMOVE", "INBETWEEN_MOUSEMOVE"):
            nx, ny = _from_px(self.graph, event.mouse_region_x, event.mouse_region_y)
            dx, dy = nx - self.press[0], 0.0 if self.only_x else ny - self.press[1]
            # The selection moves as one: the move stops where its first point hits an edge.
            ps, vs = [p for p, _v in self.start.values()], [v for _p, v in self.start.values()]
            dx = min(max(dx, -min(ps)), 1.0 - max(ps))
            inside = [v for v in vs if 0.0 <= v <= 1.0]  # values past 0-1 (typed) don't stop it
            if inside:
                dy = min(max(dy, -min(inside)), 1.0 - max(inside))
            global _quiet
            _quiet = True
            try:
                for i, (p, v) in self.start.items():
                    if i < len(ramp.keys):
                        ramp.keys[i].position, ramp.keys[i].value = p + dx, v + dy
            finally:
                _quiet = False
            write_curve(node, ramp)
            self.moved = True
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if not self.moved and self.collapse:
                select_only(ramp, self.index)
                ce._redraw(context)
            return {"FINISHED"}
        if event.type in ("ESC", "RIGHTMOUSE"):
            return {"FINISHED"}
        return {"RUNNING_MODAL"}


ACTIONS = (("ADD", "Add", "Add a point between the selected one and the next"),
           ("DELETE", "Delete", "Delete the selected points"),
           ("REVERSE", "Reverse", "Flip the ramp left to right"),
           ("INVERT", "Invert", "Flip the ramp upside down"))


class BEX_OT_ramp_delete(bpy.types.Operator):
    """Delete the selected ramp points. Delete / X do this whenever points are
    selected (wherever the mouse is), and never touch the node then"""

    bl_idname = "bex.ramp_delete"
    bl_label = "Delete Ramp Points"
    bl_options = {"INTERNAL", "UNDO"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR"

    def invoke(self, context, _event):
        tree = getattr(context.space_data, "edit_tree", None)
        targets = [(node, ramp) for node in (tree.nodes if tree else ())
                   for ramp in getattr(node, "bex_ramps", ()) if selected_indices(ramp)]
        if not targets:
            return {"PASS_THROUGH"}  # no points selected: the key does what it always does
        for node, ramp in targets:
            delete_selected(node, ramp)
        ce._redraw(context)
        return {"FINISHED"}


class BEX_OT_ramp_interpolation(bpy.types.Operator):
    """Set how the ramp runs between points, for every point"""

    bl_idname = "bex.ramp_interpolation"
    bl_label = "Interpolation"
    bl_options = {"INTERNAL", "UNDO"}

    tree: bpy.props.StringProperty()
    node: bpy.props.StringProperty()
    ramp: bpy.props.StringProperty()
    mode: bpy.props.EnumProperty(items=[(m, label, desc) for m, label, desc in R.INTERPOLATIONS])

    @classmethod
    def description(cls, _context, props):
        return next(f"{label}: {desc}" for m, label, desc in R.INTERPOLATIONS if m == props.mode)

    def execute(self, _context):
        tree = bpy.data.node_groups.get(self.tree)
        node = tree.nodes.get(self.node) if tree else None
        ramp = node.bex_ramps.get(self.ramp) if node else None
        if ramp is None:
            return {"CANCELLED"}
        ramp.interpolation = self.mode
        return {"FINISHED"}


class NODE_MT_bex_ramp_interpolation(bpy.types.Menu):
    """The header's interpolation menu (the ramp comes from the layout's context)."""

    bl_idname = "NODE_MT_bex_ramp_interpolation"
    bl_label = "Interpolation"

    def draw(self, context):
        ramp = getattr(context, "bex_ramp", None)
        node = getattr(context, "node", None)
        if ramp is None or node is None:
            return
        for mode, label, _desc in R.INTERPOLATIONS:
            op = self.layout.operator(BEX_OT_ramp_interpolation.bl_idname, text=label,
                                      icon_value=ramp_icons.icon("INTERP_" + mode), depress=mode == ramp.interpolation)
            op.tree, op.node, op.ramp, op.mode = node.id_data.name, node.name, ramp.name, mode


class BEX_OT_ramp_action(bpy.types.Operator):
    """Change a ramp on the node"""

    bl_idname = "bex.ramp_action"
    bl_label = "BEX Ramp"
    bl_options = {"INTERNAL", "UNDO"}

    ramp: bpy.props.StringProperty()
    action: bpy.props.EnumProperty(items=ACTIONS)

    @classmethod
    def description(cls, _context, props):
        return next(d for a, _n, d in ACTIONS if a == props.action)

    def execute(self, context):
        node = getattr(context, "node", None)
        ramp = node.bex_ramps.get(self.ramp) if node else None
        if ramp is None:
            return {"CANCELLED"}
        keys = keys_of(ramp)
        sel = min(ramp.selected, len(keys) - 1)
        if self.action == "ADD":
            order = sorted(range(len(keys)), key=lambda i: keys[i][0])
            at = order.index(sel)
            x0 = keys[order[at]][0]
            x1 = keys[order[at + 1]][0] if at + 1 < len(order) else 1.0
            if at + 1 >= len(order) and x0 >= 1.0:  # the last key sits at the end: go left
                x1, x0 = x0, keys[order[at - 1]][0] if at > 0 else 0.0
            x = (x0 + x1) / 2
            keys.append((x, R.evaluate(keys, x), keys[sel][2]))
            set_keys(node, ramp, keys)
            select_only(ramp, len(keys) - 1)
        elif self.action == "DELETE":
            delete_selected(node, ramp, selected_indices(ramp) or [sel])
        else:
            new = R.reverse(keys) if self.action == "REVERSE" else R.invert(keys)
            selected_pos = keys[sel][0] if self.action == "INVERT" else 1.0 - keys[sel][0]
            had_selection = bool(selected_indices(ramp))
            set_keys(node, ramp, new)
            active = min(range(len(new)), key=lambda i: abs(new[i][0] - selected_pos))
            if had_selection:
                select_only(ramp, active)
            else:
                ramp.selected = active
        return {"FINISHED"}


classes = (BexRampKey, BexRamp, BEX_OT_ramp_edit, BEX_OT_ramp_delete, BEX_OT_ramp_interpolation, NODE_MT_bex_ramp_interpolation,
           BEX_OT_ramp_action)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _previews
    ramp_icons.unregister()
    _INTERP_ITEMS.clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
