"""chramp() ramps on the node, Blender side: the data, the curve it drives, and the
Houdini-style widget (header buttons, a custom-drawn graph, the point's fields).

The keys live on the wrangle node (`node.bex_ramps`, see `BexRamp`), so undo,
duplicating and saving just work. The node graph evaluates each ramp with a Float
Curve inside the wrangle's group: `write_curve` fills it with `ramp.polyline()`
whenever a key changes, so dragging updates the result live.
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import builder, code_editor as ce
from . import ramp as R

# -- sizes (node-editor units at zoom 1, before UI scale) -----------------------
GRAPH_H = 110.0      # the graph, unfolded
STRIP_H = 26.0       # folded: a strip, still editable by dragging
MARKER_H = 13.0      # the row of position markers under the graph
KEY_R = 4.0          # key dot radius
HIT_R = 8.0          # how close a press must be to grab a key or marker
MARKER_W = 5.0       # half width of a marker
# What sits between the code box and the last ramp, and each ramp's rows. The
# layout cannot be measured from Python, so these were measured from screenshots
# of Blender 5.2 (like code_editor's SOCKET_ROW).
ROW_H = 25.0         # one row of buttons or fields in the node body (pitch, with spacing)
SEPARATOR_H = 7.0    # layout.separator(factor=0.5, type="LINE")
RAMP_LIMIT = 16      # remembered ramps no longer in the script, per node

PALETTE = {
    "back": (0.20, 0.20, 0.205, 1.0),
    "grid": (0.255, 0.255, 0.26, 1.0),
    "fill": (0.36, 0.36, 0.355, 1.0),
    "line": (0.80, 0.79, 0.72, 1.0),
    "key": (0.86, 0.92, 0.70, 1.0),
    "key_edge": (0.10, 0.10, 0.10, 1.0),
    "selected": (0.98, 0.76, 0.20, 1.0),
    "marker": (0.75, 0.75, 0.75, 1.0),
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
    self.selected = max(0, min(len(self.keys) - 1, number - 1))


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


def keys_of(ramp):
    return [(k.position, k.value, k.interpolation) for k in ramp.keys]


def set_keys(node, ramp, keys):
    """Replace a ramp's keys (one curve write)."""
    global _quiet
    _quiet = True
    try:
        ramp.keys.clear()
        for p, v, interp in keys:
            k = ramp.keys.add()
            k.position, k.value, k.interpolation = p, v, interp
        ramp.selected = min(ramp.selected, len(ramp.keys) - 1)
    finally:
        _quiet = False
    write_curve(node, ramp)


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


def block_height(ramp):
    """A ramp's rows: header, graph (+ markers), and the point's 4 fields."""
    return ROW_H + graph_height(ramp) + (4 * ROW_H if ramp.show_point else 0)


def draw_layout(layout, node, ramp, field):
    """The ramp's header row, the space the graph is drawn into, and the
    selected point's fields. `field(layout, node, label, width)` is node._field."""
    row = layout.row(align=True)
    row.prop(ramp, "show_point", text="", emboss=False, icon="DISCLOSURE_TRI_DOWN" if ramp.show_point else "DISCLOSURE_TRI_RIGHT")
    row.label(text=ramp.name)
    tools = row.row(align=True)
    tools.alignment = "RIGHT"
    tools.template_icon_view(ramp, "preset", show_labels=True, scale=1.0, scale_popup=4.0)
    for action, icon in (("ADD", "ADD"), ("DELETE", "X"), ("REVERSE", "ARROW_LEFTRIGHT"), ("INVERT", "SORT_ASC")):
        op = tools.operator("bex.ramp_action", text="", icon=icon)
        op.ramp, op.action = ramp.name, action
    tools.prop(ramp, "expanded", text="", icon="FULLSCREEN_EXIT" if ramp.expanded else "FULLSCREEN_ENTER")
    space = layout.column()
    space.scale_y = graph_height(ramp) / ce.UNIT_Y
    space.label(text="")
    if ramp.show_point and ramp.keys:
        key = ramp.keys[min(ramp.selected, len(ramp.keys) - 1)]
        field(layout, node, "Point No.", 80).prop(ramp, "point", text="")
        field(layout, node, "Position").prop(key, "position", text="", slider=True)
        field(layout, node, "Value").prop(key, "value", text="", slider=True)
        field(layout, node, "Interpolation", 130).prop(key, "interpolation", text="")


