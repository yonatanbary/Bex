"""Builds examples/bex_examples.blend: BEX Wrangle examples, from basic to complex.

Run:  "/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python examples/make_examples.py
"""

import math
import sys
import textwrap
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bex  # noqa: E402

bex.register()

SPACING = 5.0
EXAMPLES = []  # (title, notes, build function)


def example(title, notes):
    def wrap(fn):
        EXAMPLES.append((title, notes, fn))
        return fn
    return wrap


# -- helpers -----------------------------------------------------------------

def new_object(name, mesh_op=None, **kw):
    if mesh_op:
        mesh_op(**kw)
        obj = bpy.context.object
    else:
        obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
        bpy.context.collection.objects.link(obj)
    obj.name = name
    return obj


def tree_for(obj):
    tree = bpy.data.node_groups.new(obj.name, "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    obj.modifiers.new("BEX", "NODES").node_group = tree
    gin, gout = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    gin.location, gout.location = (-600, 0), (900, 0)
    return tree, gin, gout


def wrangle(tree, src, domain="POINT", group="", x=0):
    node = tree.nodes.new("GeometryNodeBexWrangle")
    node.location = (x, 0)
    node.width = 420
    node.domain = domain
    node.group = group
    node.script.from_string(textwrap.dedent(src).strip("\n") + "\n")
    node.sync(force=True)
    return node


def chain(tree, *sockets):
    """Link each node's first geometry output to the next one's first input."""
    for a, b in zip(sockets, sockets[1:]):
        tree.links.new(a, b)


def colour_material(name="BEX Cd"):
    """Shows the Cd attribute (and Cd on faces) in Material Preview."""
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    attr = nodes.new("ShaderNodeAttribute")
    attr.attribute_name = "Cd"
    attr.location = (-300, 200)
    bsdf = nodes.get("Principled BSDF")
    mat.node_tree.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.5
    return mat


def set_material(tree, geo_socket, mat, x=600):
    n = tree.nodes.new("GeometryNodeSetMaterial")
    n.location = (x, -200)
    n.inputs["Material"].default_value = mat
    tree.links.new(geo_socket, n.inputs["Geometry"])
    return n.outputs[0]


def label(text, notes, x):
    bpy.ops.object.text_add(location=(x - 1.8, -3.2, 0))
    t = bpy.context.object
    t.data.body = text
    t.data.size = 0.32
    t.name = "Label " + text
    bpy.ops.object.text_add(location=(x - 1.8, -3.7, 0))
    n = bpy.context.object
    n.data.body = notes
    n.data.size = 0.17
    n.name = "Notes " + text


# -- the examples, basic to complex ---------------------------------------------

@example("1  A wave", "@P.z from a sine of x.\nTwo values on the node:\nfreq and amp (ch*()).")
def ex_wave(x):
    obj = new_object("01 Wave", bpy.ops.mesh.primitive_grid_add, x_subdivisions=60, y_subdivisions=60,
                     size=4, location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        # The simplest wrangle: move every point.
        @P.z = sin(@P.x * chf("freq", 3, min=0, max=20)) * chf("amp", 0.3)
    """)
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, node.outputs[0], gout.inputs[0])
    return node


@example("2  Colour by height", "Noise displacement,\nthen a colour from a ramp\ndrawn on the node.")
def ex_colour(x):
    obj = new_object("02 Colour", bpy.ops.mesh.primitive_grid_add, x_subdivisions=80, y_subdivisions=80,
                     size=4, location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        # Fractal noise, then colour from the height through a ramp.
        @P.z = snoise(@P, freq=chf("freq", 0.8), octaves=chi("octaves", 5, min=1, max=10)) * 0.6
        h = fit(@P.z, -0.4, 0.4, 0, 1)
        @Cd = hsvtorgb((0.66 * (1 - chramp("shape", h)), 0.75, 1))
    """)
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, set_material(tree, node.outputs[0], colour_material()), gout.inputs[0])
    return node


