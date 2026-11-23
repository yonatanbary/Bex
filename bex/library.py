"""BEX's library of node groups.

Built-in functions that take several nodes (`relbbox()`, `getbbox_center()`, ...)
compile to one of these groups instead of loose nodes, so the wrangle's tree stays
readable. They are ordinary, visible node groups: users can add them from
Add > BEX and use them in any tree.

Each group is defined here in Python and created on first use. A group is found by
its `bex_library` key (not its name, so renaming it is fine) and rebuilt when its
`version` changes. Single-node operations (math, fit, wrap...) are not groups.
"""

from dataclasses import dataclass

import bpy

KEY_PROP = "bex_library"
VERSION_PROP = "bex_library_version"


@dataclass
class Socket:
    name: str
    type: str  # NodeSocket idname
    default_input: str = "VALUE"  # implicit field when unlinked, e.g. POSITION
    default: object = None  # the socket's value when unlinked


@dataclass
class GroupDef:
    name: str
    description: str
    inputs: list
    outputs: list
    build: object  # fn(tree, group_in, group_out)
    version: int = 1
    icon: str = "NODETREE"  # Add menu icon


GEOMETRY = Socket("Geometry", "NodeSocketGeometry")


def _vmath(nodes, op, x, y):
    n = nodes.new("ShaderNodeVectorMath")
    n.operation = op
    n.location = (x, y)
    return n


def _build_bbox(tree, gin, gout):
    nodes, links = tree.nodes, tree.links
    bb = nodes.new("GeometryNodeBoundBox")
    bb.location = (-300, 0)
    links.new(gin.outputs["Geometry"], bb.inputs["Geometry"])
    size = _vmath(nodes, "SUBTRACT", -100, -60)
    links.new(bb.outputs["Max"], size.inputs[0])
    links.new(bb.outputs["Min"], size.inputs[1])
    total = _vmath(nodes, "ADD", -100, -220)
    links.new(bb.outputs["Min"], total.inputs[0])
    links.new(bb.outputs["Max"], total.inputs[1])
    center = _vmath(nodes, "SCALE", 80, -220)
    links.new(total.outputs[0], center.inputs[0])
    center.inputs["Scale"].default_value = 0.5
    links.new(bb.outputs["Min"], gout.inputs["Min"])
    links.new(bb.outputs["Max"], gout.inputs["Max"])
    links.new(size.outputs[0], gout.inputs["Size"])
    links.new(center.outputs[0], gout.inputs["Center"])


def _build_relbbox(tree, gin, gout):
    nodes, links = tree.nodes, tree.links
    bb = nodes.new("GeometryNodeGroup")
    bb.node_tree = ensure("bbox")
    bb.location = (-300, -120)
    links.new(gin.outputs["Geometry"], bb.inputs["Geometry"])
    offset = _vmath(nodes, "SUBTRACT", -80, 0)
    links.new(gin.outputs["Position"], offset.inputs[0])
    links.new(bb.outputs["Min"], offset.inputs[1])
    rel = _vmath(nodes, "DIVIDE", 120, 0)  # Vector Math divide gives 0 on a flat axis
    links.new(offset.outputs[0], rel.inputs[0])
    links.new(bb.outputs["Size"], rel.inputs[1])
    links.new(rel.outputs[0], gout.inputs["Relative"])


