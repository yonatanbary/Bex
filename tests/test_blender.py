"""End-to-end tests inside Blender: compile scripts, evaluate the modifier, check the mesh.

Run:  "/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python tests/test_blender.py
"""

import math
import textwrap
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bex  # noqa: E402

bex.register()


def make_setup(src, domain="POINT"):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=2)
    obj = bpy.context.object
    tree = bpy.data.node_groups.new("Test", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gin, gout = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    node = tree.nodes.new("GeometryNodeBexWrangle")
    tree.links.new(gin.outputs[0], node.inputs[0])
    tree.links.new(node.outputs[0], gout.inputs[0])
    text = bpy.data.texts.new("test.bex")
    text.from_string(textwrap.dedent(src).strip("\n"))
    node.domain = domain
    node.script = text
    obj.modifiers.new("BEX", "NODES").node_group = tree
    return obj, node, text


def evaluate(obj):
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    return obj.evaluated_get(deps).data


def original_points(obj):
    return [tuple(v.co) for v in obj.data.vertices]


def close(a, b, eps=1e-4):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def test_position_and_channels():
    obj, node, text = make_setup('@P.z = sin(@P.x * chf("freq", 2.0)) * chf("amp", 0.5); @P.x += 1;')
    assert node.error == "", node.error
    assert [s.name for s in node.inputs if s.enabled and s.name] == ["Geometry", "freq", "amp"], [s.name for s in node.inputs]
    mesh = evaluate(obj)
    for (x, y, z), v in zip(original_points(obj), mesh.vertices):
        assert close(v.co, (x + 1, y, math.sin(x * 2.0) * 0.5)), (tuple(v.co), x, y)

    # Values set on the node survive a recompile that keeps the input.
    node.inputs["amp"].default_value = 2.0
    text.from_string('@P.z = sin(@P.x * chf("freq", 2.0)) * chf("amp", 0.5) + chf("lift", 1.0);')
    node.sync()
    assert node.inputs["amp"].default_value == 2.0
    mesh = evaluate(obj)
    for (x, y, z), v in zip(original_points(obj), mesh.vertices):
        assert close(v.co, (x, y, math.sin(x * 2.0) * 2.0 + 1.0)), tuple(v.co)


def test_attributes_and_sequencing():
    src = """
    f@a = @P.x              # new attribute
    @P = (0, 0, 0)          # later writes must not change what f@a saw
    v@old = @P + (0, 0, 1)  # reads the *new* P, like VEX
    i@n = @ptnum * 2
    if @ptnum % 2 == 0:
        f@even = 1
    else:
        f@even = -1
        odd = True
    s = 0
    for i in range(1, 4):
        s += i * @a
    f@sum = s
    i@half = @ptnum // 2
    f@third = @ptnum / 3
    """
    obj, node, _ = make_setup(src)
    assert node.error == "", node.error
    mesh = evaluate(obj)
    attrs = mesh.attributes
    for i, (x, y, z) in enumerate(original_points(obj)):
        assert close(mesh.vertices[i].co, (0, 0, 0))
        assert abs(attrs["a"].data[i].value - x) < 1e-5
        assert close(attrs["old"].data[i].vector, (0, 0, 1))
        assert attrs["n"].data[i].value == i * 2
        assert attrs["even"].data[i].value == (1 if i % 2 == 0 else -1)
        assert abs(attrs["sum"].data[i].value - 6 * x) < 1e-4
        assert attrs["half"].data[i].value == i // 2 and abs(attrs["third"].data[i].value - i / 3) < 1e-5
    assert attrs["n"].data_type == "INT" and attrs["old"].data_type == "FLOAT_VECTOR"


def test_swap_uses_original_values():
    obj, node, _ = make_setup("f@a = @P.x; f@b = @P.y;")
    # second wrangle after the first one would be the real case; emulate by chaining scripts
    node.script.from_string("f@a = @P.x; f@b = @P.y; t = @a; @a = @b; @b = t; @P.z = @a - @b;")
    node.sync()
    assert node.error == "", node.error
    mesh = evaluate(obj)
    for i, (x, y, z) in enumerate(original_points(obj)):
        assert abs(mesh.attributes["a"].data[i].value - y) < 1e-5
        assert abs(mesh.attributes["b"].data[i].value - x) < 1e-5
        assert abs(mesh.vertices[i].co.z - (y - x)) < 1e-5


def test_face_domain_and_functions():
    src = """
    f@area_ish = length(@P)
    v@n = normalize(@N) * fit(@P.x, -1, 1, 0, 2)
    f@r = rand(@primnum)
    f@c = clamp(@P.x * 10, -1, 1) + lerp(1, 3, 0.5) + noise(@P) * 0
    i@m = max(@primnum, 3)
    """
    obj, node, _ = make_setup(src, domain="FACE")
    assert node.error == "", node.error
    mesh = evaluate(obj)
    attrs = mesh.attributes
    assert attrs["area_ish"].domain == "FACE" and len(attrs["r"].data) == len(mesh.polygons)
    rs = [d.value for d in attrs["r"].data]
    assert all(0 <= r <= 1 for r in rs) and len(set(rs)) > 1
    for i, poly in enumerate(mesh.polygons):
        cx = poly.center.x
        assert close(attrs["n"].data[i].vector, (0, 0, cx + 1)), tuple(attrs["n"].data[i].vector)
        assert abs(attrs["c"].data[i].value - (max(-1, min(1, cx * 10)) + 2)) < 1e-4
        assert attrs["m"].data[i].value == max(i, 3)


def test_bbox_and_mapping():
    src = """
    v@r = relbbox(@P)
    v@lo = getbbox_min()
    v@sz = getbbox_size()
    f@w = wrap(@P.x * 3, 0, 1)
    v@f = fit(@P, -1, 1, (0, 0, 0), (2, 4, 6))
    @P.z = relbbox(@P).x
    """
    obj, node, _ = make_setup(src)
    assert node.error == "", node.error
    pts = original_points(obj)
    mesh = evaluate(obj)
    attrs = mesh.attributes
    for i, (x, y, z) in enumerate(pts):
        assert close(attrs["r"].data[i].vector, ((x + 1) / 2, (y + 1) / 2, 0)), tuple(attrs["r"].data[i].vector)
        assert close(attrs["lo"].data[i].vector, (-1, -1, 0)) and close(attrs["sz"].data[i].vector, (2, 2, 0))
        assert abs(attrs["w"].data[i].value - (x * 3 - math.floor(x * 3))) < 1e-4
        assert close(attrs["f"].data[i].vector, (x + 1, 2 * (y + 1), 3 * (z + 1)))
        assert close(mesh.vertices[i].co, (x, y, (x + 1) / 2))


def test_library_groups():
    from bex import library
    obj, node, _ = make_setup("v@c = getbbox_center(); v@s = getbbox_size(); v@r = relbbox(@P);")
    assert node.error == "", node.error
    inner = [n for n in node.node_tree.nodes if n.bl_idname == "GeometryNodeGroup"]
    # getbbox_* share one group node; relbbox is its own
    assert sorted(n.node_tree[library.KEY_PROP] for n in inner) == ["bbox", "relbbox"], inner
    bbox = library.find("bbox")
    assert bbox.use_fake_user and not bbox.name.startswith(".")
    # relbbox nests the bbox group; a second wrangle reuses the same datablocks
    assert any(n.bl_idname == "GeometryNodeGroup" and n.node_tree == bbox for n in library.find("relbbox").nodes)
    count = len(bpy.data.node_groups)
    obj2, node2, _ = make_setup("v@r = relbbox(@P);")
    assert len(bpy.data.node_groups) == count + 2  # only the new test tree and the wrangle's own group
    # renaming is fine; a version bump rebuilds in place and keeps users linked
    bbox.name = "My Box"
    bbox[library.VERSION_PROP] = -1
    assert library.ensure("bbox") == bbox and bbox[library.VERSION_PROP] == library.GROUPS["bbox"].version
    mesh = evaluate(obj)
    assert close(mesh.attributes["s"].data[0].vector, (2, 2, 0)) and close(mesh.attributes["c"].data[0].vector, (0, 0, 0))
    # used directly in a user tree, Position falls back to the real position
    tree = bpy.data.node_groups.new("User", "GeometryNodeTree")
    g = tree.nodes.new("GeometryNodeGroup")
    g.node_tree = library.ensure("relbbox")
    assert [s.name for s in g.inputs] == ["Geometry", "Position"] and g.inputs["Position"].hide_value
    assert hasattr(bpy.types, "NODE_MT_bex_library") and hasattr(bpy.ops.bex, "add_library_node")


def test_error_keeps_last_good_nodes():
    obj, node, text = make_setup("@P.z = 1;")
    count = len(node.node_tree.nodes)
    text.from_string("@P.z = 1;\n@P.y = oops(2);")
    node.sync()
    assert "line 2" in node.error and "oops" in node.error, node.error
    assert len(node.node_tree.nodes) == count
    assert close(evaluate(obj).vertices[0].co, (-1, -1, 1))
    text.from_string("@P.z = 2;")
    node.sync()
    assert node.error == ""
    assert close(evaluate(obj).vertices[0].co, (-1, -1, 2))


def test_sync_all_and_copy():
    from bex import node as bex_node

    obj, node, text = make_setup("@P.z = 1;")
    text.from_string("@P.z = 3;")
    bex_node.sync_all()  # what the undo handler / Update All do
    assert close(evaluate(obj).vertices[0].co, (-1, -1, 3))
    tree = node.id_data
    dup = tree.nodes.new("GeometryNodeBexWrangle")
    assert dup.node_tree is not node.node_tree


def test_new_node_gets_its_own_script():
    tree = bpy.data.node_groups.new("Fresh", "GeometryNodeTree")
    a = tree.nodes.new("GeometryNodeBexWrangle")
    b = tree.nodes.new("GeometryNodeBexWrangle")
    assert a.script and b.script and a.script != b.script
    assert "chf(" in a.script.as_string()
    assert a.error == "", a.error
    assert any(n.bl_idname == "GeometryNodeSetPosition" for n in a.node_tree.nodes)
    # Its script is hidden from Text pickers (leading dot) and has no fake user,
    # so Blender drops it on save once no node uses it.
    from bex import node as bex_node
    assert a.script.name.startswith(".BEX Script") and not a.script.use_fake_user
    assert a.draw_label() == "BEX Wrangle"
    c = tree.nodes.new("GeometryNodeBexWrangle")
    bex_node.BexWrangleNode.copy(c, a)  # what duplicating calls
    assert c.script != a.script and c.script.name.startswith(".BEX Script") and not c.script.use_fake_user


def test_presets():
    import tempfile
    from bex import presets

    builtins = presets.builtin_presets()
    assert {"Displace", "Ripple"} <= {p.stem for p in builtins}
    tree = bpy.data.node_groups.new("Presets", "GeometryNodeTree")
    node = tree.nodes.new("GeometryNodeBexWrangle")
    for path in builtins:  # every built-in preset compiles
        presets.load_into(node, path)
        assert node.error == "" and node.is_up_to_date(), (path.stem, node.error)
    # User presets go to their own folder (redirected here) and show in the menu list.
    folder = Path(tempfile.mkdtemp())
    real_user_dir = presets.user_dir
    presets.user_dir = lambda create=False: folder
    try:
        node.script.from_string("@P.z = 7")
        path = presets.save(' my/wave? ', node.script.as_string())
        assert path == folder / "mywave.bex" and path.read_text() == "@P.z = 7"
        assert [p.stem for p in presets.user_presets()] == ["mywave"]
        presets.save("mywave", "@P.z = 8")  # same name replaces it
        assert path.read_text() == "@P.z = 8" and len(presets.user_presets()) == 1
        presets.load_into(node, path)
        assert node.script.as_string() == "@P.z = 8" and node.is_up_to_date()
    finally:
        presets.user_dir = real_user_dir


def test_pending_line_keeps_inputs_steady():
    obj, node, text = make_setup('@P.z = chf("amp", 0.5);')
    node.inputs["amp"].default_value = 3.0
    # Typing a new channel on line 2: no socket yet, and "amp" (renamed away on
    # the edited line) is not removed meanwhile. The default is used as a constant.
    text.from_string('@P.z = chf("am", 0.5);\n@P.x = chf("of", 7);')
    node.sync(pending_line=1)
    names = [s.name for s in node.channels()]
    assert "am" not in names and "amp" in names and "of" in names, names
    assert close(evaluate(obj).vertices[0].co, (7, -1, 0.5))
    # Leaving the line finishes it: "am" appears, "amp" goes away.
    node.sync()
    assert [s.name for s in node.channels()] == ["am", "of"]
    # Rename back: the value set on "amp" earlier comes back.
    text.from_string('@P.z = chf("amp", 0.5);')
    node.sync()
    assert node.inputs["amp"].default_value == 3.0
    assert all(i.hide_value for i in node.node_tree.interface.items_tree
               if i.item_type == "SOCKET" and i.in_out == "INPUT" and i.socket_type != "NodeSocketGeometry")


def test_code_editor_compiles_on_focus_loss():
    import types
    from bex import code_editor as ce

    obj, node, text = make_setup("@P.z = 1;")
    ce._session = ce.Session(node)
    op = types.SimpleNamespace()
    for name in ("_key", "_after_change", "_end"):
        setattr(op, name, getattr(ce.BEX_OT_code_edit, name).__get__(op))
    ctx = bpy.context

    def press(key, ch=""):
        ev = types.SimpleNamespace(type=key, unicode=ch, ctrl=False, shift=False, alt=False, oskey=False)
        return op._key(ctx, node, ev)

    def type_text(s):
        for ch in s:
            press(ch.upper() if ch.isalpha() else ch, ch)

    ce._session.buf.move_doc_end()
    press("RET")
    type_text('lift = ch')
    assert ce._session.suggestion == "b"  # shortest language name first
    type_text('f("lift", 2)')
    press("RET")
    type_text("@P.x = li")
    assert ce._session.suggestion == "ft"  # the script's own variable
    press("TAB")  # accepts the inline completion
    assert text.as_string() == '@P.z = 1;\nlift = chf("lift", 2)\n@P.x = lift'
    # Nothing is evaluated while focused...
    assert node.channels() == [] and not node.is_up_to_date()
    # ...losing focus compiles.
    assert press("ESC") == {"FINISHED"} and ce._session is None
    assert [s.name for s in node.channels()] == ["lift"] and node.is_up_to_date()
    assert close(evaluate(obj).vertices[0].co, (2, -1, 1))


def test_code_editor_undo_survives_refocus():
    import types
    from bex import code_editor as ce

    obj, node, text = make_setup("@P.z = 1")
    op = types.SimpleNamespace()
    for name in ("_key", "_after_change", "_end"):
        setattr(op, name, getattr(ce.BEX_OT_code_edit, name).__get__(op))
    ctx = bpy.context

    def press(key, ch="", **mods):
        ev = types.SimpleNamespace(type=key, unicode=ch, ctrl=False, shift=False, alt=False, oskey=False)
        for k, v in mods.items():
            setattr(ev, k, v)
        return op._key(ctx, node, ev)

    undo = {"oskey": True} if ce.MAC else {"ctrl": True}
    ce._session = ce.Session(node)
    ce._session.buf.move_doc_end()
    press("RET")
    for ch in "@P = 1":
        press(ch.upper() if ch.isalpha() else ch, ch)
    press("ESC")
    # Refocus: the history is still there, and undo is by word, not by character.
    ce._session = ce.Session(node)
    press("Z", **undo)
    assert text.as_string() == "@P.z = 1\n@P", repr(text.as_string())
    press("Z", **undo)
    press("Z", **undo)
    assert text.as_string() == "@P.z = 1"
    press("Z", shift=True, **undo)
    assert text.as_string() == "@P.z = 1\n"
    press("ESC")
    # A change from outside (Text Editor) becomes one more step.
    text.from_string("@P.z = 2")
    ce._session = ce.Session(node)
    press("Z", **undo)
    assert text.as_string() == "@P.z = 1\n"
    press("ESC")
    # Each script has its own history.
    other = make_setup("@P.z = 5")[1]
    ce._session = ce.Session(other)
    assert not ce._session.buf.undo()
    ce._session = None
    # Loading a file forgets it.
    ce.forget_buffers()
    assert not ce.buffer_for(text).undo()


def test_code_box_scrolls():
    import types
    from bex import code_editor as ce

    obj, node, text = make_setup("\n".join(f"@P.z = {i}" for i in range(40)))
    def wheel(kind, **mods):
        return types.SimpleNamespace(**{"type": kind, "ctrl": False, "oskey": False, "alt": False, "shift": False, **mods})
    assert ce.visible_rows(40) == ce.MAX_ROWS < 40  # the box stops growing
    view = ce.view_for(node)
    assert ce.scroll_code(node, None, wheel("WHEELDOWNMOUSE")) and view.top == 3
    for _ in range(20):
        ce.scroll_code(node, None, wheel("WHEELDOWNMOUSE"))
    assert view.top == 40 - ce.MAX_ROWS  # clamped at the last line
    assert not ce.scroll_code(node, None, wheel("WHEELUPMOUSE", ctrl=True))  # Ctrl+wheel stays the editor's
    # The scroll is kept when editing starts.
    ce._session = ce.Session(node)
    assert ce._session.view is view
    ce._session = None
    # A short script has nothing to scroll: the wheel goes to the Node Editor.
    short = make_setup("@P.z = 1")[1]
    assert not ce.scroll_code(short, None, wheel("WHEELDOWNMOUSE"))


def test_channels_are_properties():
    obj, node, text = make_setup('@P.z = chf("amp", 0.5)')
    sock = node.inputs["amp"]
    assert sock.hide  # a property on the node, not an input socket
    sock.default_value = 2.0
    assert close(evaluate(obj).vertices[0].co, (-1, -1, 2))
    # A link (made before, or after Ctrl+H) keeps the socket visible and drives the value...
    tree = node.id_data
    value = tree.nodes.new("ShaderNodeValue")
    value.outputs[0].default_value = 3.0
    link = tree.links.new(value.outputs[0], sock)
    node.update()
    assert not sock.hide and close(evaluate(obj).vertices[0].co, (-1, -1, 3))
    # ...and unlinking makes it a property again.
    tree.links.remove(link)
    node.update()
    assert sock.hide
    # Recompiling keeps it hidden, and a new channel starts hidden.
    text.from_string('@P.z = chf("amp", 0.5) + chf("lift", 1)')
    node.sync()
    assert node.inputs["amp"].hide and node.inputs["lift"].hide


def test_group():
    def attr(mesh, name):
        return [d.value for d in mesh.attributes[name].data]

    # Several writes: the group is measured once on the incoming geometry, so
    # moving @P first does not change which points the later write picks.
    obj, node, text = make_setup("@P.y = 5\nf@mark = 1")
    node.group = "@P.y > 0"
    mesh = evaluate(obj)
    assert node.error == "", node.error
    before = original_points(obj)
    for (x, y, z), v, m in zip(before, mesh.vertices, attr(mesh, "mark")):
        inside = y > 0
        assert close(v.co, (x, 5 if inside else y, z)) and m == (1.0 if inside else 0.0), (x, y, m)
    # Frames label the group's nodes.
    assert any(n.bl_idname == "NodeFrame" and n.label == "Group: @P.y > 0" for n in node.node_tree.nodes)

    # A bool attribute as the group, in all three spellings, written by an upstream wrangle.
    tree = node.id_data
    first = tree.nodes.new("GeometryNodeBexWrangle")
    first.script.from_string("@group_top = @P.x > 0.5")
    first.sync(force=True)
    gin = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
    tree.links.new(gin.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], node.inputs[0])
    text.from_string("f@mark = 1")
    expected = [1.0 if x > 0.5 else 0.0 for x, _y, _z in before]
    for spelling in ("top", "b@top", "@group_top"):
        node.group = spelling
        assert node.error == "" and attr(evaluate(obj), "mark") == expected, spelling

    # A group that doesn't make sense selects nothing, with a warning instead of an error.
    node.group = "@P.4 > 0"
    assert node.error == "" and node.warning.startswith("group:") and "selects nothing" in node.warning
    assert attr(evaluate(obj), "mark") == [0.0] * len(before)
    node.group = ""
    assert node.error == "" and node.warning == "" and attr(evaluate(obj), "mark") == [1.0] * len(before)


