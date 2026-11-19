"""Turns a compiled BEX `Program` into real nodes inside a geometry node group."""

from . import library
from .lang.compiler import INPUT_SOCKETS

SOCKET_IDNAME = {"float": "NodeSocketFloat", "int": "NodeSocketInt", "vector": "NodeSocketVector", "bool": "NodeSocketBool"}
ATTR_TYPE = {"float": "FLOAT", "int": "INT", "vector": "FLOAT_VECTOR", "bool": "BOOLEAN"}
SOCKET_TYPE = {"float": "FLOAT", "int": "INT", "vector": "VECTOR", "bool": "BOOLEAN"}

INPUT_NODES = {
    "position": "GeometryNodeInputPosition",
    "normal": "GeometryNodeInputNormal",
    "index": "GeometryNodeInputIndex",
    "id": "GeometryNodeInputID",
    "time": "GeometryNodeInputSceneTime",
    "spline_parameter": "GeometryNodeSplineParameter",
    "curve_of_point": "GeometryNodeCurveOfPoint",
    "face_of_corner": "GeometryNodeFaceOfCorner",
    "vertex_of_corner": "GeometryNodeVertexOfCorner",
}
VMATH_FLOAT_OUT = {"DOT_PRODUCT", "LENGTH", "DISTANCE"}
RAMP_PREFIX = "BEX Ramp "  # + the chramp() name: the Float Curve node evaluating that ramp


def ramp_node(tree, name):
    """The Float Curve node that evaluates chramp(`name`), or None."""
    n = tree.nodes.get(RAMP_PREFIX + name) if tree else None
    return n if n is not None and n.bl_idname == "ShaderNodeFloatCurve" else None


def set_curve(node, points):
    """Make a Float Curve the straight pieces through `points` [(x, y)], x rising
    (a ramp sampled by ramp.polyline)."""
    mapping = node.mapping
    mapping.use_clip = False
    mapping.extend = "HORIZONTAL"
    curve = mapping.curves[0]
    while len(curve.points) > max(2, len(points)):
        curve.points.remove(curve.points[-1])
    while len(curve.points) < len(points):
        curve.points.new(1.0, 0.0)
    for p, (x, y) in zip(curve.points, points):
        p.location = (x, y)
        p.handle_type = "VECTOR"
    mapping.update()


def _enabled(sockets, name):
    return [s for s in sockets if s.enabled and s.name == name]


def _set_default(sock, const):
    if sock.type == "VECTOR":
        sock.default_value = const if isinstance(const, tuple) else (float(const),) * 3
    elif sock.type == "INT":
        sock.default_value = int(const)
    elif sock.type == "BOOLEAN":
        sock.default_value = bool(const)
    else:
        sock.default_value = float(const)


def sync_interface(tree, program, keep_stale=False):
    """Make the group's sockets match the script: the Geometry in/out, the three
    extra geometry inputs "Input 2"-"Input 4" (always there, like a wrangle's four
    inputs), and the script's ch*() inputs.

    Sockets whose name and type are unchanged are kept, so links and values on
    the wrangle node survive a recompile. With `keep_stale` (a line is still being
    typed) unused inputs are kept too, so they don't vanish mid-edit.
    Every value input shows only its connector: the node draws the values above the code.
    """
    iface = tree.interface
    geometry = "NodeSocketGeometry"

    def sockets():
        return [i for i in iface.items_tree if i.item_type == "SOCKET"]

    def find(in_out, name):
        return next((i for i in sockets() if i.in_out == in_out and i.name == name), None)

    def is_main(i):
        return i.name == "Geometry" and i.socket_type == geometry

    for in_out in ("OUTPUT", "INPUT"):
        if find(in_out, "Geometry") is None:
            iface.new_socket("Geometry", in_out=in_out, socket_type=geometry)

    extra = list(INPUT_SOCKETS)
    wanted = {name: geometry for name in extra}
    wanted.update({name: SOCKET_IDNAME[type] for name, type, _ in program.params})
    stale = [
        i for i in sockets()
        if not is_main(i) and not (i.in_out == "INPUT" and wanted.get(i.name) == i.socket_type)
        and not (keep_stale and i.in_out == "INPUT" and i.name not in wanted)
    ]
    for item in stale:
        iface.remove(item)

    for name in extra:
        if find("INPUT", name) is None:
            iface.new_socket(name, in_out="INPUT", socket_type=geometry)
    created = []
    for name, type, default in program.params:
        if find("INPUT", name) is None:
            item = iface.new_socket(name, in_out="INPUT", socket_type=SOCKET_IDNAME[type])
            item.default_value = default
            created.append((name, default))
    for item in sockets():
        if item.in_out == "INPUT" and item.socket_type != geometry:
            item.hide_value = True

    names = ["Geometry"] + extra + [name for name, _, _ in program.params]
    order = [find("OUTPUT", "Geometry")] + [find("INPUT", name) for name in names]
    for position, item in enumerate(order):
        if item.position != position:
            iface.move(item, position)
    return created