def _build_curl(tree, gin, gout):
    """The curl of a vector noise potential, by central differences (6 noise samples)."""
    nodes, links = tree.nodes, tree.links

    def math(op, a, b, x, y):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        n.location = (x, y)
        for sock, v in zip(n.inputs, (a, b)):
            if isinstance(v, float):
                sock.default_value = v
            else:
                links.new(v, sock)
        return n.outputs[0]

    step = math("DIVIDE", 0.01, gin.outputs["Scale"], -340, 300)  # a small step in noise space
    two_steps = math("MULTIPLY", step, 2.0, -340, 220)
    comps = {}  # (axis, sign) -> Separate XYZ of the noise there
    for axis in range(3):
        for k, sign in enumerate((1.0, -1.0)):
            y = -260 * (axis * 2 + k)
            delta = nodes.new("ShaderNodeCombineXYZ")
            delta.location = (-340, y)
            links.new(math("MULTIPLY", step, sign, -520, y), delta.inputs[axis])
            pos = _vmath(nodes, "ADD", -160, y)
            links.new(gin.outputs["Position"], pos.inputs[0])
            links.new(delta.outputs[0], pos.inputs[1])
            noise = nodes.new("ShaderNodeTexNoise")
            noise.noise_dimensions = "4D"
            noise.location = (20, y)
            links.new(pos.outputs[0], noise.inputs["Vector"])
            for name in ("W", "Scale", "Detail", "Roughness", "Lacunarity", "Distortion"):
                links.new(gin.outputs[name], noise.inputs[name])
            sep = nodes.new("ShaderNodeSeparateXYZ")
            sep.location = (200, y)
            links.new(noise.outputs["Color"], sep.inputs[0])
            comps[(axis, sign)] = sep

    def d(c, axis, y):  # d(noise component c) / d(axis)
        diff = math("SUBTRACT", comps[(axis, 1.0)].outputs[c], comps[(axis, -1.0)].outputs[c], 380, y)
        return math("DIVIDE", diff, two_steps, 540, y)

    curl = nodes.new("ShaderNodeCombineXYZ")
    curl.location = (900, 0)
    for out, (c1, a1, c2, a2) in enumerate(((2, 1, 1, 2), (0, 2, 2, 0), (1, 0, 0, 1))):
        y = -300 * out
        links.new(math("SUBTRACT", d(c1, a1, y), d(c2, a2, y - 120), 720, y), curl.inputs[out])
    links.new(curl.outputs[0], gout.inputs["Curl"])