def test_group_with_float_attribute_and_constant_writes():
    # A float attribute compared in the Group is read as a float.
    obj, node, text = make_setup("f@mask = 0.3\n")
    tree = node.id_data
    first = tree.nodes.new("GeometryNodeBexWrangle")
    first.script.from_string("f@mask = 0.3\nb@top = 1")
    first.sync(force=True)
    gin = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
    tree.links.new(gin.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], node.inputs[0])
    text.from_string("@P.z = 5")
    node.group = "@mask > 0.5"
    assert node.error == "" and node.warning == "", node.warning
    assert all(abs(v.co.z) < 1e-6 for v in evaluate(obj).vertices)  # 0.3 is not > 0.5: nothing moves
    # Only constant writes: the group is still measured before the first write.
    text.from_string("b@top = False\nf@x = 5")
    node.group = "top"
    assert any(n.bl_idname == "GeometryNodeCaptureAttribute" for n in node.node_tree.nodes)
    assert all(d.value == 5.0 for d in evaluate(obj).attributes["x"].data)


def test_group_patterns():
    obj, node, text = make_setup("f@mark = 1")
    tree = node.id_data
    first = tree.nodes.new("GeometryNodeBexWrangle")  # writes the group "top"
    first.script.from_string("@group_top = @P.x > 0.5")
    first.sync(force=True)
    gin = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
    tree.links.new(gin.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], node.inputs[0])
    pts = original_points(obj)
    count = len(pts)
    top = {i for i, (x, _y, _z) in enumerate(pts) if x > 0.5}
    every = set(range(count))
    cases = {
        "*": every,
        "3": {3},
        "2-5": {2, 3, 4, 5},
        "5-2": {2, 3, 4, 5},
        "0-10:3": {0, 3, 6, 9},
        "1 7 top": {1, 7} | top,
        "!top": every - top,
        "* ^top": every - top,
        "0-9 ^top": set(range(10)) - top,
        "^top": set(),
        "@P.y>0 4": {i for i, (_x, y, _z) in enumerate(pts) if y > 0} | {4},
        "@P.y > 0 and top": {i for i, (_x, y, _z) in enumerate(pts) if y > 0} & top,  # an expression
    }
    for pattern, chosen in cases.items():
        node.group = pattern
        assert node.error == "", (pattern, node.error)
        marks = [d.value for d in evaluate(obj).attributes["mark"].data]
        got = {i for i, m in enumerate(marks) if m == 1.0}
        assert got == chosen, (pattern, sorted(got), sorted(chosen))