def build(tree, program, domain, keep_stale=False, ramps=None):
    """Rebuild `tree` from scratch for `program`. `ramps`: {chramp() name: polyline}.

    Returns (node count, [(name, default)] of inputs that were newly added).
    """
    created = sync_interface(tree, program, keep_stale)
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    group_out.is_active_output = True
    idents = {(i.in_out, i.name): i.identifier for i in tree.interface.items_tree if i.item_type == "SOCKET"}
    built = {}  # IRNode -> [output sockets]
    # Node name -> source line it belongs to. None is the geometry chain along the top.
    line_of = {group_in.name: None, group_out.name: None}
    line_stack = [None]  # source line of the expression being built

    def new(idname):
        n = nodes.new(idname)
        line_of[n.name] = line_stack[-1]
        return n

    def group_input(name):
        return next(s for s in group_in.outputs if s.identifier == idents[("INPUT", name)])

    def geometry(n):
        """Geometry input n: 0 is the node's Geometry, 1-3 are "Input 2"-"Input 4"."""
        return group_input("Geometry" if n == 0 else INPUT_SOCKETS[n - 1])

    captured = {}  # (IRNode, out) -> Capture Attribute output, see the geometry chain
    closures = {}  # shared ramp name -> its Closure Output

    def connect(val, sock):
        if val.is_const:
            _set_default(sock, val.const)
        elif (val.node, val.out) in captured:
            links.new(captured[(val.node, val.out)], sock)
        else:
            links.new(realize(val.node)[val.out], sock)

    def connect_field(val, sock):
        """Like connect(), for sockets that ignore their own value when unlinked
        (they fall back to an implicit field such as the position)."""
        if val.is_const:
            n = new("FunctionNodeInputVector")
            n.vector = val.const
            links.new(n.outputs[0], sock)
        else:
            connect(val, sock)

    def realize(ir):
        if ir not in built:
            line_stack.append(ir.line)
            built[ir] = create(ir)
            line_stack.pop()
        return built[ir]

    def create(ir):
        kind, p, ins = ir.kind, ir.params, ir.inputs
        if kind == "param":
            return [group_input(p["name"])]
        if kind == "input":
            return list(new(INPUT_NODES[p["name"]]).outputs)
        if kind == "attr":
            n = new("GeometryNodeInputNamedAttribute")
            n.data_type = ATTR_TYPE[p["dtype"]]
            n.inputs["Name"].default_value = p["name"]
            n.label = "@" + p["name"]
            return _enabled(n.outputs, "Attribute")
        if kind in ("math", "imath"):
            n = new("ShaderNodeMath" if kind == "math" else "FunctionNodeIntegerMath")
            n.operation = p["op"]
            for i, v in enumerate(ins):
                connect(v, n.inputs[i])
            return [n.outputs[0]]
        if kind == "vmath":
            n = new("ShaderNodeVectorMath")
            n.operation = p["op"]
            if p["op"] == "SCALE":
                connect(ins[0], n.inputs[0])
                connect(ins[1], n.inputs["Scale"])
            else:
                for i, v in enumerate(ins):
                    connect(v, n.inputs[i])
            return [n.outputs["Value" if p["op"] in VMATH_FLOAT_OUT else "Vector"]]
        if kind == "compare":
            n = new("FunctionNodeCompare")
            n.data_type = p["dtype"]
            n.operation = p["op"]
            connect(ins[0], _enabled(n.inputs, "A")[0])
            connect(ins[1], _enabled(n.inputs, "B")[0])
            return [n.outputs[0]]
        if kind == "bmath":
            n = new("FunctionNodeBooleanMath")
            n.operation = p["op"]
            for i, v in enumerate(ins):
                connect(v, n.inputs[i])
            return [n.outputs[0]]
        if kind == "switch":
            n = new("GeometryNodeSwitch")
            n.input_type = SOCKET_TYPE[p["dtype"]]
            for v, name in zip(ins, ("Switch", "False", "True")):
                connect(v, _enabled(n.inputs, name)[0])
            return _enabled(n.outputs, "Output")
        if kind == "combine":
            n = new("ShaderNodeCombineXYZ")
            for i, v in enumerate(ins):
                connect(v, n.inputs[i])
            return [n.outputs[0]]
        if kind == "separate":
            n = new("ShaderNodeSeparateXYZ")
            connect(ins[0], n.inputs[0])
            return list(n.outputs)
        if kind == "clamp":
            n = new("ShaderNodeClamp")
            for i, v in enumerate(ins):
                connect(v, n.inputs[i])
            return [n.outputs[0]]
        if kind == "mix":
            n = new("ShaderNodeMix")
            n.data_type = SOCKET_TYPE[p["dtype"]]
            n.clamp_factor = False
            if p["dtype"] == "vector":
                n.factor_mode = "UNIFORM"
            for v, name in zip(ins, ("Factor", "A", "B")):
                connect(v, _enabled(n.inputs, name)[0])
            return _enabled(n.outputs, "Result")
        if kind == "maprange":
            n = new("ShaderNodeMapRange")
            vec = p.get("dtype") == "vector"
            n.data_type = "FLOAT_VECTOR" if vec else "FLOAT"
            n.interpolation_type = p["interp"]
            n.clamp = p["clamp"]
            for v, sock in zip(ins, [s for s in n.inputs if s.enabled]):
                connect(v, sock)
            return _enabled(n.outputs, "Vector" if vec else "Result")
        if kind == "group":  # a library node group (library.py); geometry comes from the input
            d = library.GROUPS[p["name"]]
            n = new("GeometryNodeGroup")
            n.node_tree = library.ensure(p["name"])
            n.show_options = False
            vals = iter(ins)
            for spec, sock in zip(d.inputs, n.inputs):
                if spec.type == "NodeSocketGeometry":
                    links.new(geometry(p.get("input", 0)), sock)
                elif spec.default_input != "VALUE":
                    connect_field(next(vals), sock)
                else:
                    connect(next(vals), sock)
            return list(n.outputs)
        if kind == "sample":  # point(1, "P", i): the value of element i on an input
            n = new("GeometryNodeSampleIndex")
            n.data_type = ATTR_TYPE[p["dtype"]]
            n.domain = p["domain"]
            links.new(geometry(p["input"]), n.inputs["Geometry"])
            connect(ins[0], _enabled(n.inputs, "Value")[0])
            connect(ins[1], n.inputs["Index"])
            return _enabled(n.outputs, "Value")
        if kind == "nearest":  # nearpoint(1, pos)
            n = new("GeometryNodeSampleNearest")
            n.domain = p["domain"]
            links.new(geometry(p["input"]), n.inputs["Geometry"])
            connect_field(ins[0], n.inputs["Sample Position"])
            return [n.outputs["Index"]]
        if kind == "count":  # npoints(1): sums 1 over the domain, across mesh, points and curves
            n = new("GeometryNodeAttributeStatistic")
            n.data_type = "FLOAT"
            n.domain = p["domain"]
            links.new(geometry(p["input"]), n.inputs["Geometry"])
            n.inputs["Attribute"].default_value = 1.0
            return [n.outputs["Sum"]]
        if kind == "ramp":
            name = p["name"]
            curve = ramp_node(tree, name)
            if curve is None:
                curve = nodes.new("ShaderNodeFloatCurve")
                curve.name = curve.label = RAMP_PREFIX + name
                set_curve(curve, (ramps or {}).get(name, [(0.0, 0.0), (1.0, 1.0)]))
            if name not in program.shared_ramps:
                line_of[curve.name] = line_stack[-1]
                connect(ins[0], curve.inputs["Value"])
                return [curve.outputs[0]]
            # Read at several positions: the one curve sits in a closure zone, and
            # each position is an Evaluate Closure of it, so the shape stays in one node.
            closure = closures.get(name)
            if closure is None:
                line_of[curve.name] = line_stack[-1]
                zone_in, closure = new("NodeClosureInput"), new("NodeClosureOutput")
                zone_in.pair_with_output(closure)
                closure.input_items.new("FLOAT", "Value")
                closure.output_items.new("FLOAT", "Result")
                links.new(zone_in.outputs[0], curve.inputs["Value"])
                links.new(curve.outputs[0], closure.inputs[0])
                closures[name] = closure
            ev = new("NodeEvaluateClosure")
            ev.input_items.new("FLOAT", "Value")
            ev.output_items.new("FLOAT", "Result")
            links.new(closure.outputs[0], ev.inputs[0])
            connect(ins[0], ev.inputs[1])
            return [ev.outputs[0]]
        if kind == "noise":
            n = new("ShaderNodeTexNoise")
            n.noise_dimensions = "3D"
            n.inputs["Scale"].default_value = 1.0
            n.inputs["Detail"].default_value = 0.0
            connect_field(ins[0], n.inputs["Vector"])
            return [n.outputs[0], n.outputs[1]]
        if kind == "white_noise":
            n = new("ShaderNodeTexWhiteNoise")
            n.noise_dimensions = p["dims"]
            if p["dims"] == "3D":
                connect_field(ins[0], n.inputs["Vector"])
            else:
                connect(ins[0], n.inputs["W"])
            return [n.outputs[0], n.outputs[1]]
        raise RuntimeError(f"BEX builder: unknown IR node '{kind}'")

    # -- geometry chain: capture the values that need it (Program.captures) on the
    # *input* geometry, then one write node per Program.stores entry.
    geo = group_input("Geometry")
    if program.captures:
        cap = new("GeometryNodeCaptureAttribute")
        cap.domain = domain
        links.new(geo, cap.inputs["Geometry"])
        for i, (label, type, val) in enumerate(program.captures):
            cap.capture_items.new(SOCKET_TYPE[type], label)
            connect(val, cap.inputs[2 + i])
        # From here on these values come from the Capture node.
        for i, (_, _, val) in enumerate(program.captures):
            captured[(val.node, val.out)] = cap.outputs[2 + i]
        geo = cap.outputs["Geometry"]

    for st in program.stores:
        if st.name == "P":
            n = new("GeometryNodeSetPosition")
            if st.offset:
                connect(st.value, n.inputs["Offset"])
            else:
                connect_field(st.value, n.inputs["Position"])
        else:
            n = new("GeometryNodeStoreNamedAttribute")
            n.data_type = ATTR_TYPE[st.type]
            n.domain = domain
            n.inputs["Name"].default_value = st.name
            n.label = "@" + st.name
            connect(st.value, _enabled(n.inputs, "Value")[0])
        links.new(geo, n.inputs["Geometry"])
        if st.selection is not None:
            connect(st.selection, n.inputs["Selection"])
        geo = n.outputs["Geometry"]

    links.new(geo, next(s for s in group_out.inputs if s.identifier == idents[("OUTPUT", "Geometry")]))
    _split_group_inputs(tree, group_in, line_of)
    _layout(tree, group_in, line_of, program.source, program.group_source)
    return len(nodes), created


