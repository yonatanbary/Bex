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


def zone_of(val):
    """The repeat_out node behind a written value (looking through one conversion level)."""
    n = val.node
    assert n is not None and n.kind == "repeat_out", n
    return n, n.params["zone"]


def test_for_folds_constants():
    w = writes("s = 0\nfor i in range(4):\n    s += i\n@a = s\n@b = i")
    assert w["a"].const == 6.0 and w["b"].const == 3.0  # the loop variable keeps its last value
    w = writes("for i in range(3):\n    @a = i * 2")
    assert w["a"].const == 4.0
    w = writes("s = 0\nfor i in range(100000):\n    pass\n@a = s")  # too long to fold, nothing changes
    assert w["a"].const == 0.0


def kinds(val):
    """Every IR node kind `val` depends on (following zones), with counts."""
    seen, stack, found = set(), [val.node], {}
    while stack:
        n = stack.pop()
        if n is None or id(n) in seen:
            continue
        seen.add(id(n))
        found[n.kind] = found.get(n.kind, 0) + 1
        stack += [v.node for v in n.inputs] + ([n.params["zone"]] if n.kind == "repeat_out" else []) + ([n.params["closure"]] if n.kind == "closure_call" else [])
    return found


def test_short_loops_unroll():
    # Up to UNROLL_MAX iterations: no zone, so components and the Set Position Offset work.
    c = compile_source("v = @N\nfor i in range(3):\n    v[i] = v[i] * 2\n@a = v.x\nfor i in range(8):\n    @P.z += @a")
    assert all("repeat_out" not in kinds(v) for _, _, v in c.writes)
    assert [st.offset for st in c.stores if st.name == "P"] == [True]
    c = compile_source("x = @P.x\nfor i in range(9):\n    x += i\n@a = x")
    assert "repeat_out" in kinds(c.writes[0][2])


def test_nested_loop_counts():
    # The inner count comes from the outer loop's Iteration: one value, so it's fine.
    c = compile_source("for i in range(chi('n', 4)):\n    for j in range(i):\n        @a += @P.x")
    assert kinds(c.writes[0][2])["repeat_in"] == 2
    w = writes("s = 0\nfor i in range(4):\n    for j in range(i):\n        s += 1\n@a = s")
    assert w["a"].const == 6.0


def test_per_element_count():
    # range(@ptnum): the zone runs the longest count (an Attribute Statistic Max), each
    # element stops changing at its own count.
    c = compile_source("for i in range(@ptnum):\n    @P.z += 0.1", group="@P.x > 0")
    found = kinds(c.stores[0].value)
    assert found["stat"] == 1 and found["repeat_in"] == 1
    stat = next(n for n in _nodes(c.stores[0].value) if n.kind == "stat")
    assert stat.inputs[1] == c.group  # only elements in the Group count
    assert not c.captures


def _nodes(val):
    seen, stack = {}, [val.node]
    while stack:
        n = stack.pop()
        if n is None or id(n) in seen:
            continue
        seen[id(n)] = n
        stack += [v.node for v in n.inputs] + ([n.params["zone"]] if n.kind == "repeat_out" else []) + ([n.params["closure"]] if n.kind == "closure_call" else [])
    return list(seen.values())


def test_break_continue():
    # Constant: worked out now, like Python.
    w = writes("s = 0\nfor i in range(100):\n    if i == 5:\n        break\n    if i % 2:\n        continue\n    s += i\n"
               "@a = s\n@b = i")
    assert w["a"].const == 0 + 2 + 4 and w["b"].const == 5
    w = writes("i = 0\nwhile True:\n    i += 1\n    if i >= 7:\n        break\n@a = i")
    assert w["a"].const == 7
    # Per element: the flags become zone items, values freeze once broken.
    # A loop that can end early runs in growing chunks, skipped once every element is done.
    c = compile_source("x = 0.0\nfor i in range(chi('n', 50)):\n    if @P.x * i > 1:\n        break\n    x += 1\n@a = x")
    (end,) = c.preludes
    chunk = end.params["chunk"]
    assert end.kind == "chunk_end" and chunk.params["items"][end.params["flag"]] == "bool"
    assert chunk.params["count"] == 19  # 32 * (1.5^19 - 1) / 0.5 >= MAX_ZONE_ITERATIONS
    assert c.writes[0][2].node.kind == "attr" and c.writes[0][2].node.params["name"].startswith(".bex")
    # Inside another zone: a plain masked zone.
    c = compile_source("for j in range(chi('m', 3)):\n    for i in range(chi('n', 50)):\n        if @P.x * i > 1:\n"
                       "            break\n        @a += 1")
    assert not c.preludes  # the break is the inner loop's, and that one sits in a zone
    # Values that depend on each other survive the chunk stores (swap through shadows).
    c = compile_source("a = 0.0\nb = 1.0\nwhile b < @P.x * 1000 + 2:\n    t = a + b\n    a = b\n    b = t\n@f = a")
    assert len(c.preludes) == 1
    c = compile_source("x = 0.0\nfor i in range(20):\n    if @P.x > i:\n        continue\n    x += i\n@a = x")
    assert "repeat_in" in kinds(c.writes[0][2])