def test_incoming_attributes():
    from bex import upstream
    upstream.CACHE_SECONDS = 0
    obj, node, text = make_setup("@P.z = 0")
    obj.data.attributes.new("weight", "FLOAT", "POINT")
    tree = node.id_data
    gin = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
    got = dict(upstream.incoming_attributes(node))
    assert got.get("weight") == "float" and "position" not in got, got

    # Store Named Attribute and an upstream wrangle add names; Remove takes one away.
    store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type = "FLOAT_VECTOR"
    store.inputs["Name"].default_value = "vel"
    first = tree.nodes.new("GeometryNodeBexWrangle")
    first.script.from_string("i@count = 3")
    first.sync(force=True)
    remove = tree.nodes.new("GeometryNodeRemoveAttribute")
    remove.inputs["Name"].default_value = "weight"
    tree.links.new(gin.outputs[0], store.inputs["Geometry"])
    tree.links.new(store.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], remove.inputs["Geometry"])
    tree.links.new(remove.outputs[0], node.inputs[0])
    got = dict(upstream.incoming_attributes(node))
    assert got.get("vel") == "vector" and got.get("count") == "int" and "weight" not in got, got

    # Through a group node: the group's own Store Named Attribute, and its Group Input
    # maps back to the outer link.
    inner = bpy.data.node_groups.new("Inner", "GeometryNodeTree")
    inner.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    inner.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    i_in, i_out = inner.nodes.new("NodeGroupInput"), inner.nodes.new("NodeGroupOutput")
    i_store = inner.nodes.new("GeometryNodeStoreNamedAttribute")
    i_store.inputs["Name"].default_value = "inside"
    inner.links.new(i_in.outputs[0], i_store.inputs["Geometry"])
    inner.links.new(i_store.outputs[0], i_out.inputs[0])
    group = tree.nodes.new("GeometryNodeGroup")
    group.node_tree = inner
    tree.links.new(remove.outputs[0], group.inputs[0])
    tree.links.new(group.outputs[0], node.inputs[0])
    got = dict(upstream.incoming_attributes(node))
    assert got.get("inside") == "float" and got.get("vel") == "vector" and "weight" not in got, got

    from bex import code_editor
    assert code_editor.info_lines(node)[0][0] == "input"