def _build_copy_to_points(tree, gin, gout):
    """Houdini's Copy to Points: one copy per target point, placed by its instance attributes.

    Instance on Points puts the copies on the points and hands every point attribute to
    its instance. Set Instance Transform then builds each copy's matrix from those
    attributes, in Houdini's order (as column-vector matrices):

        T(P + trans) . O . R . S . T(-pivot)

    O is `orient`, else +Z turned to N (or `v` without N), keeping +Y towards `up` when
    there is one; R is `rot`; S is `scale` times `pscale` (or `radius` with Scale by
    Radius on: Blender fills radius on every point it makes). A `transform` attribute
    replaces O . R . S. Missing attributes leave their part out.
    """
    nodes, links = tree.nodes, tree.links

    def new(idname, x, y, **props):
        n = nodes.new(idname)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def attr(name, data_type, y):
        n = new("GeometryNodeInputNamedAttribute", -1400, y, data_type=data_type)
        n.inputs["Name"].default_value = name
        return n

    def switch(input_type, cond, off, on, x, y):
        n = new("GeometryNodeSwitch", x, y, input_type=input_type)
        links.new(cond, n.inputs["Switch"])
        for sock, v in ((n.inputs["False"], off), (n.inputs["True"], on)):
            if isinstance(v, bpy.types.NodeSocket):
                links.new(v, sock)
            elif v is not None:
                sock.default_value = v
        return n.outputs[0]

    def boolean(op, a, b, x, y):
        n = new("FunctionNodeBooleanMath", x, y, operation=op)
        links.new(a, n.inputs[0])
        links.new(b, n.inputs[1])
        return n.outputs[0]

    # Instance on Points leaves radius behind, so it is captured on the points first
    radius = new("GeometryNodeInputNamedAttribute", -1400, 500, data_type="FLOAT")
    radius.inputs["Name"].default_value = "radius"
    cap = new("GeometryNodeCaptureAttribute", -1000, 400)
    cap.capture_items.new("FLOAT", "Radius")
    cap.capture_items.new("BOOLEAN", "Has Radius")
    links.new(gin.outputs["Target Points"], cap.inputs["Geometry"])
    links.new(radius.outputs["Attribute"], cap.inputs["Radius"])
    links.new(radius.outputs["Exists"], cap.inputs["Has Radius"])

    inst = new("GeometryNodeInstanceOnPoints", -200, 300)
    links.new(cap.outputs["Geometry"], inst.inputs["Points"])
    links.new(gin.outputs["Selection"], inst.inputs["Selection"])
    links.new(gin.outputs["Geometry"], inst.inputs["Instance"])

    # The instance attributes, read on the instances (they carry their point's attributes).
    n_, v_, up = attr("N", "FLOAT_VECTOR", 0), attr("v", "FLOAT_VECTOR", -160), attr("up", "FLOAT_VECTOR", -320)
    vel = attr("velocity", "FLOAT_VECTOR", -480)  # BEX's @v, and Blender's own name for it
    orient, rot = attr("orient", "QUATERNION", -640), attr("rot", "QUATERNION", -800)
    scale, pscale = attr("scale", "FLOAT_VECTOR", -960), attr("pscale", "FLOAT", -1120)
    trans, pivot = attr("trans", "FLOAT_VECTOR", -1280), attr("pivot", "FLOAT_VECTOR", -1440)
    xform = attr("transform", "FLOAT4X4", -1600)

    # O: orient, else N (or v) with up
    has_v = boolean("OR", v_.outputs["Exists"], vel.outputs["Exists"], -1200, -200)
    v_dir = switch("VECTOR", v_.outputs["Exists"], vel.outputs["Attribute"], v_.outputs["Attribute"], -1050, -200)
    has_dir = boolean("OR", n_.outputs["Exists"], has_v, -1050, -60)
    direction = switch("VECTOR", n_.outputs["Exists"], v_dir, n_.outputs["Attribute"], -900, -100)
    turn = new("FunctionNodeAlignRotationToVector", -750, -40)  # the shortest turn of +Z onto N
    links.new(direction, turn.inputs["Vector"])
    axes = new("FunctionNodeAxesToRotation", -750, -220, primary_axis="Z", secondary_axis="Y")
    links.new(direction, axes.inputs["Primary Axis"])
    links.new(up.outputs["Attribute"], axes.inputs["Secondary Axis"])
    aligned = switch("ROTATION", up.outputs["Exists"], turn.outputs[0], axes.outputs[0], -600, -100)
    aligned = switch("ROTATION", has_dir, None, aligned, -450, -100)
    o = switch("ROTATION", orient.outputs["Exists"], aligned, orient.outputs["Attribute"], -300, -300)
    r = switch("ROTATION", rot.outputs["Exists"], None, rot.outputs["Attribute"], -300, -460)
    o_r = new("FunctionNodeRotateRotation", -150, -360, rotation_space="LOCAL")  # O . R: rot first
    links.new(o, o_r.inputs["Rotation"])
    links.new(r, o_r.inputs["Rotate By"])

    # S: scale * pscale
    s = switch("VECTOR", scale.outputs["Exists"], (1.0, 1.0, 1.0), scale.outputs["Attribute"], -450, -900)
    # Blender gives every point a radius, so it only stands in for pscale when asked to
    use_radius = boolean("AND", gin.outputs["Scale by Radius"], cap.outputs["Has Radius"], -600, -1160)
    ps = switch("FLOAT", use_radius, 1.0, cap.outputs["Radius"], -600, -1060)
    ps = switch("FLOAT", pscale.outputs["Exists"], ps, pscale.outputs["Attribute"], -450, -1060)
    s_ps = _vmath(nodes, "SCALE", -300, -950)
    links.new(s, s_ps.inputs[0])
    links.new(ps, s_ps.inputs["Scale"])

    # T(P + trans) and T(-pivot)
    pos = new("GeometryNodeInputPosition", -450, -1250)
    moved = _vmath(nodes, "ADD", -300, -1250)
    links.new(pos.outputs[0], moved.inputs[0])
    links.new(trans.outputs["Attribute"], moved.inputs[1])
    back = _vmath(nodes, "SCALE", -300, -1450)
    links.new(pivot.outputs["Attribute"], back.inputs[0])
    back.inputs["Scale"].default_value = -1.0
    unpivot = new("FunctionNodeCombineTransform", -150, -1450)
    links.new(back.outputs[0], unpivot.inputs["Translation"])

    trs = new("FunctionNodeCombineTransform", 0, -700)  # T . O . R . S
    links.new(moved.outputs[0], trs.inputs["Translation"])
    links.new(o_r.outputs[0], trs.inputs["Rotation"])
    links.new(s_ps.outputs[0], trs.inputs["Scale"])
    place = new("FunctionNodeCombineTransform", 0, -1250)  # T alone
    links.new(moved.outputs[0], place.inputs["Translation"])
    t_x = new("FunctionNodeMatrixMultiply", 150, -1350)  # T . transform
    links.new(place.outputs[0], t_x.inputs[0])
    links.new(xform.outputs["Attribute"], t_x.inputs[1])
    oriented = switch("MATRIX", xform.outputs["Exists"], trs.outputs[0], t_x.outputs[0], 300, -800)
    m = switch("MATRIX", gin.outputs["Use Point Orientations"], place.outputs[0], oriented, 450, -800)
    full = new("FunctionNodeMatrixMultiply", 600, -800)  # ... . T(-pivot)
    links.new(m, full.inputs[0])
    links.new(unpivot.outputs[0], full.inputs[1])

    set_xform = new("GeometryNodeSetInstanceTransform", 100, 300)
    links.new(inst.outputs[0], set_xform.inputs["Instances"])
    links.new(full.outputs[0], set_xform.inputs["Transform"])
    realize = new("GeometryNodeRealizeInstances", 300, 200)
    realize.inputs["Realize All"].default_value = False  # only the copies; instances inside them stay
    realize.inputs["Depth"].default_value = 1  # Depth counts levels realized: 0 realizes nothing
    links.new(set_xform.outputs[0], realize.inputs["Geometry"])
    out = switch("GEOMETRY", gin.outputs["Pack and Instance"], realize.outputs[0], set_xform.outputs[0], 500, 300)
    links.new(out, gout.inputs["Geometry"])
    gin.location, gout.location = (-1700, 300), (700, 300)