def test_while_loops():
    # A constant condition with a non-constant body: a zone with the exact count.
    c = compile_source("i = 0\nx = @P.x\nwhile i < 20:\n    x = x * 0.5 + 1\n    i += 1\n@a = x")
    zone = c.writes[0][2].node.params["zone"]
    assert zone.inputs[0].const == 20
    # A per-element condition: a zone with the safety cap, stopping each element.
    c = compile_source("x = @P.x\nn = 0\nwhile x > 1:\n    x *= 0.5\n    n += 1\ni@n = n")
    assert len(c.preludes) == 1  # chunked
    c = compile_source("for j in range(chi('m', 2)):\n    x = @P.x\n    while x > 1:\n        x *= 0.5\n    @a += x")
    zones = [n for n in _nodes(c.writes[0][2]) if n.kind == "repeat_in"]
    assert sorted(z.inputs[0].const or 0 for z in zones) == [0, 1000]  # inside another loop: the cap
    # Short ones unroll.
    w = writes("i = 0\nx = @P.x\nwhile i < 3:\n    x += 1\n    i += 1\n@a = x")
    assert "repeat_in" not in kinds(w["a"])


def test_own_functions():
    # Inlined: constants fold through, defaults and keywords bind, globals are read.
    w = writes("""
scale = 2
def f(x, k=3):
    y = x * k
    return y + scale
@a = f(1)
@b = f(1, k=10)
@c = f(k=1, x=5)
""")
    assert (w["a"].const, w["b"].const, w["c"].const) == (5.0, 12.0, 7.0)
    # Locals stay local; attributes can be written; a value-less call is a statement.
    w = writes("""
def lift(h):
    @P.z += h
    t = 5
t = 1
lift(0.5)
@a = t
""")
    assert w["a"].const == 1.0 and "P" in w
    # Early return under a per-element condition.
    c = compile_source("def sgn(x):\n    if x < 0:\n        return -1.0\n    return 1.0\n@a = sgn(@P.x)")
    assert c.writes[0][2].node.kind == "switch"
    # return inside a loop leaves it.
    w = writes("def first_over(limit):\n    for i in range(100):\n        if i * i > limit:\n            return i\n"
               "    return -1\n@a = first_over(50)")
    assert w["a"].const == 8.0
    c = compile_source("def steps(x):\n    n = 0\n    for i in range(chi('cap', 99)):\n        if x < 1:\n"
                       "            return n\n        x *= 0.5\n        n += 1\n    return n\n@a = steps(@P.x * 100)")
    assert c.preludes  # can end early: chunked
    # Recursion that ends while compiling.
    w = writes("def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n@a = fib(10)")
    assert w["a"].const == 55.0
    # A function writing an attribute inside a loop: the attribute is a zone item.
    c = compile_source("def bump():\n    @w += @P.x\nfor i in range(chi('n', 20)):\n    bump()")
    assert c.writes[0][0] == "w" and c.writes[0][2].node.kind == "repeat_out"