def ramp_rects(node, region):
    """{name: (graph rect, marker rect)} in region pixels, each (x0, y0, x1, y1);
    and px. Found upwards from the code box: the Presets row and the separator,
    then the ramps from the last one up."""
    x0, _y0, x1, y1, px = ce.box_rect(node, region)
    y = y1 + (ROW_H + SEPARATOR_H) * px
    rects = {}
    for ramp in reversed(shown_ramps(node)):
        y += (4 * ROW_H if ramp.show_point else 0) * px
        markers = (x0, y, x1, y + MARKER_H * px)
        graph = (x0, markers[3], x1, y + graph_height(ramp) * px)
        rects[ramp.name] = (graph, markers)
        y += (graph_height(ramp) + ROW_H) * px
    return rects, px


def shown_ramps(node):
    return [node.bex_ramps[n] for n in node.ramps()]


# -- drawing -------------------------------------------------------------------
def _inner(graph, px):
    """The graph's 0-1 area: inset so the end keys' dots fit."""
    x0, y0, x1, y1 = graph
    inset = (KEY_R + 2) * px
    return x0 + inset, y0 + inset, x1 - inset, y1 - inset


def _to_px(inner, x, y):
    return inner[0] + x * (inner[2] - inner[0]), inner[1] + y * (inner[3] - inner[1])


def _from_px(inner, mx, my):
    w, h = max(1.0, inner[2] - inner[0]), max(1.0, inner[3] - inner[1])
    return (mx - inner[0]) / w, (my - inner[1]) / h


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


def draw_node(node, region, vx, vy):
    """Paint every ramp graph of a wrangle (called from code_editor's draw handler)."""
    if not node.bex_ramps:
        return
    rects, px = ramp_rects(node, region)
    for ramp in shown_ramps(node):
        graph, markers = rects[ramp.name]
        if graph[2] < 0 or graph[0] > region.width or markers[1] > region.height or graph[3] < 0:
            continue
        gpu.state.blend_set("ALPHA")
        ce._rect(*graph, PALETTE["back"])
        inner = _inner(graph, px)
        if not ce._clip(region, vx, vy, *graph):
            continue
        if ramp.expanded:
            for i in range(1, 10):
                gx = inner[0] + (inner[2] - inner[0]) * i / 10
                ce._rect(round(gx), graph[1], round(gx) + 1, graph[3], PALETTE["grid"])
            for i in range(1, 4):
                gy = inner[1] + (inner[3] - inner[1]) * i / 4
                ce._rect(graph[0], round(gy), graph[2], round(gy) + 1, PALETTE["grid"])
        keys = keys_of(ramp)
        pts = [_to_px(inner, x, y) for x, y in R.polyline(keys)]
        pts = [(graph[0], pts[0][1])] + pts + [(graph[2], pts[-1][1])]
        _tris([p for a, b in zip(pts, pts[1:]) for p in (a, b, (a[0], graph[1]), b, (b[0], graph[1]), (a[0], graph[1]))],
              PALETTE["fill"])
        _line(region, pts, PALETTE["line"], max(1.0, 1.2 * px))
        sel = min(ramp.selected, len(keys) - 1)
        for i, (x, y, _interp) in enumerate(keys):
            cx, cy = _to_px(inner, x, y)
            _dot(cx, cy, (KEY_R + 1) * px, PALETTE["key_edge"])
            _dot(cx, cy, KEY_R * px, PALETTE["selected" if i == sel else "key"])
        # Position markers under the graph.
        gpu.state.scissor_test_set(False)
        if ce._clip(region, vx, vy, markers[0], markers[1], markers[2], markers[3]):
            for i, (x, _y, _interp) in enumerate(keys):
                cx = _to_px(inner, x, 0)[0]
                w, top, bottom = MARKER_W * px, markers[3] - 1 * px, markers[1] + 1 * px
                mid = top - w
                color = PALETTE["selected" if i == sel else "marker"]
                _tris([(cx, top), (cx - w, mid), (cx + w, mid)], color)
                ce._rect(cx - w, bottom, cx + w, mid, color)
        gpu.state.scissor_test_set(False)