def test_layout_has_no_overlaps():
    from bex import builder

    src = (Path(__file__).resolve().parent.parent / "examples" / "ripple.bex").read_text()
    obj, node, _ = make_setup(src)
    assert node.error == "", node.error
    tree = node.node_tree
    frames = [n for n in tree.nodes if n.bl_idname == "NodeFrame"]
    assert "@P.z += wave * amp" in [f.label for f in frames], [f.label for f in frames]
    rects = []
    for n in tree.nodes:
        if n.bl_idname != "NodeFrame":
            x, y = n.location_absolute
            rects.append((n.name, x, y - builder.estimate_height(n), x + n.width, y))
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not (a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]), (a, b)
    # Each line that reads inputs has its own Group Input inside its frame,
    # showing only the inputs it uses.
    group_inputs = [n for n in tree.nodes if n.bl_idname == "NodeGroupInput"]
    assert len(group_inputs) > 1
    for link in tree.links:
        if link.from_node.bl_idname == "NodeGroupInput":
            assert link.from_node.parent == link.to_node.parent, (link.from_node.name, link.to_node.name)
    for gi in group_inputs:
        if gi.parent:
            shown = [s.name for s in gi.outputs if not s.hide]
            assert shown and "Geometry" not in shown, shown
    # Links flow left to right.
    for link in tree.links:
        assert link.from_node.location_absolute.x < link.to_node.location_absolute.x, (link.from_node.name, link.to_node.name)


