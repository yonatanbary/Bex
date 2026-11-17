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


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