VEC = "NodeSocketVector"
FLOAT = "NodeSocketFloat"
BOOL = "NodeSocketBool"

# key -> definition. The compiler emits IR `group` nodes with these keys; output
# order here is the IR output index.
GROUPS = {
    "bbox": GroupDef(
        "BEX Bounding Box", "Min, max, size and center of the geometry's bounding box",
        [GEOMETRY], [Socket("Min", VEC), Socket("Max", VEC), Socket("Size", VEC), Socket("Center", VEC)],
        _build_bbox, icon="PIVOT_BOUNDBOX",
    ),
    "relbbox": GroupDef(
        "BEX Relative Bounding Box", "Position inside the geometry's bounding box, 0 to 1 on each axis",
        [GEOMETRY, Socket("Position", VEC, "POSITION")], [Socket("Relative", VEC)],
        _build_relbbox, icon="PIVOT_BOUNDBOX",
    ),
    "curl": GroupDef(
        "BEX Curl Noise", "A swirling, divergence-free vector field: the curl of a noise field",
        [Socket("Position", VEC, "POSITION"), Socket("W", FLOAT), Socket("Scale", FLOAT, default=1.0),
         Socket("Detail", FLOAT), Socket("Roughness", FLOAT, default=0.5), Socket("Lacunarity", FLOAT, default=2.0),
         Socket("Distortion", FLOAT)],
        [Socket("Curl", VEC)], _build_curl, icon="FORCE_VORTEX",
    ),
    "copy_to_points": GroupDef(
        "BEX Copy to Points",
        "Copy the geometry onto each target point, placed by its attributes like Houdini "
        "(P, orient, N, up, v, rot, scale, pscale, trans, pivot, transform)",
        [GEOMETRY, Socket("Target Points", "NodeSocketGeometry"), Socket("Selection", BOOL, default=True),
         Socket("Pack and Instance", BOOL, default=False), Socket("Use Point Orientations", BOOL, default=True),
         Socket("Scale by Radius", BOOL, default=False)],
        [GEOMETRY], _build_copy_to_points, icon="PARTICLE_POINT",
    ),
}