def test_write_plan():
    def kinds(node):
        return [n.bl_idname for n in node.node_tree.nodes]

    def attr(mesh, name):
        return [d.value for d in mesh.attributes[name].data]

    obj, node, text = make_setup("f@m = @P.x")
    points = original_points(obj)
    # A conditional write keeps the old values where the condition is false.
    text.from_string("f@m = @P.x\nif @P.y > 0:\n    f@m = 7")
    node.sync()
    assert node.error == "", node.error
    assert attr(evaluate(obj), "m") == [pytest_approx(7.0 if y > 0 else x) for x, y, z in points]
    # Upstream value on another domain: an earlier wrangle writes m per face.
    tree = node.id_data
    first = tree.nodes.new("GeometryNodeBexWrangle")
    first.domain = "FACE"
    first.script.from_string("f@m = 3")
    first.sync(force=True)
    gin = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
    tree.links.new(gin.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], node.inputs[0])
    text.from_string("if @P.y > 0:\n    f@m = 7")
    node.sync()
    mesh = evaluate(obj)
    assert mesh.attributes["m"].domain == "POINT"
    assert attr(mesh, "m") == [pytest_approx(7.0 if y > 0 else 3.0) for x, y, z in points]
    tree.links.new(gin.outputs[0], node.inputs[0])
    tree.nodes.remove(first)
    # @P += x is one Set Position with Offset; under an if it uses Selection.
    text.from_string("if @P.x > 0:\n    @P.z += 2")
    node.sync()
    setpos = next(n for n in node.node_tree.nodes if n.bl_idname == "GeometryNodeSetPosition")
    assert "GeometryNodeSwitch" not in kinds(node) and not setpos.inputs["Position"].is_linked
    assert tuple(setpos.inputs["Offset"].default_value) == (0, 0, 2) and setpos.inputs["Selection"].is_linked
    mesh = evaluate(obj)
    for v, (x, y, z) in zip(mesh.vertices, points):
        assert close(v.co, (x, y, z + (2 if x > 0 else 0))), (tuple(v.co), x)
    # Independent writes: no Capture. A value read before its attribute changed: captured.
    text.from_string("f@a = @P.x\n@P.z = 1\nf@b = @P.y")
    node.sync()
    assert "GeometryNodeCaptureAttribute" not in kinds(node)
    mesh = evaluate(obj)
    assert attr(mesh, "a") == [pytest_approx(x) for x, y, z in points]
    assert attr(mesh, "b") == [pytest_approx(y) for x, y, z in points]
    # A value read before its attribute changed is captured: @a comes from an earlier wrangle.
    first = tree.nodes.new("GeometryNodeBexWrangle")
    first.script.from_string("f@a = @P.x")
    first.sync(force=True)
    tree.links.new(gin.outputs[0], first.inputs[0])
    tree.links.new(first.outputs[0], node.inputs[0])
    text.from_string("t = @a\nf@a = 5\nf@c = t")
    node.sync()
    assert "GeometryNodeCaptureAttribute" in kinds(node)
    mesh = evaluate(obj)
    assert attr(mesh, "c") == [pytest_approx(x) for x, y, z in points] and set(attr(mesh, "a")) == {5.0}