@example("3  Groups and conditions", "The Group field picks the\nelements (@P.x > 0); if / elif\ncolour the rest by rings.")
def ex_groups(x):
    obj = new_object("03 Groups", bpy.ops.mesh.primitive_grid_add, x_subdivisions=60, y_subdivisions=60,
                     size=4, location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        # Only the elements in the Group field (above) are changed.
        r = length(@P)
        ring = int(r * 4) % 3
        if ring == 0:
            @Cd = (1, 0.3, 0.2)
        elif ring == 1:
            @Cd = (1, 0.8, 0.2)
        else:
            @Cd = (0.2, 0.5, 1)
        @P.z = 0.15 * ring
    """, group="@P.x > 0")
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, set_material(tree, node.outputs[0], colour_material()), gout.inputs[0])
    return node


@example("4  A spiral of instances", "Points placed in a spiral;\np@orient and @scale turn and\nsize the cubes instanced on them.")
def ex_spiral(x):
    obj = new_object("04 Spiral", location=None) if False else new_object("04 Spiral")
    obj.location = (x, 0, 0)
    tree, gin, gout = tree_for(obj)
    pts = tree.nodes.new("GeometryNodePoints")
    pts.inputs["Count"].default_value = 160
    pts.location = (-400, 0)
    node = wrangle(tree, """
        t = @ptnum / @numpt
        a = t * TAU * chf("turns", 3)
        @P = (cos(a), sin(a), t * 2) * (0.3 + 1.5 * t)
        p@orient = quaternion(a, (0, 0, 1)) * quaternion(t * PI, (1, 0, 0))
        @scale = (0.08 + 0.12 * t, 0.08 + 0.12 * t, 0.08 + 0.12 * t)
        @Cd = hsvtorgb((t, 0.7, 1))
    """)
    cube, inst = tree.nodes.new("GeometryNodeMeshCube"), tree.nodes.new("GeometryNodeInstanceOnPoints")
    cube.location, inst.location = (300, -250), (500, 0)
    rot, scale = (tree.nodes.new("GeometryNodeInputNamedAttribute") for _ in range(2))
    rot.data_type, scale.data_type = "QUATERNION", "FLOAT_VECTOR"
    rot.inputs["Name"].default_value, scale.inputs["Name"].default_value = "orient", "scale"
    rot.location, scale.location = (300, -400), (300, -500)
    tree.links.new(pts.outputs[0], node.inputs[0])
    tree.links.new(node.outputs[0], inst.inputs["Points"])
    tree.links.new(cube.outputs[0], inst.inputs["Instance"])
    tree.links.new(rot.outputs["Attribute"], inst.inputs["Rotation"])
    tree.links.new(scale.outputs["Attribute"], inst.inputs["Scale"])
    real = tree.nodes.new("GeometryNodeRealizeInstances")
    real.location = (700, 0)
    tree.links.new(inst.outputs[0], real.inputs[0])
    chain(tree, set_material(tree, real.outputs[0], colour_material(), x=800), gout.inputs[0])
    return node


@example("5  Functions and while loops", "A Mandelbrot height field: an\nown function with a while loop\nthat ends at its own time per point.")
def ex_mandel(x):
    obj = new_object("05 Mandelbrot", bpy.ops.mesh.primitive_grid_add, x_subdivisions=150, y_subdivisions=150,
                     size=4, location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        def escape(c, limit):
            z = (0, 0, 0)
            i = 0
            while i < limit:
                z = (z.x * z.x - z.y * z.y + c.x, 2 * z.x * z.y + c.y, 0)
                if length2(z) > 4:
                    break
                i += 1
            return i

        limit = chi("iterations", 40, min=1, max=200)
        n = escape(@P * 0.7 - (0.6, 0, 0), limit)
        t = n / limit
        @P.z = t * 0.5
        @Cd = hsvtorgb((0.7 - 0.7 * t, 0.8, 0.3 + 0.7 * t))
    """)
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, set_material(tree, node.outputs[0], colour_material()), gout.inputs[0])
    return node


