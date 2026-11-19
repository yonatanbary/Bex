"""Pure-Python tests for chramp() ramps (bex/ramp.py).  Run: python3 tests/test_ramp.py"""

import sys
import types
from pathlib import Path

# bex/__init__.py imports bpy, so register a bare "bex" package pointing at the folder.
_BEX = Path(__file__).resolve().parent.parent / "bex"
_pkg = types.ModuleType("bex")
_pkg.__path__ = [str(_BEX)]
sys.modules.setdefault("bex", _pkg)

from bex.ramp import (DEFAULT_KEYS, PRESETS, STEP, evaluate, invert, polyline,  # noqa: E402
                      reverse)


def near(a, b, eps=1e-3):
    return abs(a - b) < eps


def test_default_is_straight():
    for x in (0, 0.25, 0.5, 0.9, 1):
        assert near(evaluate(DEFAULT_KEYS, x), x)


def test_only_the_points_needed():
    assert polyline(DEFAULT_KEYS) == [(0.0, 0.0), (1.0, 1.0)]  # straight: just its ends
    hill = polyline(PRESETS["Hill"])
    assert 10 < len(hill) < 200  # a curve keeps what it needs
    # Dropping points changes the shape by less than the tolerance.
    from bex.ramp import _sorted, _tangents, _hermite
    keys = _sorted(PRESETS["Hill"])
    m = _tangents(keys, True)
    for i in range(1, 200):
        x = i / 200
        k = 0 if x <= 0.5 else 1
        exact = _hermite(keys[k][0], keys[k][1], m[k], keys[k + 1][0], keys[k + 1][1], m[k + 1], x)
        assert abs(evaluate(PRESETS["Hill"], x) - exact) < 2e-4


def test_linear_and_constant():
    keys = [(0.0, 0.0, "CONSTANT"), (0.5, 1.0, "LINEAR"), (1.0, 0.0, "LINEAR")]
    assert near(evaluate(keys, 0.25), 0.0) and near(evaluate(keys, 0.5 - 2 * STEP), 0.0)
    assert near(evaluate(keys, 0.5), 1.0) and near(evaluate(keys, 0.75), 0.5)


def test_held_outside_the_keys():
    keys = [(0.3, 0.2, "LINEAR"), (0.6, 0.8, "LINEAR")]
    assert near(evaluate(keys, 0.0), 0.2) and near(evaluate(keys, 1.0), 0.8)
    assert near(evaluate([(0.4, 0.7, "LINEAR")], 0.9), 0.7)  # one key: flat


def test_monotone_never_overshoots():
    keys = [(0.0, 0.0, "MONOTONE_CUBIC"), (0.4, 1.0, "MONOTONE_CUBIC"), (0.5, 1.0, "MONOTONE_CUBIC"), (1.0, 0.0, "MONOTONE_CUBIC")]
    ys = [y for _, y in polyline(keys)]
    assert max(ys) <= 1.0 + 1e-9 and min(ys) >= -1e-9
    cr = [y for _, y in polyline([(p, v, "CATMULL_ROM") for p, v, _ in keys])]
    assert max(cr) > 1.0  # Catmull-Rom does overshoot here
    assert near(evaluate(keys, 0.4), 1.0)  # both pass through the keys


def test_bspline_is_pulled_towards_keys():
    keys = [(0.0, 0.0, "BSPLINE"), (0.5, 1.0, "BSPLINE"), (1.0, 0.0, "BSPLINE")]
    assert 0.3 < evaluate(keys, 0.5) < 1.0


def test_polyline_rises_and_spans():
    for keys in list(PRESETS.values()) + [DEFAULT_KEYS]:
        pts = polyline(keys)
        assert pts[0][0] == 0.0 and pts[-1][0] == 1.0
        assert all(b[0] >= a[0] for a, b in zip(pts, pts[1:]))


def test_presets():
    assert near(evaluate(PRESETS["Hill"], 0.5), 1) and near(evaluate(PRESETS["Hill"], 0), 0)
    assert near(evaluate(PRESETS["Valley"], 0.5), 0)
    assert near(evaluate(PRESETS["Steps"], 0.3), 1 / 3) and near(evaluate(PRESETS["Steps"], 0.9), 1)
    assert evaluate(PRESETS["Round"], 0.5) < 0.25 < evaluate(PRESETS["Sharp"], 0.5)


def test_reverse_and_invert():
    # (A Constant stretch can't flip exactly: its held value belongs to its start key.)
    keys = [(0.0, 0.0, "MONOTONE_CUBIC"), (0.25, 1.0, "LINEAR"), (1.0, 0.5, "LINEAR")]
    r = reverse(keys)
    assert [round(p, 6) for p, _, _ in r] == [0.0, 0.75, 1.0]
    for x in (0.1, 0.5, 0.8, 0.9):
        assert near(evaluate(r, x), evaluate(keys, 1 - x), 2e-3)
    assert reverse(r) == [(p, v, i) for p, v, i in sorted(keys)]
    assert [v for _, v, _ in invert(keys)] == [1.0, 0.0, 0.5]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(len(tests), "passed")