def test_shared_functions_are_closures():
    src = "def wave(x, f):\n    y = sin(x * f) * 0.5\n    return y * y + x\n@a = wave(@P.x, 3)\n@b = wave(@P.y, 5)\n"
    c = compile_source(src)
    calls = [v.node for _, _, v in c.writes]
    assert [n.kind for n in calls] == ["closure_call", "closure_call"]
    assert calls[0].params["closure"] is calls[1].params["closure"]  # one body
    assert sum(1 for n in _nodes(c.writes[0][2]) if n.kind == "math" and n.params["op"] == "SINE") == 1
    # Called once, all-constant arguments, or writing attributes: inlined.
    assert writes("def wave(x, f):\n    return sin(x * f)\n@a = wave(@P.x, 3)")["a"].node.kind == "math"
    w = writes("def sq(x):\n    return x * x\n@a = sq(3) + sq(4)")
    assert w["a"].const == 25.0
    c = compile_source("def lift(x):\n    @w += x\n    return x\n@a = lift(@P.x) + lift(@P.y)")
    assert all("closure_call" not in kinds(v) for _, _, v in c.writes)
    # Different argument types: one closure each.
    c = compile_source("def dbl(x):\n    return x * 2\nv@a = dbl(@P) + dbl(@N)\n@b = dbl(@P.x) + dbl(@P.y)")
    closures = {id(n.params["closure"]) for _, _, v in c.writes for n in _nodes(v) if n.kind == "closure_call"}
    assert len(closures) == 2


def test_review_regressions():
    # A shared function reads the script's variables / attributes as they are at each call.
    c = compile_source("k = @P.x\ndef f(a):\n    return a * k\nf@x = f(@P.y)\nk = 5.0\nf@y = f(@P.y + 1)")
    x, y = (v.node for _, _, v in c.writes)
    assert x.params["closure"] is not y.params["closure"] if y.kind == "closure_call" else True
    # A per-element argument used as a loop count inside a shared function: its own closure, a real count.
    c = compile_source("def f(n, x):\n    for i in range(n):\n        x += 0.5\n    return x\n"
                       "f@x = f(@ptnum + 9, 1.0)\nf@y = f(@ptnum % 3 + 10, @P.x)")
    assert any(n.kind == "stat" for _, _, v in c.writes for n in _nodes(v))
    # return inside two nested zone loops: the outer break flag is an item of the inner zone.
    c = compile_source("def f(lim):\n    x = 0.0\n    for i in range(12):\n        for j in range(12):\n"
                       "            x += 1\n            if x > lim:\n                return x\n    return -x\nf@x = f(@ptnum * 3.0)")
    zones = [n for n in _nodes(c.writes[0][2]) if n.kind == "repeat_in"]
    assert len(zones) == 2 and all(z.params["items"].count("bool") >= 2 for z in zones)
    # Rollback keeps channels of a shared function first called inside a long loop.
    c = compile_source("def f(a):\n    return a * chf('amp', 2.0)\ns = 0.0\nfor i in range(20):\n    s += f(@P.x * i)\n"
                       "@a = s\n@b = f(@P.y)")
    assert [p[0] for p in c.params] == ["amp"]
    # Called once inside a zone loop: inlined (reruns don't count as calls).
    c = compile_source("def f(a):\n    return a * 2\ns = 0.0\nfor i in range(20):\n    s += f(@P.x * i)\n@a = s")
    assert "closure_call" not in kinds(c.writes[0][2])
    # A while inside another loop warns about its cap.
    assert "stops after 1000" in compile_source("for j in range(chi('m', 3)):\n    y = @P.x\n    while y < 100:\n"
                                                "        y += 1\n    @a += y").warnings[0]
    # After no iterations the loop variable keeps its value; defaults see the script's variables.
    c = compile_source("i = 7\nfor i in range(chi('n', 0)):\n    @a += @P.x\n@b = i")
    assert c.writes[1][2].node.kind == "switch"
    w = writes("k = 1\ndef g(x, d=k):\n    return x + d\ndef f(k):\n    return g(k)\n@a = f(10)")
    assert w["a"].const == 11.0