@example("6  Statistics", "Noise, smoothed with blur(),\nthen stretched to 0-1 with\nminof() / maxof(); mean() tints it.")
def ex_stats(x):
    obj = new_object("06 Statistics", bpy.ops.mesh.primitive_grid_add, x_subdivisions=80, y_subdivisions=80,
                     size=4, location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        h = blur(noise(@P * 3), chi("smooth", 6))
        lo, hi = minof(h), maxof(h)
        t = (h - lo) / (hi - lo)       # always uses the full 0-1 range
        @P.z = t * 0.6
        @Cd = (1, 0.4, 0.2) if h > mean(h) else (0.2, 0.5, 1)
    """)
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, set_material(tree, node.outputs[0], colour_material()), gout.inputs[0])
    return node


@example("7  Flow field", "Points on a grid, turned to\nfollow curl noise (dihedral),\nas cones: a field you can see.")
def ex_flow(x):
    obj = new_object("07 Flow")
    obj.location = (x, 0, 0)
    tree, gin, gout = tree_for(obj)
    grid = tree.nodes.new("GeometryNodeMeshGrid")
    grid.location = (-400, 0)
    for k, v in (("Size X", 4), ("Size Y", 4), ("Vertices X", 28), ("Vertices Y", 28)):
        grid.inputs[k].default_value = v
    node = wrangle(tree, """
        flow = curlnoise(@P * chf("scale", 0.6) + (0, 0, @Time * 0.2))
        flow.z *= 0.2
        p@orient = dihedral((0, 0, 1), normalize(flow))
        @Cd = hsvtorgb((fit(atan2(flow.y, flow.x), -PI, PI, 0, 1), 0.7, 1))
        f@size = fit(length(flow), 0, 2, 0.4, 1.2)
    """)
    cone, inst = tree.nodes.new("GeometryNodeMeshCone"), tree.nodes.new("GeometryNodeInstanceOnPoints")
    cone.inputs["Radius Top"].default_value, cone.inputs["Radius Bottom"].default_value = 0.0, 0.04
    cone.inputs["Depth"].default_value, cone.inputs["Vertices"].default_value = 0.22, 8
    rot, size = (tree.nodes.new("GeometryNodeInputNamedAttribute") for _ in range(2))
    rot.data_type, size.data_type = "QUATERNION", "FLOAT"
    rot.inputs["Name"].default_value, size.inputs["Name"].default_value = "orient", "size"
    tree.links.new(grid.outputs[0], node.inputs[0])
    tree.links.new(node.outputs[0], inst.inputs["Points"])
    tree.links.new(cone.outputs[0], inst.inputs["Instance"])
    tree.links.new(rot.outputs["Attribute"], inst.inputs["Rotation"])
    tree.links.new(size.outputs["Attribute"], inst.inputs["Scale"])
    real = tree.nodes.new("GeometryNodeRealizeInstances")
    tree.links.new(inst.outputs[0], real.inputs[0])
    chain(tree, set_material(tree, real.outputs[0], colour_material(), x=800), gout.inputs[0])
    return node


@example("8  Growing hair", "addpoint() in a loop and\naddvertex(): one curve per point,\nbending with noise, as tubes.")
def ex_hair(x):
    obj = new_object("08 Hair", bpy.ops.mesh.primitive_ico_sphere_add, subdivisions=3, radius=1.0,
                     location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        # One polyline per point: it starts at the point and grows out.
        hair = addprim(0, "polyline")
        addvertex(0, hair, @ptnum)
        p = @P
        d = @N
        for i in range(chi("segments", 8, min=1, max=30)):
            d = normalize(d + vnoise(p * 2, freq=1.5) - (0.5, 0.5, 0.5) - (0, 0, 0.15))
            p += d * chf("step", 0.12)
            addvertex(0, hair, addpoint(0, p))
    """)
    to_mesh, circle = tree.nodes.new("GeometryNodeCurveToMesh"), tree.nodes.new("GeometryNodeCurvePrimitiveCircle")
    circle.inputs["Resolution"].default_value, circle.inputs["Radius"].default_value = 6, 0.012
    sep = tree.nodes.new("GeometryNodeSeparateComponents")
    join = tree.nodes.new("GeometryNodeJoinGeometry")
    tree.links.new(gin.outputs[0], node.inputs[0])
    tree.links.new(node.outputs[0], sep.inputs[0])
    tree.links.new(sep.outputs["Curve"], to_mesh.inputs["Curve"])
    tree.links.new(circle.outputs[0], to_mesh.inputs["Profile Curve"])
    tree.links.new(to_mesh.outputs[0], join.inputs[0])
    tree.links.new(gin.outputs[0], join.inputs[0])
    chain(tree, join.outputs[0], gout.inputs[0])
    return node