def _split_group_inputs(tree, group_in, line_of):
    """Give every source line that reads node inputs its own Group Input node,
    and hide the sockets each Group Input does not use."""
    per_band = {}
    for link in tree.links:
        band = line_of.get(link.to_node.name)
        if link.from_node.name == group_in.name and band is not None:
            per_band.setdefault(band, []).append(link)
    for band, group in per_band.items():
        gi = tree.nodes.new("NodeGroupInput")
        line_of[gi.name] = band
        moves = [(l.from_socket.identifier, l.to_socket) for l in group]
        for l in group:
            tree.links.remove(l)
        for ident, target in moves:
            tree.links.new(next(s for s in gi.outputs if s.identifier == ident), target)
    used = {(l.from_node.name, l.from_socket.identifier) for l in tree.links}
    for n in tree.nodes:
        if n.bl_idname == "NodeGroupInput":
            for s in n.outputs:
                s.hide = (n.name, s.identifier) not in used


# -- layout ---------------------------------------------------------------
#
# Columns run right to left by distance from the Group Output, so links always
# flow left to right. Rows are bands: the geometry chain on top, then one framed
# band per source line, labelled with that line of code. A node only ever sits
# in its own (band, column) cell and cells stack downwards, so nothing overlaps.
#
# Each band gets its own Group Input node showing only the inputs that line uses,
# so input wires stay short and inside their frame instead of fanning out from
# one node in the corner.