def find(key):
    for tree in bpy.data.node_groups:
        if tree.get(KEY_PROP) == key and tree.bl_idname == "GeometryNodeTree":
            return tree
    return None


def ensure(key):
    """The library group for `key`, created or rebuilt as needed."""
    d = GROUPS[key]
    tree = find(key)
    if tree is not None and (tree.library or tree.get(VERSION_PROP) == d.version):
        return tree
    if tree is None:
        tree = bpy.data.node_groups.new(d.name, "GeometryNodeTree")
        tree[KEY_PROP] = key
    _rebuild(tree, d)
    tree[VERSION_PROP] = d.version
    return tree


def _rebuild(tree, d):
    tree.use_fake_user = True  # kept in the file even when no node uses it
    tree.description = d.description
    tree.color_tag = "VECTOR"
    iface = tree.interface
    wanted = [("OUTPUT", s) for s in d.outputs] + [("INPUT", s) for s in d.inputs]
    have = [(i.in_out, i.name, i.socket_type) for i in iface.items_tree if i.item_type == "SOCKET"]
    if have != [(io, s.name, s.type) for io, s in wanted]:
        iface.clear()  # only when the signature changed, so links on users survive a rebuild
        for io, s in wanted:
            iface.new_socket(s.name, in_out=io, socket_type=s.type)
    items = [i for i in iface.items_tree if i.item_type == "SOCKET"]
    for item, (io, s) in zip(items, wanted):
        if io == "INPUT" and s.default is not None:
            item.default_value = s.default
        if io == "INPUT" and s.default_input != "VALUE":
            item.default_input = s.default_input
            item.hide_value = True
    tree.nodes.clear()
    gin, gout = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    gin.location, gout.location = (-520, 0), (320, 0)
    d.build(tree, gin, gout)


def ensure_all():
    for key in GROUPS:
        ensure(key)


# -- Add > BEX menu --------------------------------------------------------

class BEX_OT_add_library_node(bpy.types.Operator):
    """Add a BEX library node group"""

    bl_idname = "bex.add_library_node"
    bl_label = "Add BEX Node"
    bl_options = {"REGISTER", "UNDO"}

    key: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space and space.type == "NODE_EDITOR" and space.edit_tree and space.tree_type == "GeometryNodeTree"

    @classmethod
    def description(cls, context, props):
        d = GROUPS.get(props.key)
        return d.description if d else cls.__doc__

    def invoke(self, context, event):
        if self.key not in GROUPS:
            return {"CANCELLED"}
        tree = ensure(self.key)
        return bpy.ops.node.add_node(
            "INVOKE_DEFAULT", type="GeometryNodeGroup", use_transform=True,
            settings=[{"name": "node_tree", "value": f"bpy.data.node_groups[{tree.name!r}]"}],
        )


class NODE_MT_bex_library(bpy.types.Menu):
    bl_idname = "NODE_MT_bex_library"
    bl_label = "BEX"

    def draw(self, context):
        for key, d in GROUPS.items():
            op = self.layout.operator(BEX_OT_add_library_node.bl_idname, text=d.name.removeprefix("BEX "), icon=d.icon)
            op.key = key


classes = (BEX_OT_add_library_node, NODE_MT_bex_library)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