def test_function_errors():
    fails("def f(x):\n    if x > 0:\n        return 1\n@a = f(@P.x)", "every path")
    fails("def f(x):\n    y = x\n@a = f(1)", "doesn't return a value")
    fails("def f(n):\n    return f(n - 1)\n@a = f(@ptnum)", "too deeply")
    fails("def f(x):\n    return x\n@a = f(1, 2)", "takes 1 argument")
    fails("def f(x):\n    return x\n@a = f(y=2)", "no parameter 'y'")
    fails("def f(x):\n    return x\n@a = f()", "missing the argument 'x'")
    fails("@a = sin(x=1)", "no keyword arguments")
    fails("def sin(x):\n    return x", "built-in")
    fails("return 1", "inside a function")
    fails("def f(a=1, b):\n    return a", "needs a default")
    fails("@a = g(1)\ndef g(x):\n    return x", "unknown function")


def test_for_repeat_zone():
    # A loop whose values depend on the geometry is one Repeat Zone, not unrolled copies.
    c = compile_source("x = @P.x\nfor i in range(64):\n    x = x + sin(x * 3 + i * 0.1) * 0.01\n@a = x")
    out, zone = zone_of(c.writes[0][2])
    assert zone.inputs[0].const == 64 and zone.params["items"] == ("float",)
    assert zone.inputs[1].node.kind == "separate"  # starts at @P.x
    # The body is built once: one sine in the whole program.
    seen, stack, sines = set(), [out], 0
    while stack:
        n = stack.pop()
        if n is None or id(n) in seen:
            continue
        seen.add(id(n))
        sines += n.kind == "math" and n.params.get("op") == "SINE"
        stack += [v.node for v in n.inputs] + ([n.params["zone"]] if n.kind == "repeat_out" else []) + ([n.params["closure"]] if n.kind == "closure_call" else [])
    assert sines == 1
    # A count from a channel: ceil((stop - start) / step) iterations, never negative.
    c = compile_source("n = chi('n', 5)\ns = 0.0\nfor i in range(2, n, 2):\n    s += @P.x * i\n@a = s")
    out, zone = zone_of(c.writes[0][2])
    clamp = zone.inputs[0].node  # min(max(count, 0), MAX_ZONE_ITERATIONS)
    assert clamp.params["op"] == "MINIMUM" and clamp.inputs[0].node.params["op"] == "MAXIMUM"
    # Attributes written in the loop are items too, and the write happens once, after it.
    c = compile_source("for i in range(chi('n', 3)):\n    @P.y += 0.1\n    @w = @w + @P.y")
    assert {name for name, _, _ in c.writes} == {"P", "w"}
    assert len(c.stores) == 2 and all(st.value.node.kind == "repeat_out" for st in c.stores)
    assert len({id(st.value.node) for st in c.stores}) == 1


def test_for_item_types_widen():
    c = compile_source("x = 0\nfor i in range(chi('n', 3)):\n    x += 0.5\n@a = x")
    out, zone = zone_of(c.writes[0][2])
    assert zone.params["items"] == ("float",)
    c = compile_source("v = 0\nfor i in range(chi('n', 3)):\n    v = v + @N\n@a = v.x")
    assert c.writes[0][2].node.inputs[0].node.params["zone"].params["items"] == ("vector",)


def test_for_errors():
    fails("n = @ptnum\nfor i in range(chi('m', 4)):\n    n += 1\n    for j in range(n):\n        @a += 1",
          "can't depend on values an outer loop changes")
    fails("break", "only be used inside a loop")
    fails("for i in range(0, 9, chi('s', 1)):\n    pass", "step must be a fixed number")
    fails("for i in range(1.5):\n    pass", "whole numbers")
    fails("for i in range(chi('n', 3)):\n    y = @P.x\n@a = y", "only set inside the loop on line 1")
    w = writes("for i in range(chi('n', 3)):\n    y = @P.x\n    @a = y")  # fine inside the loop
    assert w["a"].node.kind == "repeat_out"
    fails("while True:\n    pass", "more than")


def test_channels():
    p = compile_source('@P *= chf("scale", 2)\n@P += chv("offset")\ni@k = chi("count", 3) + chi("count")')
    assert p.params == [("scale", "float", 2.0), ("offset", "vector", (0.0, 0.0, 0.0)), ("count", "int", 3)]
    fails('@a = chf("x") + chi("x")', "already used")
    fails("@a = chf(1)", "name in quotes")