def test_other_inputs():
    obj, node, text = make_setup("f@m = 1")
    points = original_points(obj)
    # Input 1: the same grid, moved up by 3 and with an attribute.
    other = bpy.data.objects.new("Other", obj.data.copy())
    bpy.context.collection.objects.link(other)
    other.location.z = 3
    for i, v in enumerate(other.data.vertices):
        v.co.z = i * 0.5
    other.data.attributes.new("w", "FLOAT", "POINT").data.foreach_set("value", [i * 2.0 for i in range(len(points))])
    text.from_string('@P.z = point(1, "P", @ptnum).z\nf@w = @opinput1_w\ni@n = npoints(1)\n'
                     'i@near = nearpoint(1, @P + (0, 0, 100))\nv@top = getbbox_max(1)')
    node.sync()
    assert node.error == "", node.error
    assert [s.name for s in node.inputs if s.type == "GEOMETRY"] == ["Geometry", "Input 1"]
    tree = node.id_data
    info = tree.nodes.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = other
    tree.links.new(info.outputs["Geometry"], node.inputs["Input 1"])
    mesh = evaluate(obj)
    count = len(points)
    for i, v in enumerate(mesh.vertices):
        assert abs(v.co.z - i * 0.5) < 1e-4  # Object Info is in local space (Original)
    assert [d.value for d in mesh.attributes["w"].data] == [i * 2.0 for i in range(count)]
    assert {d.value for d in mesh.attributes["n"].data} == {count}
    assert {d.value for d in mesh.attributes["near"].data} == {count - 1}  # the highest point
    assert all(close(d.vector, (1, 1, (count - 1) * 0.5)) for d in mesh.attributes["top"].data)
    # The socket goes away when the script stops using it.
    text.from_string("f@m = 1")
    node.sync()
    assert [s.name for s in node.inputs if s.type == "GEOMETRY"] == ["Geometry"]


class pytest_approx(float):
    def __eq__(self, other):
        return abs(self - other) < 1e-4

    __hash__ = float.__hash__


failed = 0
for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
    try:
        fn()
        print("ok", name)
    except Exception:
        failed += 1
        print("FAIL", name)
        traceback.print_exc()
print("BEX TESTS:", "FAILED" if failed else "PASSED")
sys.exit(1 if failed else 0)
