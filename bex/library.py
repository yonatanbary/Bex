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


VEC = "NodeSocketVector"
FLOAT = "NodeSocketFloat"

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