def test_chramp():
    p = compile_source('@a = chramp("falloff", @P.x)\n@b = chramp("falloff", @P.x) * 2')
    assert p.ramps == ["falloff"] and p.params == []
    a = {n: v for n, _, v in p.writes}["a"]
    assert a.type == "float" and a.node.kind == "ramp" and a.node.params == {"name": "falloff"}
    assert p.shared_ramps == set()
    # Read at two positions: one ramp, shared (a closure in the builder).
    p = compile_source('@a = chramp("r", @P.x)\n@b = chramp("r", @P.z)')
    assert p.ramps == ["r"] and p.shared_ramps == {"r"}
    fails('@a = chramp(1, @P.x)', "name in quotes")
    fails('@a = chramp("r")', "a name and a position")
    # A new ramp on the line being typed acts as a straight ramp and makes no node yet.
    p = compile_source('@a = chramp("fal", @P.x)', pending_line=1)
    assert p.ramps == [] and p.pending == ["fal"]
    assert compile_source('@a = chramp("fal", @P.x)', pending_line=1, known_params={"fal"}).ramps == ["fal"]


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
    fails("v@a = getbbox_min(4)", "input number")
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
    fails("import math", "not supported")
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


def test_other_inputs():
    p = compile_source('v@a = point(1, "P", @ptnum)\nf@b = prim(3, "f@area", 0)\ni@n = npoints(1)')
    assert p.inputs == [1, 3]
    a = {n: v for n, _, v in p.writes}
    assert a["a"].node.kind == "sample" and a["a"].node.params["domain"] == "POINT" and a["a"].type == "vector"
    assert a["b"].node.params == {"input": 3, "domain": "FACE", "dtype": "float"}
    assert a["n"].node.kind == "count" and a["n"].type == "int"
    # Input 0 is this node's geometry: no extra socket.
    p = compile_source('f@x = point(0, "x", 3) + npoints(0) + @numpt\nv@c = getbbox_max(2)')
    assert p.inputs == [2] and not any(st.offset for st in p.stores)
    # @opinput1_name reads the same element number on input 1; the type comes from the prefix.
    w = writes("v@c = v@opinput1_Cd\nf@d = @opinput2_mass")
    assert w["c"].type == "vector" and w["c"].node.params["input"] == 1
    assert w["d"].node.inputs[1].node.kind == "input"  # the index of this element
    w = writes("i@k = nearpoint(1, @P)")
    assert w["k"].node.kind == "nearest" and w["k"].type == "int"
    # Reading another input never needs a Capture, even after a write to the same name.
    _, caps, _ = plan('f@a = 5\nf@b = point(0, "a", 0) + @opinput0_a')
    assert caps == []
    fails('f@a = point(4, "a", 0)', "input number")
    fails('f@a = point(1, a, 0)', "attribute name in quotes")
    fails("@opinput1_P = 1", "cannot be written")
    fails("@numpt = 1", "read-only")
    fails('chf("Input 2")', "cannot be used")


def test_curve_builtins():
    w = writes("f@u = @curveu\nf@l = @curvelength")
    assert w["u"].node.params["name"] == "spline_parameter" and w["u"].out == 0 and w["l"].out == 1
    fails("@curveu = 1", "read-only")


def test_domain_indices():
    def kind(src, domain):
        v = writes(src, domain)["a"]
        return v.node.params["name"], v.out
    assert kind("i@a = @ptnum", "POINT") == ("index", 0)
    assert kind("i@a = @ptnum", "CORNER") == ("vertex_of_corner", 0)
    assert kind("i@a = @primnum", "FACE") == ("index", 0) == kind("i@a = @primnum", "CURVE")
    assert kind("i@a = @primnum", "CORNER") == ("face_of_corner", 0)
    assert kind("i@a = @curvenum", "POINT") == ("curve_of_point", 0)
    assert kind("i@a = @curvenum", "CURVE") == ("index", 0)
    assert kind("i@a = @vtxnum", "CORNER") == kind("i@a = @edgenum", "EDGE") == ("index", 0)
    assert kind("i@a = @elemnum", "FACE") == ("index", 0)
    fails("i@a = @primnum", "@curvenum", domain="POINT")
    fails("i@a = @ptnum", "@elemnum", domain="FACE")
    fails("i@a = @vtxnum", "Corners", domain="POINT")
    fails("@curvenum = 1", "read-only")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
