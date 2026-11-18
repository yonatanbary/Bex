"""Pure-Python tests for the BEX front end.  Run: python3 tests/test_lang.py"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bex"))

from lang import BexError, compile_source  # noqa: E402


def writes(src, domain="POINT"):
    return {name: val for name, _type, val in compile_source(src, domain).writes}


def fails(src, fragment, line=None, domain="POINT"):
    try:
        compile_source(src, domain)
    except BexError as e:
        assert fragment in e.msg, f"expected {fragment!r} in {e.msg!r}"
        assert line is None or e.line == line, f"expected line {line}, got {e.line}: {e.msg}"
        return
    raise AssertionError(f"expected an error containing {fragment!r}")


def test_constant_folding():
    w = writes("f@a = 1 + 2 * 3\nf@b = 7 / 2\nf@c = 7 // 2\nf@d = sin(0) + max(2, 5)\nv@e = (1, 2, 3) * 2\n"
               "f@f = 2 ** 3 ** 2\nf@g = -7 // 2\nf@h = -2 ** 2\nf@i = 7.5 // 2")
    assert w["a"].const == 7.0
    assert w["b"].const == 3.5  # `/` is true division, as in Python
    assert w["c"].const == 3.0 and w["g"].const == -4.0 and w["i"].const == 3.0
    assert w["d"].const == 5.0
    assert w["e"].const == (2.0, 4.0, 6.0)
    assert w["f"].const == 512.0 and w["h"].const == -4.0  # ** is right-associative and binds tighter than -


def test_python_syntax():
    src = """
# a comment
amp = chf("amp", 0.5)   # trailing comment
if @P.z > 0:
    @a = 1
elif @P.z < -1:
    @a = 2
else:
    @a = 3
    @b = 4
if True: @c = 5
@d = 1 if @P.x > 0 else 2; @e = (1 if False else 2)
@f = 0 < 1 <= 1 and not False or False
@g = (1 +
      2)
