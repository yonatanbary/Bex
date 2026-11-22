"""Turns a compiled BEX `Program` into real nodes inside a geometry node group."""

import mathutils

from . import library
from .lang.compiler import CHUNK_FIRST, CHUNK_GROWTH, INPUT_SOCKETS, TEMP_PREFIX, Val

SOCKET_IDNAME = {"float": "NodeSocketFloat", "int": "NodeSocketInt", "vector": "NodeSocketVector", "bool": "NodeSocketBool",
                 "quaternion": "NodeSocketRotation", "matrix": "NodeSocketMatrix"}
ATTR_TYPE = {"float": "FLOAT", "int": "INT", "vector": "FLOAT_VECTOR", "bool": "BOOLEAN", "quaternion": "QUATERNION",
             "matrix": "FLOAT4X4"}
SOCKET_TYPE = {"geometry": "GEOMETRY", "float": "FLOAT", "int": "INT", "vector": "VECTOR", "bool": "BOOLEAN", "quaternion": "ROTATION",
               "matrix": "MATRIX"}

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
    "tangent": "GeometryNodeInputTangent",
    "face_area": "GeometryNodeInputMeshFaceArea",
    "mesh_island": "GeometryNodeInputMeshIsland",
    "edge_angle": "GeometryNodeInputMeshEdgeAngle",
    "spline_cyclic": "GeometryNodeInputSplineCyclic",
    "instance_rotation": "GeometryNodeInputInstanceRotation",
    "instance_scale": "GeometryNodeInputInstanceScale",
    "instance_transform": "GeometryNodeInstanceTransform",
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
    if sock.type == "ROTATION":  # a quaternion (x, y, z, w); the socket holds Euler angles
        x, y, z, w = const
        sock.default_value = mathutils.Quaternion((w, x, y, z)).to_euler("XYZ")
    elif sock.type in ("VECTOR", "RGBA"):  # also a colour or 2D vector attribute's value (alpha 1)
        size = len(sock.default_value)
        const = tuple(const) if isinstance(const, tuple) else (float(const),) * 3
        sock.default_value = (const + (1.0,))[:size] if size > 3 else const[:size]
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
    limits = getattr(program, "limits", {})
    for name, type, _ in program.params:  # chf("amp", 1, min=0, max=10): the slider's range
        item = find("INPUT", name)
        if item is not None and hasattr(item, "min_value"):
            lo, hi = limits.get(name, (None, None))
            big = 2147483647 if type == "int" else 3.4028234663852886e38
            item.min_value = lo if lo is not None else -big
            item.max_value = hi if hi is not None else big
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

    base = []  # the node's Geometry after the chunked loops built so far (they add hidden attributes)

    def geometry(n):
        """Geometry input n: 0 is the node's Geometry, 1-3 are "Input 2"-"Input 4".
        Input 0 comes after the chunked loops, so a measurement can read their results."""
        if n == 0:
            return base[-1] if base else group_input("Geometry")
        return group_input(INPUT_SOCKETS[n - 1])

    captured = {}  # (IRNode, out) -> Capture Attribute output, see the geometry chain
    record_base = []  # the geometry after the Capture: GeoOp records read their values there
    closures = {}  # shared ramp name -> its Closure Output
    zones = {}  # repeat_in IRNode -> its Repeat Output node

    def connect(val, sock):
        if val.is_const and val.type == "geometry":  # empty: an unlinked geometry socket
            return
        if val.is_const and val.type == "matrix":  # a matrix socket has no value of its own
            n = new("FunctionNodeCombineMatrix")
            for s, c in zip(n.inputs, val.const):
                s.default_value = c
            links.new(n.outputs[0], sock)
        elif val.is_const:
            _set_default(sock, val.const)
        elif (val.node, val.out) in captured:
            links.new(captured[(val.node, val.out)], sock)
        else:
            links.new(realize(val.node)[val.out], sock)

    def connect_field(val, sock):
        """Like connect(), for sockets that ignore their own value when unlinked
        (they fall back to an implicit field such as the position)."""
        if val.is_const:
            if val.type == "vector":
                n = new("FunctionNodeInputVector")
                n.vector = val.const
            elif val.type == "int":
                n = new("FunctionNodeInputInt")
                n.integer = val.const
            elif val.type == "bool":
                n = new("FunctionNodeInputBool")
                n.boolean = val.const
            else:
                n = new("ShaderNodeValue")
                n.outputs[0].default_value = val.const
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
            if p["op"] in ("SCALE", "REFRACT"):  # the last value goes into Scale (REFRACT: the IOR)
                for i, v in enumerate(ins[:-1]):
                    connect(v, n.inputs[i])
                connect(ins[-1], n.inputs["Scale"])
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
        if kind == "node":  # one Blender node, described by its params (see Compiler.gnode)
            n = new(p["idname"])
            for prop, value in p.get("props", ()):
                setattr(n, prop, value)
            for name, value in p.get("menus", ()):
                n.inputs[name].default_value = value
            if "geo" in p:
                links.new(geometry(p["geo"][1]), n.inputs[p["geo"][0]])
            implicit = p.get("implicit", ())
            for name, v in zip(p["ins"], ins):  # a socket name, or its number when names repeat
                sock = n.inputs[name] if isinstance(name, int) else _enabled(n.inputs, name)[0]
                (connect_field if name in implicit else connect)(v, sock)
            # an output this node's settings hide is listed too, so indexes stay put
            return [(_enabled(n.outputs, name) or [o for o in n.outputs if o.name == name] or [None])[0]
                    for name in p["outs"]]
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
        if kind == "count" and p.get("primary"):  # the first new point number (see split_points)
            return [primary_count(geometry(p["input"]))]
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
        if kind == "white_noise":
            n = new("ShaderNodeTexWhiteNoise")
            n.noise_dimensions = p["dims"]
            if p["dims"] == "3D":
                connect_field(ins[0], n.inputs["Vector"])
            else:
                connect(ins[0], n.inputs["W"])
            return [n.outputs[0], n.outputs[1]]
        if kind == "stat":  # the largest value of a field over the incoming geometry (a per-element loop count)
            n = new("GeometryNodeAttributeStatistic")
            n.data_type = "FLOAT"
            n.domain = p["domain"]
            links.new(geometry(0), n.inputs["Geometry"])
            connect(ins[0], n.inputs["Attribute"])
            connect(ins[1], n.inputs["Selection"])
            return [n.outputs["Max"]]
        if kind == "repeat_in":  # a for-loop: both zone nodes; values changed in it are its items
            zone_in, zone_out = new("GeometryNodeRepeatInput"), new("GeometryNodeRepeatOutput")
            zone_in.pair_with_output(zone_out)
            zone_out.repeat_items.clear()
            for k, t in enumerate(p["items"]):
                zone_out.repeat_items.new(SOCKET_TYPE[t], f"Item {k + 1}")
            for v, sock in zip(ins, zone_in.inputs):  # Iterations, then the starting values
                connect(v, sock)
            zones[ir] = zone_out
            return list(zone_in.outputs)  # Iteration, then the items
        if kind == "closure_out":  # an own function called from several places, built once
            zone_in, zone_out = new("NodeClosureInput"), new("NodeClosureOutput")
            zone_in.pair_with_output(zone_out)
            for k, t in enumerate(p["items"]):
                zone_out.input_items.new(SOCKET_TYPE[t], f"Arg {k + 1}")
            zone_out.output_items.new(SOCKET_TYPE[p["ret"]], "Result")
            zone_out.label = zone_in.label = p["name"] + "()"
            built[p["input"]] = list(zone_in.outputs)
            connect(ins[0], zone_out.inputs[0])
            return [zone_out.outputs[0]]
        if kind == "closure_in":
            raise RuntimeError("BEX builder: a closure's input is built by its closure_out")
        if kind == "closure_call":
            closure = p["closure"]
            ev = new("NodeEvaluateClosure")
            for k, t in enumerate(closure.params["items"]):
                ev.input_items.new(SOCKET_TYPE[t], f"Arg {k + 1}")
            ev.output_items.new(SOCKET_TYPE[closure.params["ret"]], "Result")
            ev.label = closure.params["name"] + "()"
            links.new(realize(closure)[0], ev.inputs[0])
            for k, v in enumerate(ins):
                connect(v, ev.inputs[k + 1])
            return [ev.outputs[0]]
        if kind == "records":  # one Repeat Zone pass of a GeoOp
            return [make_records(ins[0], dict(zip(p["keys"], ins[1:])), p["domain"])]
        if kind == "geojoin":
            n = new("GeometryNodeJoinGeometry")
            for v in reversed(ins):
                if not v.is_const:
                    connect(v, n.inputs[0])
            return [n.outputs[0]]
        if kind == "chunk":
            raise RuntimeError("BEX builder: a chunked loop is built by its chunk_end (Program.preludes)")
        if kind == "repeat_out":
            realize(p["zone"])
            zone_out = zones[p["zone"]]
            for v, sock in zip(ins, zone_out.inputs):
                connect(v, sock)
            return list(zone_out.outputs)
        raise RuntimeError(f"BEX builder: unknown IR node '{kind}'")

    def store(geo, name, type, val, dom, selection=None):
        n = new("GeometryNodeStoreNamedAttribute")
        n.data_type = ATTR_TYPE[type]
        n.domain = dom
        n.inputs["Name"].default_value = name
        links.new(geo, n.inputs["Geometry"])
        if selection is not None:
            links.new(selection, n.inputs["Selection"])
        value = _enabled(n.inputs, "Value")[0]
        if isinstance(val, Val):
            connect(val, value)
        else:  # a socket
            links.new(val, value)
        return n.outputs["Geometry"]

    def read(name, type):
        n = new("GeometryNodeInputNamedAttribute")
        n.data_type = ATTR_TYPE[type]
        n.inputs["Name"].default_value = name
        return _enabled(n.outputs, "Attribute")[0]

    def chunked_loop(end, geo):
        """A loop that can end early (compiler._zone, `chunked`): an outer zone of
        growing chunks. Each chunk runs the inner zone, then stores the items on the
        geometry (all under shadow names first, so values that read each other stay
        right, then copied back); once no element is running it is skipped."""
        ch = end.params["chunk"]
        p = ch.params
        line_stack.append(ch.line)
        for name, type, val in zip(p["names"], p["items"], ch.inputs):  # starting values
            geo = store(geo, name, type, val, p["domain"])
        zone_in, zone_out = new("GeometryNodeRepeatInput"), new("GeometryNodeRepeatOutput")
        zone_in.pair_with_output(zone_out)
        zone_out.repeat_items.clear()
        zone_out.repeat_items.new("GEOMETRY", "Geometry")
        zone_out.repeat_items.new("INT", "Offset")
        zone_in.inputs["Iterations"].default_value = p["count"]
        links.new(geo, zone_in.inputs["Geometry"])
        # this chunk's size: floor(CHUNK_FIRST * CHUNK_GROWTH ^ chunk)
        grow = new("ShaderNodeMath")
        grow.operation = "POWER"
        grow.inputs[0].default_value = CHUNK_GROWTH
        links.new(zone_in.outputs["Iteration"], grow.inputs[1])
        size = new("ShaderNodeMath")
        size.operation = "MULTIPLY"
        links.new(grow.outputs[0], size.inputs[0])
        size.inputs[1].default_value = CHUNK_FIRST
        floor = new("ShaderNodeMath")
        floor.operation = "FLOOR"
        links.new(size.outputs[0], floor.inputs[0])
        built[ch] = [floor.outputs[0], zone_in.outputs["Offset"]]
        inside = zone_in.outputs["Geometry"]
        # Only elements still running (the loop's break flag unset) are worked on and stored.
        flag = p["names"][end.params["flag"]]
        running = new("FunctionNodeBooleanMath")
        running.operation = "NOT"
        links.new(read(flag, "bool"), running.inputs[0])
        stepped = inside
        for name, type, val in zip(p["names"], p["items"], end.inputs):
            stepped = store(stepped, name + "~", type, val, p["domain"], running.outputs[0])
        order = sorted(range(len(p["names"])), key=lambda k: p["names"][k] == flag)  # the flag last
        for k in order:
            name, type = p["names"][k], p["items"][k]
            stepped = store(stepped, name, type, read(name + "~", type), p["domain"], running.outputs[0])
        # still running anywhere (in the Group)?
        stat = new("GeometryNodeAttributeStatistic")
        stat.data_type = "FLOAT"
        stat.domain = p["domain"]
        links.new(inside, stat.inputs["Geometry"])
        links.new(running.outputs[0], stat.inputs["Attribute"])
        connect(end.inputs[-1], stat.inputs["Selection"])
        any_running = new("FunctionNodeCompare")
        any_running.data_type = "FLOAT"
        any_running.operation = "GREATER_THAN"
        links.new(stat.outputs["Max"], _enabled(any_running.inputs, "A")[0])
        _enabled(any_running.inputs, "B")[0].default_value = 0.5
        skip = new("GeometryNodeSwitch")
        skip.input_type = "GEOMETRY"
        links.new(any_running.outputs[0], _enabled(skip.inputs, "Switch")[0])
        links.new(inside, _enabled(skip.inputs, "False")[0])
        links.new(stepped, _enabled(skip.inputs, "True")[0])
        links.new(_enabled(skip.outputs, "Output")[0], zone_out.inputs["Geometry"])
        offset = new("FunctionNodeIntegerMath")
        offset.operation = "ADD"
        links.new(zone_in.outputs["Offset"], offset.inputs[0])
        links.new(floor.outputs[0], offset.inputs[1])
        links.new(offset.outputs[0], zone_out.inputs["Offset"])
        line_stack.pop()
        return zone_out.outputs["Geometry"]

    # -- addpoint(), addprim(), set*attrib(), remove*() ------------------------
    # Each call site (GeoOp) becomes record points: one per element where it ran,
    # holding its values (read on the incoming geometry). Then, on the written
    # geometry: new points are joined after the existing ones, primitives are built
    # from their corners (copies of the points they use), writes to other elements
    # are gathered per element, and removals run last.
    G = TEMP_PREFIX + "g_"  # the records' hidden attributes

    def out(n, name):
        return _enabled(n.outputs, name)[0]

    def g_read(name, type):
        return read(G + name, type)

    def g_store(geo, name, type, value, dom="POINT"):
        return store(geo, G + name, type, value, dom)

    def index():
        return new("GeometryNodeInputIndex").outputs[0]

    def size(geo, dom):
        n = new("GeometryNodeAttributeStatistic")
        n.data_type, n.domain = "FLOAT", dom
        links.new(geo, n.inputs["Geometry"])
        n.inputs["Attribute"].default_value = 1.0
        return n.outputs["Sum"]

    def points(count):
        n = new("GeometryNodePoints")
        links.new(count, n.inputs["Count"])
        return n.outputs[0]

    def int_math(op, a, b):
        n = new("FunctionNodeIntegerMath")
        n.operation = op
        for sock, v in zip(n.inputs, (a, b)):
            if isinstance(v, int):
                sock.default_value = v
            else:
                links.new(v, sock)
        return n.outputs[0]

    def int_compare(a, op, b):
        n = new("FunctionNodeCompare")
        n.data_type, n.operation = "INT", op
        sa, sb = _enabled(n.inputs, "A")[0], _enabled(n.inputs, "B")[0]
        links.new(a, sa)
        if isinstance(b, int):
            sb.default_value = b
        else:
            links.new(b, sb)
        return n.outputs[0]

    def delete(geo, dom, selection, mode="ALL"):
        n = new("GeometryNodeDeleteGeometry")
        n.domain, n.mode = dom, mode
        links.new(geo, n.inputs["Geometry"])
        links.new(selection, n.inputs["Selection"])
        return n.outputs[0]

    def join(geos):
        n = new("GeometryNodeJoinGeometry")
        for g in reversed(geos):  # a multi-input socket lists its newest link first
            links.new(g, n.inputs[0])
        return n.outputs[0]

    def sample(geo, type, value, idx, dom="POINT"):
        n = new("GeometryNodeSampleIndex")
        n.data_type, n.domain = ATTR_TYPE[type], dom
        links.new(geo, n.inputs["Geometry"])
        (connect if isinstance(value, Val) else links.new)(value, _enabled(n.inputs, "Value")[0])
        links.new(idx, n.inputs["Index"])
        return out(n, "Value")

    def field_node(idname, type, value, group, dom="POINT", **props):
        n = new(idname)
        n.domain = dom
        n.data_type = ATTR_TYPE[type]
        for k, v in props.items():
            setattr(n, k, v)
        links.new(value, _enabled(n.inputs, "Value")[0])
        if group is not None:
            links.new(group, n.inputs["Group ID"])
        return n

    def sort(geo, group=None, weight=None):
        n = new("GeometryNodeSortElements")
        n.domain = "POINT"
        links.new(geo, n.inputs["Geometry"])
        if group is not None:
            links.new(group, n.inputs["Group ID"])
        if weight is not None:
            links.new(weight, n.inputs["Sort Weight"])
        return n.outputs[0]

    def to_points(geo):
        """Every point of `geo` as a point cloud (mesh vertices, point cloud points, curve points)."""
        sep = new("GeometryNodeSeparateComponents")
        links.new(geo, sep.inputs[0])
        m2p = new("GeometryNodeMeshToPoints")
        links.new(sep.outputs["Mesh"], m2p.inputs["Mesh"])
        c2p = new("GeometryNodeCurveToPoints")  # copies of curve points are one-point curves
        c2p.mode = "EVALUATED"
        links.new(sep.outputs["Curve"], c2p.inputs["Curve"])
        return join([m2p.outputs[0], sep.outputs["Point Cloud"], c2p.outputs["Points"]])

    def component_points(geo, comp):
        n = new("GeometryNodeAttributeDomainSize")
        n.component = comp
        links.new(geo, n.inputs[0])
        return n.outputs["Point Count"]

    def primary_count(geo):
        """How many points Houdini's point numbers count: mesh and point cloud points, else curve points."""
        main = int_math("ADD", component_points(geo, "MESH"), component_points(geo, "POINTCLOUD"))
        sw = new("GeometryNodeSwitch")
        sw.input_type = "INT"
        links.new(int_compare(main, "GREATER_THAN", 0), _enabled(sw.inputs, "Switch")[0])
        links.new(component_points(geo, "CURVE"), _enabled(sw.inputs, "False")[0])
        links.new(main, _enabled(sw.inputs, "True")[0])
        return _enabled(sw.outputs, "Output")[0]

    def split_points(geo):
        """(points, rest): Houdini's point numbers mean the mesh and point cloud
        points; curve points only when there are none of those (each component
        numbers its points on its own)."""
        sep = new("GeometryNodeSeparateComponents")
        links.new(geo, sep.inputs[0])
        counts = []
        for comp in ("MESH", "POINTCLOUD"):
            n = new("GeometryNodeAttributeDomainSize")
            n.component = comp
            links.new(geo, n.inputs[0])
            counts.append(n.outputs["Point Count"])
        has = int_compare(int_math("ADD", *counts), "GREATER_THAN", 0)
        picked = []
        for when_true, when_false in ((join([sep.outputs["Mesh"], sep.outputs["Point Cloud"]]), geo),
                                      (join([sep.outputs["Curve"], sep.outputs["Instances"], sep.outputs["Volume"],
                                             sep.outputs["Grease Pencil"]]), None)):
            sw = new("GeometryNodeSwitch")
            sw.input_type = "GEOMETRY"
            links.new(has, _enabled(sw.inputs, "Switch")[0])
            links.new(when_true, _enabled(sw.inputs, "True")[0])
            if when_false is not None:
                links.new(when_false, _enabled(sw.inputs, "False")[0])
            picked.append(_enabled(sw.outputs, "Output")[0])
        return picked

    def on_points(geo, dom, fn):
        """fn(geometry) on the points Houdini's point numbers mean (split_points), or on all of `dom`."""
        if dom != "POINT":
            return fn(geo)
        main, rest = split_points(geo)
        return join([fn(main), rest])

    def make_records(mask, values, dom):
        """One point per element of `dom` where `mask` is true, holding `values`.
        Read on the geometry after the Capture (if any), so captured values are right."""
        source = record_base[-1] if record_base else geometry(0)
        if dom == "POINT":  # the points Houdini's numbers mean, not the curve points beside a mesh
            source = split_points(source)[0]
        geo = points(size(source, dom))
        for key, val in list(values.items()) + ([("mask", mask)] if not mask.is_const else []):
            v = val if val.is_const else sample(source, val.type, val, index(), dom)
            geo = g_store(geo, key, val.type, v)
        if not mask.is_const:
            no = new("FunctionNodeBooleanMath")
            no.operation = "NOT"
            links.new(g_read("mask", "bool"), no.inputs[0])
            geo = delete(geo, "POINT", no.outputs[0])
        return geo

    def records(op):
        """The call's records: one point per element where it ran (every pass, from a Repeat Zone)."""
        if op.records is not None:
            return realize(op.records.node)[op.records.out]
        line_stack.append(op.line)
        geo = make_records(op.mask, op.values, domain)
        line_stack.pop()
        return geo

    def gather(geo, recs, key, dom):
        """Lines the records up with the elements of `dom` they name in `key`: a
        point cloud with one point per element, in element order, holding `c` (how
        many records name it) and `m` (where its records start in the sorted cloud,
        which is returned too)."""
        marks = g_store(g_store(points(size(geo, dom)), "t", "int", index()), "k", "int", Val("int", 0))
        recs = g_store(g_store(recs, "t", "int", g_read(key, "int")), "k", "int", Val("int", 1))
        recs = g_store(recs, "o", "int", index())
        # Grouped by element, a group's marker first (sorting is stable), then its records in order.
        cloud = sort(join([marks, recs]), group=g_read("t", "int"))
        acc = field_node("GeometryNodeAccumulateField", "int", g_read("k", "int"), g_read("t", "int"))
        cloud = g_store(g_store(cloud, "c", "int", out(acc, "Total")), "m", "int", index())
        return cloud, delete(cloud, "POINT", int_compare(g_read("k", "int"), "EQUAL", 1))

    def pick(geo, recs, key, carry):
        """Copies of geo's points, one per record, in record order: record r copies
        point `key` with all its attributes (`sp` = that point's number). `carry`:
        record attributes copied onto the copies."""
        cloud, marks = gather(geo, recs, key, "POINT")
        src = g_store(geo, "sp", "int", index())
        dup = new("GeometryNodeDuplicateElements")
        dup.domain = "POINT"
        links.new(src, dup.inputs["Geometry"])
        links.new(sample(marks, "int", g_read("c", "int"), index()), dup.inputs["Amount"])
        copies = g_store(dup.outputs["Geometry"], "di", "int", dup.outputs["Duplicate Index"])
        # copy k of point p is the k-th record naming p in the sorted cloud
        start = sample(marks, "int", g_read("m", "int"), g_read("sp", "int"))
        rec = int_math("ADD", int_math("ADD", start, 1), g_read("di", "int"))
        for name, type in list(carry) + [("o", "int")]:
            copies = g_store(copies, name, type, sample(cloud, type, g_read(name, type), rec))
        return sort(copies, weight=g_read("o", "int"))

    def gathered(geo, recs, dom, mode, type):
        """Per element of `dom`: (any record names it, the records' value combined by mode)."""
        big = {"float": 3.4e38, "int": 2147483647}
        if mode in ("min", "max"):
            b = big.get(type, 3.4e38) * (1 if mode == "min" else -1)
            init = Val(type, (b,) * 3 if type == "vector" else (int(b) if type == "int" else b))
        else:
            init = Val(type, {"vector": (0.0, 0.0, 0.0), "int": 0, "float": 0.0, "bool": False}.get(type, 0))
        marks_v = None
        if type is not None:
            recs = g_store(recs, "v", type, g_read("value", type))
        cloud, marks = gather(geo, recs, "target", dom)
        if type is not None:
            # the markers' own value can't change a sum, minimum or maximum
            cloud = store(cloud, G + "v", type, init, "POINT",
                          int_compare(g_read("k", "int"), "EQUAL", 0)) if init.const not in (0, 0.0, False) else cloud
            group = g_read("t", "int")
            if mode == "set":  # the last record wins: it sits c places after its marker
                n = field_node("GeometryNodeFieldAtIndex", type, g_read("v", type), None)
                links.new(int_math("ADD", index(), g_read("c", "int")), n.inputs["Index"])
                agg = n.outputs[0]
            elif mode == "add":
                agg = out(field_node("GeometryNodeAccumulateField", type, g_read("v", type), group), "Total")
            else:
                agg = out(field_node("GeometryNodeFieldMinAndMax", type, g_read("v", type), group),
                          "Min" if mode == "min" else "Max")
            cloud = g_store(cloud, "a", type, agg)
            marks = delete(cloud, "POINT", int_compare(g_read("k", "int"), "EQUAL", 1))
            marks_v = sample(marks, type, g_read("a", type), index())
        hit = int_compare(sample(marks, "int", g_read("c", "int"), index()), "GREATER_THAN", 0)
        return hit, marks_v

    def combine_existing(name, type, mode, v):
        if mode == "set":
            return v
        old = read(name, type)
        if type == "vector":
            n = new("ShaderNodeVectorMath")
            n.operation = {"add": "ADD", "min": "MINIMUM", "max": "MAXIMUM"}[mode]
        elif type == "int":
            n = new("FunctionNodeIntegerMath")
            n.operation = {"add": "ADD", "min": "MINIMUM", "max": "MAXIMUM"}[mode]
        else:
            n = new("ShaderNodeMath")
            n.operation = {"add": "ADD", "min": "MINIMUM", "max": "MAXIMUM"}[mode]
        links.new(old, n.inputs[0])
        links.new(v, n.inputs[1])
        return n.outputs[0]

    def write_other(geo, op):
        """setpointattrib() & co.: each element gets the values of the records naming it."""
        name, type, dom, mode = (op.info[k] for k in ("name", "type", "domain", "mode"))
        hit, v = gathered(geo, records(op), dom, mode, type)
        if name == "position":
            n = new("GeometryNodeSetPosition")
            links.new(geo, n.inputs["Geometry"])
            links.new(combine_existing(name, type, mode, v), n.inputs["Position"])
            links.new(hit, n.inputs["Selection"])
            return n.outputs[0]
        n = new("GeometryNodeStoreNamedAttribute")
        n.data_type = op.info["storage"] or ATTR_TYPE[type]
        n.domain = dom
        n.inputs["Name"].default_value = name
        n.label = "@" + name
        links.new(geo, n.inputs["Geometry"])
        links.new(combine_existing(name, type, mode, v), _enabled(n.inputs, "Value")[0])
        links.new(hit, n.inputs["Selection"])
        return n.outputs["Geometry"]

    def geometry_ops(geo, ops):
        kinds = lambda k: [op for op in ops if op.kind == k]  # noqa: E731
        # New points, in call order, after the existing ones (vertices when there is a mesh).
        adds = kinds("addpoint")
        if adds:
            recs = g_store(join([records(op) for op in adds]), "n", "int", index())  # n: the order they're numbered in
            at = new("GeometryNodeSetPosition")
            links.new(delete(recs, "POINT", int_compare(g_read("src", "int"), "GREATER_EQUAL", 0)), at.inputs["Geometry"])
            links.new(g_read("pos", "vector"), at.inputs["Position"])
            copies = pick(split_points(geo)[0], delete(recs, "POINT", int_compare(g_read("src", "int"), "LESS_THAN", 0)),
                          "src", [("n", "int")])  # addpoint(0, ptnum): a copy of that point
            fresh = sort(join([at.outputs[0], to_points(copies)]), weight=g_read("n", "int"))
            # They join the points the numbers count: mesh vertices when there is a mesh,
            # else the point cloud, else (only curves) one-point curves.
            verts = new("GeometryNodePointsToVertices")
            links.new(fresh, verts.inputs["Points"])
            singles = new("GeometryNodePointsToCurves")
            links.new(fresh, singles.inputs["Points"])
            links.new(index(), singles.inputs["Curve Group ID"])
            only_curves = int_compare(int_math("ADD", component_points(geo, "POINTCLOUD"),
                                               int_compare(component_points(geo, "CURVE"), "EQUAL", 0)), "EQUAL", 0)
            loose = new("GeometryNodeSwitch")
            loose.input_type = "GEOMETRY"
            links.new(only_curves, _enabled(loose.inputs, "Switch")[0])
            links.new(join([geo, fresh]), _enabled(loose.inputs, "False")[0])
            links.new(join([geo, singles.outputs[0]]), _enabled(loose.inputs, "True")[0])
            sw = new("GeometryNodeSwitch")
            sw.input_type = "GEOMETRY"
            links.new(int_compare(component_points(geo, "MESH"), "GREATER_THAN", 0), _enabled(sw.inputs, "Switch")[0])
            links.new(_enabled(loose.outputs, "Output")[0], _enabled(sw.inputs, "False")[0])
            links.new(join([geo, verts.outputs[0]]), _enabled(sw.inputs, "True")[0])
            geo = _enabled(sw.outputs, "Output")[0]
        # New primitives from their corners: copies of the points they use.
        corners = kinds("vertex")
        if corners:
            recs = join([records(op) for op in corners])
            copies = to_points(pick(split_points(geo)[0], recs, "pt", [("prim", "int"), ("ty", "int")]))
            types = {op.info["ptype"] for op in corners}
            if "poly" in types:
                polys = delete(copies, "POINT", int_compare(g_read("ty", "int"), "NOT_EQUAL", 0))
                polys = g_store(polys, "rp", "vector", new("GeometryNodeInputPosition").outputs[0])
                curves = new("GeometryNodePointsToCurves")
                links.new(polys, curves.inputs["Points"])
                links.new(g_read("prim", "int"), curves.inputs["Curve Group ID"])
                links.new(index(), curves.inputs["Weight"])
                cyc = new("GeometryNodeSetSplineCyclic")
                cyc.inputs["Cyclic"].default_value = True
                links.new(curves.outputs[0], cyc.inputs[0])
                path = new("GeometryNodeCurvePrimitiveLine")
                path.inputs["End"].default_value = (0.0, 0.0, 0.0)
                one = new("GeometryNodeResampleCurve")
                one.inputs["Count"].default_value = 1
                links.new(path.outputs[0], one.inputs[0])
                faces = new("GeometryNodeCurveToMesh")  # a one-point path: each profile becomes its cap
                faces.inputs["Fill Caps"].default_value = True
                links.new(one.outputs[0], faces.inputs["Curve"])
                links.new(cyc.outputs[0], faces.inputs["Profile Curve"])
                placed = new("GeometryNodeSetPosition")
                links.new(faces.outputs[0], placed.inputs["Geometry"])
                links.new(g_read("rp", "vector"), placed.inputs["Position"])
                # Curve to Mesh's cap runs the profile backwards: Blender's winding of Houdini's
                # clockwise order, so the face points the way it would in Houdini.
                # The corners join the points they copy: Merge Points by point number.
                sep = new("GeometryNodeSeparateComponents")
                links.new(geo, sep.inputs[0])
                cloud = new("GeometryNodePointsToVertices")
                links.new(sep.outputs["Point Cloud"], cloud.inputs["Points"])
                whole = join([sep.outputs["Mesh"], cloud.outputs[0], sep.outputs["Curve"], sep.outputs["Instances"]])
                whole = g_store(whole, "sp", "int", index())
                merge = new("GeometryNodeMergePoints")
                links.new(join([whole, placed.outputs[0]]), merge.inputs["Geometry"])
                links.new(g_read("sp", "int"), merge.inputs["Merge ID"])
                geo = merge.outputs[0]
            if "polyline" in types:
                lines = delete(copies, "POINT", int_compare(g_read("ty", "int"), "NOT_EQUAL", 1))
                curves = new("GeometryNodePointsToCurves")
                links.new(lines, curves.inputs["Points"])
                links.new(g_read("prim", "int"), curves.inputs["Curve Group ID"])
                links.new(index(), curves.inputs["Weight"])
                geo = join([geo, curves.outputs[0]])
        # Writes to other elements, call by call.
        for op in kinds("set"):
            geo = on_points(geo, op.info["domain"], lambda g, op=op: write_other(g, op))
        # Removals: mark every target first (removing renumbers), faces and curves before points.
        groups = {}
        for op in kinds("remove"):
            groups.setdefault((op.info["domain"], op.info["mode"]), []).append(op)
        flags = []
        for k, ((dom, mode), group) in enumerate(groups.items()):
            def mark(g, dom=dom, group=group, k=k):
                hit, _ = gathered(g, join([records(op) for op in group]), dom, "set", None)
                return store(g, f"{G}del{k}", "bool", hit, dom)
            geo = on_points(geo, dom, mark)
            flags.append((dom, mode, f"{G}del{k}"))
        for dom, mode, flag in sorted(flags, key=lambda f: f[0] == "POINT"):
            geo = delete(geo, dom, read(flag, "bool"), mode)
        return geo

    # -- geometry chain: chunked loops first (they leave their results on the
    # geometry as hidden attributes), then capture the values that need it
    # (Program.captures), then one write node per Program.stores entry.
    geo = group_input("Geometry")
    for end in program.preludes:
        geo = chunked_loop(end, geo)
        base.append(geo)
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
        record_base.append(geo)

    for st in program.stores:
        if st.name == "N":  # custom normals
            n = new("GeometryNodeSetMeshNormal")
            n.mode = "FREE"
            n.domain = domain
            connect(st.value, n.inputs["Custom Normal"])
            links.new(geo, n.inputs["Mesh"])
            geo = n.outputs["Mesh"]
            continue
        if st.name == "P":
            n = new("GeometryNodeSetPosition")
            if st.offset:
                connect(st.value, n.inputs["Offset"])
            else:
                connect_field(st.value, n.inputs["Position"])
        else:
            n = new("GeometryNodeStoreNamedAttribute")
            n.data_type = st.storage or ATTR_TYPE[st.type]
            n.domain = domain
            n.inputs["Name"].default_value = st.name
            n.label = "@" + st.name
            connect(st.value, _enabled(n.inputs, "Value")[0])
        links.new(geo, n.inputs["Geometry"])
        if st.selection is not None:
            connect(st.selection, n.inputs["Selection"])
        geo = n.outputs["Geometry"]

    if program.geo_ops:  # addpoint() & co.: after the writes, on the written geometry
        geo = geometry_ops(geo, program.geo_ops)

    if program.preludes or program.geo_ops:  # the chunked loops' and geometry records' hidden attributes
        n = new("GeometryNodeRemoveAttribute")
        n.inputs["Pattern Mode"].default_value = "Wildcard"  # a menu socket in 5.2, not a property
        n.inputs["Name"].default_value = TEMP_PREFIX + "*"
        links.new(geo, n.inputs["Geometry"])
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
    "ShaderNodeTexVoronoi": 4,
    "ShaderNodeTexGabor": 1,
    "GeometryNodeSetMeshNormal": 2,
    "GeometryNodeDeleteGeometry": 2,
    "GeometryNodeSortElements": 1,
    "GeometryNodeAccumulateField": 2,
    "GeometryNodeFieldAtIndex": 2,
    "GeometryNodeFieldMinAndMax": 2,
    "GeometryNodeFieldAverage": 2,
    "GeometryNodeFieldVariance": 2,
    "GeometryNodeFieldOnDomain": 2,
    "GeometryNodeBlurAttribute": 1,
    "GeometryNodeDuplicateElements": 1,
    "GeometryNodeAttributeDomainSize": 1,
    "GeometryNodeSampleCurve": 3,
    "GeometryNodeSampleUVSurface": 1,
    "GeometryNodeInputNamedAttribute": 1,
    "GeometryNodeStoreNamedAttribute": 2,
    "GeometryNodeCaptureAttribute": 1,
    "FunctionNodeInputVector": 3,
    "GeometryNodeSampleIndex": 3,
    "GeometryNodeSampleNearest": 1,
    "GeometryNodeAttributeStatistic": 2,
    "GeometryNodeRepeatInput": 0,
    "GeometryNodeRemoveAttribute": 1,
    "NodeClosureInput": 0,
    "NodeClosureOutput": 0,
    "NodeEvaluateClosure": 0,
    "GeometryNodeRepeatOutput": 0,
    "FunctionNodeInputInt": 1,
    "FunctionNodeInputBool": 1,
    "ShaderNodeValue": 1,
    "GeometryNodeProximity": 1,
    "FunctionNodeRotateRotation": 1,
    "FunctionNodeAxesToRotation": 2,
    "FunctionNodeCombineMatrix": 0,
    "GeometryNodeRaycast": 2,  # data type, and the Interpolation menu
    "GeometryNodeSampleNearestSurface": 1,
    "FunctionNodeCombineColor": 1,
    "FunctionNodeSeparateColor": 1,
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
            expanded = s.type in ("VECTOR", "ROTATION") and not s.is_linked and not s.hide_value
            h += SOCKET_ROW * (4 if expanded else 1)
    return h + 10.0


def _layout(tree, group_in, line_of, source, group_source=""):
    nodes = list(tree.nodes)
    feeds = {}
    for link in tree.links:
        feeds.setdefault(link.from_node.name, set()).add(link.to_node.name)
    for n in nodes:  # a zone's input sits left of its output even when no item passes straight through
        if n.bl_idname in ("GeometryNodeRepeatInput", "NodeClosureInput") and n.paired_output:
            feeds.setdefault(n.name, set()).add(n.paired_output.name)
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