# -- presets thumbnails --------------------------------------------------------
THUMB = 64


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
            line = R.polyline(keys)
            heights = [R.evaluate(keys, (i + 0.5) / THUMB, line) for i in range(THUMB)]
            pixels = []
            for row in range(THUMB):
                level = (row + 0.5) / THUMB
                for col in range(THUMB):
                    pixels += (0.66, 0.66, 0.66, 1.0) if level <= heights[col] else (0.14, 0.14, 0.14, 1.0)
            pv.image_size = (THUMB, THUMB)
            pv.image_pixels_float = pixels
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
            graph, markers = rects[ramp.name]
            if graph[0] <= x <= graph[2] and markers[1] <= y <= graph[3]:
                return node, ramp, graph, markers, px
    return None


def _segment_interpolation(keys, x):
    """The interpolation of the stretch containing x (a new key there keeps the shape's style)."""
    before = [k for k in sorted(keys) if k[0] <= x]
    return before[-1][2] if before else (sorted(keys)[0][2] if keys else R.DEFAULT_INTERPOLATION)


class BEX_OT_ramp_edit(bpy.types.Operator):
    """Drag a ramp's points; click empty space to add one, Ctrl+click a point to delete it"""

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
        if hit is None:
            return {"PASS_THROUGH"}
        node, ramp, graph, markers, px = hit
        self.tree, self.node, self.ramp = node.id_data.name, node.name, ramp.name
        inner = _inner(graph, px)
        keys = keys_of(ramp)
        grab, best = None, HIT_R * px
        on_markers = y < markers[3]
        for i, (kx, ky, _interp) in enumerate(keys):
            cx, cy = _to_px(inner, kx, ky)
            d = abs(x - cx) if on_markers else ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= best:
                grab, best = i, d
        if grab is not None and event.ctrl:
            if len(ramp.keys) > 1:
                ramp.keys.remove(grab)
                ramp.selected = min(ramp.selected, len(ramp.keys) - 1)
                write_curve(node, ramp)
            return {"FINISHED"}
        if grab is None:
            if on_markers:
                return {"FINISHED"}
            nx, ny = _from_px(inner, x, y)
            nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
            k = ramp.keys.add()
            k.interpolation = _segment_interpolation(keys, nx)
            k.position, k.value = nx, ny
            grab = len(ramp.keys) - 1
        ramp.selected = grab
        self.index, self.only_x = grab, on_markers
        self.inner = inner
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
            nx, ny = _from_px(self.inner, event.mouse_region_x, event.mouse_region_y)
            key = ramp.keys[self.index]
            key.position = min(max(nx, 0.0), 1.0)
            if not self.only_x:
                key.value = min(max(ny, 0.0), 1.0)
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            return {"FINISHED"}
        if event.type in ("ESC", "RIGHTMOUSE"):
            return {"FINISHED"}
        return {"RUNNING_MODAL"}


ACTIONS = (("ADD", "Add", "Add a point between the selected one and the next"),
           ("DELETE", "Delete", "Delete the selected point"),
           ("REVERSE", "Reverse", "Flip the ramp left to right"),
           ("INVERT", "Invert", "Flip the ramp upside down"))


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
            ramp.selected = len(keys) - 1
        elif self.action == "DELETE":
            if len(keys) > 1:
                del keys[sel]
                set_keys(node, ramp, keys)
        else:
            new = R.reverse(keys) if self.action == "REVERSE" else R.invert(keys)
            selected_pos = keys[sel][0] if self.action == "INVERT" else 1.0 - keys[sel][0]
            set_keys(node, ramp, new)
            ramp.selected = min(range(len(new)), key=lambda i: abs(new[i][0] - selected_pos))
        return {"FINISHED"}


classes = (BexRampKey, BexRamp, BEX_OT_ramp_edit, BEX_OT_ramp_action)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _previews
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