v@h = [1, 2, 3]
f@k = h[-1]
"""
    w = writes(src.replace("h[-1]", "v@h[-1]"))
    assert w["a"].node.kind == "switch" and w["a"].node.inputs[1].node.kind == "switch"  # elif nests
    assert w["c"].const == 5.0 and w["d"].node.kind == "switch" and w["e"].const == 2.0
    assert w["f"].const == 1.0 and w["g"].const == 3.0 and w["k"].const == 3.0
    c = compile_source("x = 0\nfor i in range(1, 10, 3):\n    x += i\n    pass\nwhile x < 100:\n    x *= 2\n@a = x")
    assert c.writes[0][2].const == 192.0  # 1+4+7 = 12 -> 24 -> 48 -> 96 -> 192


def test_attribute_types():
    p = compile_source("@P.y += 1; i@n = 3; @mask = 0.5; v@dir = @N")
    assert [(n, t) for n, t, _ in p.writes] == [("P", "vector"), ("n", "int"), ("mask", "float"), ("dir", "vector")]
    assert p.writes[0][2].node.kind == "combine"


def test_sequential_semantics():
    w = writes("t = @a\n@a = @b\n@b = t")
    assert w["a"].node.params["name"] == "b"
    assert w["b"].node.params["name"] == "a"
    assert writes("@P = @P") == {}


def test_if_becomes_switch():
    w = writes("if @P.z > 0:\n    @a = 1\nelse:\n    @a = 2\n    @b = 3")
    assert w["a"].node.kind == "switch" and w["b"].node.kind == "switch"
    cond, false, true = w["b"].node.inputs
    assert cond.node.kind == "compare" and false.const == 3.0 and true.node.kind == "attr"
    # constant conditions pick a branch at compile time
    assert writes("if 1 < 2: @a = 5\nelse: @a = 6")["a"].const == 5.0


def test_variables_like_python():
    # no declarations; variables set inside an if survive it and are merged
    w = writes("x = 1\nif @P.x > 0:\n    y = 2\n    x = y\n@a = x")
    assert w["a"].node.kind == "switch"
    w = writes("if @P.x > 0:\n    y = 1\nelse:\n    y = 2\n@a = y")
    assert w["a"].node.kind == "switch"
    fails("if @P.x > 0:\n    y = 2\n@a = y", "only set in one branch", 3)
    # the type follows the value; an annotation converts
    w = writes("x = 1\nx = x / 2\n@a = x\nn: int = 2.7\ni@n = n\nv: vector = 1\nv@v = v")
    assert w["a"].const == 0.5 and w["n"].const == 2 and w["v"].const == (1.0, 1.0, 1.0)
    fails("x += 1", "unknown variable 'x'")


def test_for_unrolls():
    w = writes("s = 0\nfor i in range(4):\n    s += i\n@a = s")
    assert w["a"].const == 6.0
    fails("for i in range(@ptnum):\n    pass", "compile time")
    fails("for i in range(100000):\n    pass", "more than")
    fails("while True:\n    pass", "more than")
    fails("while @P.x > 0:\n    pass", "compile time")


def test_channels():
    p = compile_source('@P *= chf("scale", 2)\n@P += chv("offset")\ni@k = chi("count", 3) + chi("count")')
    assert p.params == [("scale", "float", 2.0), ("offset", "vector", (0.0, 0.0, 0.0)), ("count", "int", 3)]
    fails('@a = chf("x") + chi("x")', "already used")
    fails("@a = chf(1)", "name in quotes")


def test_cse():
    w = writes("@a = sin(@P.x) + sin(@P.x)")
    a, b = w["a"].node.inputs
    assert a.node is b.node


def test_functions():
    w = writes("@a = fit01(noise(@P * 3), -1, 1)\n@b = length(@P)\nv@c = normalize(cross(@N, (0, 0, 1)))\n@d = rand(@ptnum)")
    assert w["a"].node.kind == "maprange" and w["b"].node.params["op"] == "LENGTH"
    assert w["c"].node.params["op"] == "NORMALIZE" and w["d"].node.kind == "white_noise"
    assert abs(writes("@a = PI")["a"].const - math.pi) < 1e-9 and abs(writes("@a = tau")["a"].const - math.tau) < 1e-9
    assert writes("i@a = int(3.9)")["a"].const == 3
    w = writes("@a = @P.x // 2\ni@b = @ptnum // 2\n@c = @P.x ** 2\ni@d = @ptnum ** 2")
    assert w["a"].node.params["op"] == "FLOOR" and w["b"].node.params["op"] == "DIVIDE_FLOOR"
    assert w["c"].node.params["op"] == "POWER" and w["d"].node.kind == "imath"
    assert writes("@a = @ptnum / 2")["a"].node.kind == "math"  # true division, even for ints


def test_bbox_and_mapping():
    w = writes("v@lo = getbbox_min(); v@hi = getbbox_max(0); v@r = relbbox(0, @P); f@h = relbbox(@P).z;"
               "v@c = getbbox_center(); f@s = getbbox_size()[1]")
    assert w["lo"].node.kind == "group" and w["lo"].node.params["name"] == "bbox"
    assert w["lo"].out == 0 and w["hi"].out == 1 and w["c"].out == 3
    assert w["lo"].node is w["hi"].node is w["c"].node  # one group node, shared
    assert w["r"].node.params["name"] == "relbbox" and w["h"].node.kind == "separate"
    assert w["s"].node.inputs[0].out == 2 and w["s"].out == 1
    fails("v@a = getbbox_min(1)", "only be 0")
    fails("v@a = relbbox()", "takes 1 or 2")
    # folding and vector support
    w = writes("@a = fit(5, 0, 10, 0, 1); @b = smooth(0, 1, 0.5); @c = wrap(5.5, 0, 2); @d = snap(7, 3);"
               "@e = pingpong(3, 2); v@f = fit((0, 5, 10), 0, 10, (0, 0, 0), (1, 2, 3)); @g = smootherstep(0, 1, 2)")
    assert w["a"].const == 0.5 and w["b"].const == 0.5 and w["c"].const == 1.5 and w["d"].const == 6.0
    assert w["e"].const == 1.0 and w["f"].const == (0.0, 1.0, 3.0) and w["g"].const == 1.0
    w = writes("v@f = fit(@P, -1, 1, 0, 1); v@w = wrap(@P, -1, 1); @s = smoothstep(0, 1, @P.x)")
    assert w["f"].node.params["dtype"] == "vector" and w["w"].node.params["op"] == "WRAP"
    assert w["s"].node.params["interp"] == "SMOOTHSTEP"


def test_errors():
    fails("@a = 1 2", "end of the line", 1)
    fails("@a = 1\n@b = foo(2)", "unknown function", 2)
    fails("@a = @P", "vector as float")
    fails("@N = (0, 0, 1)", "read-only")
    fails("@P = (0, 0, 1)", "Points", domain="FACE")
    fails("v@a = 1; f@a = 2", "cannot be used as float")
    fails("@a = 1 $ 2", "unexpected character")
    fails("@a = sin(1, 2)", "takes 1")
    fails("float x = 1", "no declarations needed")
    fails("@a = 1 && 2", "use 'and'")
    fails("@a = !b", "use 'not'")
    fails("@a = b ? 1 : 2", "a if condition else b")
    fails("if @P.x > 0 {", "indented after a ':'")
    fails("if @P.x > 0:\n@a = 1", "indented block", 2)
    fails("if @P.x > 0:\n        @a = 1\n    @b = 2", "indentation does not match", 3)
    fails("    @a = 1", "unexpected indentation")
    fails("else:\n    @a = 1", "without an 'if'")
    fails("@a = true", "True")
    fails("v@a = (1, 2)", "three components")
    fails("@a = range(3)", "for-loop")
    fails("for i in (1, 2, 3):\n    pass", "range(")
    fails("def f():\n    pass", "not supported")
    fails("@a = (1 +\n2", "never closed", 1)


def test_reads_lists_incoming_attributes():
    prog = compile_source("@P += @N * f@mask\nf@mask = 2\n@Cd = @mask")
    # The later @mask reads the script's own write, so it is listed once.
    assert sorted(prog.reads) == [("N", "vector"), ("P", "vector"), ("mask", "float")]
    assert [w[0] for w in prog.writes] == ["P", "mask", "Cd"]


def test_pending_channel_line():
    src = 'f@a = chf("amp", 0.5)\nf@b = chf("fr", 2.0)'
    # The name being typed on line 2 is held back and used as its default.
    prog = compile_source(src, pending_line=2)
    assert [p[0] for p in prog.params] == ["amp"] and prog.pending == ["fr"]
    assert {n: v for n, _, v in prog.writes}["b"].const == 2.0
    # A name that already is an input stays an input while its line is edited.
    prog = compile_source(src, pending_line=2, known_params={"fr"})
    assert [p[0] for p in prog.params] == ["amp", "fr"] and prog.pending == []
    # Used again on a finished line: it is a real input.
    prog = compile_source(src + '\nf@c = chf("fr")', pending_line=2)
    assert [p[0] for p in prog.params] == ["amp", "fr"]


def test_numeric_components():
    # .0 .1 .2 pick components like .x .y .z and [0] [1] [2].
    p = compile_source("v@a = (1, 2, 3)\nf@b = @a.1 + v@a.2 * 10\nf@c = (4, 5, 6).0\n@P.1 = 7\nf@d = .5")
    w = {n: v for n, _t, v in p.writes}
    assert w["b"].const == 32.0 and w["c"].const == 4.0 and w["d"].const == 0.5
    assert w["P"].node.kind == "combine"
    fails("f@x = @P.3", "component", line=1)
    fails("f@x = @P .1", "expected the end of the line")  # with a space it's a number, as in Python


def test_group():
    # No group, or an always-true one: every element.
    assert compile_source("@P.z = 1").group is None
    assert compile_source("@P.z = 1", group="  ").group is None
    assert compile_source("@P.z = 1", group="1 < 2").group is None
    p = compile_source("@P.z = 1", group="@P.y > 0")
    assert p.group.type == "bool" and not p.group.is_const and p.group.node.kind == "compare"
    assert p.group_source == "@P.y > 0"
    # A bare name, b@name and @group_name are the same bool attribute.
    for spelling in ("top", "b@top", "@group_top"):
        g = compile_source("@P.z = 1", group=spelling).group
        assert g.node.kind == "attr" and g.node.params["name"] == "top" and g.node.params["dtype"] == "bool", spelling
    p = compile_source("@P.z = 1", group="top and not @P.x < 0")
    assert ("top", "bool") in p.reads and ("P", "vector") in p.reads
    # The group reads the incoming geometry, before the script's own writes.
    p = compile_source("@P.y = -5", group="@P.y > 0")
    assert p.group.node.inputs[0].node.kind != "combine"
    # A group that doesn't make sense selects nothing and warns; it never stops the build.
    for src, fragment in (("@P.y >", "group:"), ("@P.y > 0 0", "end of the expression"),
                          ("v@top", "as bool"), ("@P.4", "component")):
        p = compile_source("b@top = 1", group=src)
        assert p.group.is_const and p.group.const is False, src
        assert len(p.warnings) == 1 and fragment in p.warnings[0] and "selects nothing" in p.warnings[0], p.warnings
        assert [n for n, _t, _v in p.writes] == ["top"]  # the script still compiled
    assert compile_source("b@top = 1", group="@P.y > 0").warnings == []


def test_group_patterns():
    def group(text):
        return compile_source("f@mark = 1", group=text).group

    assert group("*") is None                      # every element
    assert group("* ^top").node.params["op"] == "NOT"  # all minus top folds to `not top`
    assert group("0-9 ^top").node.params["op"] == "AND"
    assert group("^top").const is False            # removing from nothing leaves nothing
    assert group("!top").node.params["op"] == "NOT"
    assert group("0-9 top").node.params["op"] == "OR"
    # Element numbers compare the index; a number alone is one element.
    g = group("5")
    assert g.node.kind == "compare" and g.node.inputs[0].node.params["name"] == "index"
    # Ad-hoc expressions without spaces mix with the rest; @name is a group (bool).
    p = compile_source("f@mark = 1", group="@P.y>0 @sel 0-3")
    assert ("sel", "bool") in p.reads and ("P", "vector") in p.reads
    # Not a pattern: spaces inside an expression, keywords, negative numbers.
    for text in ("@P.y > 0", "top and not bottom", "@ptnum < -1 or top"):
        assert compile_source("f@mark = 1", group=text).group is not None, text
    p = compile_source("f@mark = 1", group="0-9 @P.y>")
    assert p.group.const is False and p.warnings  # a bad word: nothing, with a warning


def test_group_types():
    # A name used as a whole condition is a group (bool); inside a comparison it
    # keeps its usual type, and nothing the Group guessed binds the script.
    p = compile_source("f@mask = 2.0", group="@mask > 0.5")
    assert p.group.node.inputs[0].node.params["dtype"] == "float"
    assert [(n, t) for n, t, _ in p.writes] == [("mask", "float")]
    assert compile_source("f@top = 1", group="top").writes[0][1] == "float"
    for text in ("ptnum < 5", "P.y > 0", "mask > 0.5 and top"):
        p = compile_source("f@x = 1", group=text)
        assert not p.warnings and not p.group.is_const, (text, p.warnings)
    assert compile_source("f@x = 1", group="mask > 0.5 and top").group.node.inputs[1].node.params["dtype"] == "bool"
    # A ch*() in the Group is never "being typed" on script line 1.
    p = compile_source("@P.z = 1", group="@P.y > chf('k', 1)", pending_line=1)
    assert p.pending == [] and [n for n, *_ in p.params] == ["k"]
    # A keyword before .5 is not a component.
    assert compile_source("f@a = 1 if @P.x > 0 else.5").writes


def test_group_prefix_in_scripts():
    # Houdini spelling in the script: @group_top writes and reads the bool attribute "top".
    p = compile_source("@group_top = @P.z > 0.5\nif @group_top:\n    @P.z = 1")
    assert [(n, t) for n, t, _ in p.writes] == [("top", "bool"), ("P", "vector")]
    fails("f@group_top = 1", "is a group")


def plan(src, group=""):
    p = compile_source(src, group=group)
    return {st.name: st for st in p.stores}, [label for label, _, _ in p.captures], p


def test_write_plan_selection():
    # A conditional write uses the write node's Selection: no Switch, no read of the old value.
    st, caps, _ = plan("if @P.z > 0:\n    f@m = 1")
    assert st["m"].value.const == 1.0 and st["m"].selection.node.kind == "compare" and caps == []
    # Written only in the else branch: Selection is NOT the condition.
    st, _, _ = plan("if @P.z > 0:\n    pass\nelse:\n    f@m = 1")
    assert st["m"].value.const == 1.0 and st["m"].selection.node.params["op"] == "NOT"
    # Nested ifs AND their conditions; the Group comes first.
    st, _, _ = plan("if @P.z > 0:\n    if @P.x > 0:\n        f@m = 1", group="@P.y > 0")
    sel = st["m"].selection
    assert st["m"].value.const == 1.0 and len(st["m"].conds) == 3 and sel.node.params["op"] == "AND"
    # Written in both branches: still a Switch.
    st, _, _ = plan("if @P.z > 0:\n    f@m = 1\nelse:\n    f@m = 2")
    assert st["m"].value.node.kind == "switch" and st["m"].selection is None


def test_write_plan_position_offset():
    for src in ("@P += @N * 0.5", "@P = @N * 0.5 + @P", "@P.z += 1", "@P.z = @P.z - 1",
                "v = @P\nv.x += @ptnum\n@P = v"):
        st, _, _ = plan(src)
        assert st["P"].offset, src
    st, _, _ = plan("@P.z += 1")
    assert st["P"].value.const == (0.0, 0.0, 1.0)
    st, _, _ = plan("@P -= (1, 2, 3)")
    assert st["P"].offset and st["P"].value.const == (-1.0, -2.0, -3.0)
    st, _, _ = plan("if @P.x > 0:\n    @P.z += 1")
    assert st["P"].offset and st["P"].value.const == (0.0, 0.0, 1.0) and st["P"].selection is not None
    for src in ("@P = @P * 2", "@P.z = 1 - @P.z", "@P = @N"):
        st, _, _ = plan(src)
        assert not st["P"].offset, src


def test_write_plan_captures_only_when_needed():
    # Independent writes need no Capture; Set Position goes last.
    st, caps, p = plan("@P.z += 1\nf@a = @P.x\nf@b = @N.y")
    assert caps == [] and [s.name for s in p.stores] == ["a", "b", "P"]
    # A value read before an earlier write changed it is captured (only that one).
    _, caps, _ = plan("t = @a\nf@a = 5\nf@b = t\nf@c = @P.x")
    assert caps == ["@b"]
    # The swap: the second write reads what the first changed.
    _, caps, _ = plan("t = @a\n@a = @b\n@b = t")
    assert caps == ["@b"]
    # Set Position reading an attribute stored before it.
    _, caps, _ = plan("t = @a\nf@a = 1\n@P.z = t")
    assert caps == ["@P"]
    # The Group is captured once it reads something a write before it changed.
    _, caps, _ = plan("b@top = False\nf@x = 5", group="top")
    assert caps == ["Group"]
    _, caps, _ = plan("f@x = 5\nf@y = 6", group="top")
    assert caps == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