@example("9  Plexus", "Arrays: nearpoints() finds each\npoint's neighbours; addprim() joins\nthem with lines, swept into tubes.")
def ex_plexus(x):
    obj = new_object("09 Plexus")
    obj.location = (x, 0, 0)
    tree, gin, gout = tree_for(obj)
    pts = tree.nodes.new("GeometryNodePoints")
    pts.inputs["Count"].default_value = 140
    node = wrangle(tree, """
        # Scatter the points in a ball, then connect each to its nearest ones.
        @P = sample_sphere_uniform(vrand(@ptnum)) * 2
        near = nearpoints(0, @P, chf("radius", 0.75), 5)
        for other in near:
            if other > @ptnum:          # each pair once
                addprim(0, "polyline", @ptnum, other)
        i@links = len(near) - 1
    """)
    tree.links.new(pts.outputs[0], node.inputs[0])
    sep = tree.nodes.new("GeometryNodeSeparateComponents")
    to_mesh, circle = tree.nodes.new("GeometryNodeCurveToMesh"), tree.nodes.new("GeometryNodeCurvePrimitiveCircle")
    circle.inputs["Resolution"].default_value, circle.inputs["Radius"].default_value = 5, 0.008
    sphere, inst = tree.nodes.new("GeometryNodeMeshUVSphere"), tree.nodes.new("GeometryNodeInstanceOnPoints")
    sphere.inputs["Radius"].default_value = 0.035
    sphere.inputs["Segments"].default_value, sphere.inputs["Rings"].default_value = 10, 6
    join, real = tree.nodes.new("GeometryNodeJoinGeometry"), tree.nodes.new("GeometryNodeRealizeInstances")
    tree.links.new(node.outputs[0], sep.inputs[0])
    tree.links.new(sep.outputs["Curve"], to_mesh.inputs["Curve"])
    tree.links.new(circle.outputs[0], to_mesh.inputs["Profile Curve"])
    tree.links.new(sep.outputs["Point Cloud"], inst.inputs["Points"])
    tree.links.new(sphere.outputs[0], inst.inputs["Instance"])
    tree.links.new(inst.outputs[0], real.inputs[0])
    tree.links.new(to_mesh.outputs[0], join.inputs[0])
    tree.links.new(real.outputs[0], join.inputs[0])
    chain(tree, join.outputs[0], gout.inputs[0])
    return node


@example("10  Inset and extrude", "Runs over faces: primpoints()\nas an array, new points in a loop,\nnew faces, the old face removed.")
def ex_inset(x):
    obj = new_object("10 Inset", bpy.ops.mesh.primitive_ico_sphere_add, subdivisions=2, radius=1.3,
                     location=(x, 0, 0))
    tree, gin, gout = tree_for(obj)
    node = wrangle(tree, """
        # Every face: a smaller copy pushed out along the normal, joined by side walls.
        pts = primpoints(0, @primnum)
        n = len(pts)
        h = chf("height", 0.25) * rand(@primnum)
        inner = []
        for k in range(n):
            corner = point(0, "P", pts[k])
            inner.append(addpoint(0, lerp(corner, @P, chf("inset", 0.35)) + @N * h))
        cap = addprim(0, "poly", inner)
        setprimattrib(0, "Cd", cap, hsvtorgb((rand(@primnum + 7), 0.6, 1)))
        for k in range(n):
            j = (k + 1) % n
            addprim(0, "poly", pts[k], pts[j], inner[j], inner[k])
        removeprim(0, @primnum, 0)
    """, domain="FACE")
    chain(tree, gin.outputs[0], node.inputs[0])
    chain(tree, set_material(tree, node.outputs[0], colour_material()), gout.inputs[0])
    return node


# -- build the file ---------------------------------------------------------

def main():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    failed = []
    for k, (title, notes, build) in enumerate(EXAMPLES):
        x = k * SPACING
        node = build(x)
        label(title, notes, x)
        if node.error:
            failed.append((title, node.error))
        print(f"{title}: {node.error or 'ok'}  {node.warning}")
    # a sun, and every 3D view in Material Preview looking at the row
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 10), rotation=(math.radians(40), 0, math.radians(30)))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                space.shading.type = "MATERIAL"
                r3d = space.region_3d
                r3d.view_location = ((len(EXAMPLES) - 1) * SPACING / 2, 0, 0)
                r3d.view_distance = len(EXAMPLES) * SPACING * 0.75
    out = ROOT / "examples" / "bex_examples.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(out))
    print("saved", out)
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


main()