GAP_X = 60.0  # between columns
GAP_Y = 20.0  # between nodes stacked in one column
BAND_GAP = 30.0  # between bands
FRAME_PAD_TOP = 50.0  # room for the frame label
FRAME_PAD_BOTTOM = 30.0
SOCKET_ROW = 22.0
BUTTON_ROW = 25.0

# Rows of buttons each node draws above its sockets.
BUTTON_ROWS = {
    "ShaderNodeFloatCurve": 9,  # the curve widget
    "ShaderNodeMath": 2,
    "ShaderNodeVectorMath": 1,
    "FunctionNodeIntegerMath": 1,
    "FunctionNodeBooleanMath": 1,
    "FunctionNodeCompare": 3,
    "GeometryNodeSwitch": 1,
    "ShaderNodeMix": 3,
    "ShaderNodeMapRange": 3,
    "GeometryNodeGroup": 0,  # drawn with show_options off
    "ShaderNodeClamp": 1,
    "ShaderNodeTexNoise": 3,
    "ShaderNodeTexWhiteNoise": 1,
    "GeometryNodeInputNamedAttribute": 1,
    "GeometryNodeStoreNamedAttribute": 2,
    "GeometryNodeCaptureAttribute": 1,
    "FunctionNodeInputVector": 3,
    "GeometryNodeSampleIndex": 3,
    "GeometryNodeSampleNearest": 1,
    "GeometryNodeAttributeStatistic": 2,
}


def estimate_height(node):
    """Node height in node-space units. Real sizes only exist after a redraw,
    so this errs on the tall side."""
    h = 35.0 + BUTTON_ROW * BUTTON_ROWS.get(node.bl_idname, 0)
    for s in node.outputs:
        if s.enabled and not s.hide:
            h += SOCKET_ROW
    for s in node.inputs:
        if s.enabled and not s.hide:
            expanded = s.type == "VECTOR" and not s.is_linked and not s.hide_value
            h += SOCKET_ROW * (4 if expanded else 1)
    return h + 10.0


def _layout(tree, group_in, line_of, source, group_source=""):
    nodes = list(tree.nodes)
    feeds = {}
    for link in tree.links:
        feeds.setdefault(link.from_node.name, set()).add(link.to_node.name)
    depth = {}

    def get_depth(name):
        if name not in depth:
            depth[name] = 0
            depth[name] = 1 + max((get_depth(n) for n in feeds.get(name, ())), default=-1)
        return depth[name]

    deepest = max((get_depth(n.name) for n in nodes if n != group_in), default=0)
    depth[group_in.name] = deepest + 1

    # Column x positions (a node's location is its top-left corner).
    width = {}
    for n in nodes:
        d = get_depth(n.name)
        width[d] = max(width.get(d, 0.0), n.width)
    column_x, x = {}, 0.0
    for d in sorted(width):
        x -= width[d]
        column_x[d] = x
        x -= GAP_X

    bands = {}
    for n in nodes:
        bands.setdefault(line_of.get(n.name), []).append(n)

    y = 0.0
    for line in sorted(bands, key=lambda b: (b is not None, b or 0)):
        members = bands[line]
        framed = line is not None
        if framed:
            frame = tree.nodes.new("NodeFrame")
            if line == 0:  # nodes of the Group expression
                text = "Group: " + group_source
            else:
                text = source[line - 1].strip() if 0 < line <= len(source) else ""
            frame.label = text if len(text) <= 64 else text[:61] + "..."
            frame.shrink = True
            y -= FRAME_PAD_TOP
        used = {}  # column -> height already filled in this band
        for n in members:
            d = depth[n.name]
            if framed:
                n.parent = frame
            n.location_absolute = (column_x[d], y - used.get(d, 0.0))
            used[d] = used.get(d, 0.0) + estimate_height(n) + GAP_Y
        y -= max(used.values()) - GAP_Y
        y -= BAND_GAP + (FRAME_PAD_BOTTOM if framed else 0.0)

