"""Pure-Python tests for chramp() ramps (bex/ramp.py).  Run: python3 tests/test_ramp.py"""

import math
import sys
import types
from pathlib import Path

# bex/__init__.py imports bpy, so register a bare "bex" package pointing at the folder.
_BEX = Path(__file__).resolve().parent.parent / "bex"
_pkg = types.ModuleType("bex")
_pkg.__path__ = [str(_BEX)]
sys.modules.setdefault("bex", _pkg)

from bex.ramp import (DEFAULT_KEYS, PRESETS, STEP, anchor_of, bezier_delete, bezier_insert,  # noqa: E402
                      evaluate, from_bezier, handles_of, invert, is_anchor, polyline, reverse, to_bezier)


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
    m = _tangents(keys)
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


def test_two_keys_make_an_s():
    # Like Houdini: the end keys are flat, so two keys ease in and out (not a line).
    for interp in ("CATMULL_ROM", "MONOTONE_CUBIC"):
        keys = [(0.0, 0.0, interp), (1.0, 1.0, interp)]
        assert evaluate(keys, 0.15) < 0.11 and evaluate(keys, 0.85) > 0.89, interp  # below / above the line
        assert near(evaluate(keys, 0.5), 0.5), interp
    assert near(evaluate([(0.0, 0.0, "MONOTONE_CUBIC"), (1.0, 1.0, "MONOTONE_CUBIC")], 0.25), 0.15625)  # smoothstep
    # Catmull-Rom's ends run at half the line's slope (Houdini's softer S): t/2 + 3t^2/2 - t^3
    cr = [(0.0, 0.0, "CATMULL_ROM"), (1.0, 1.0, "CATMULL_ROM")]
    assert near(evaluate(cr, 0.25), 0.203125) and near(evaluate(cr, 0.01) / 0.01, 0.5, 0.04)


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
    # Round and Sharp are quarter circles, each the other turned half around.
    for i in range(1, 100):
        x = i / 100
        assert near(evaluate(PRESETS["Round"], x), 1 - math.sqrt(1 - x * x), 2e-3), x
        assert near(evaluate(PRESETS["Sharp"], x), 1 - evaluate(PRESETS["Round"], 1 - x), 1e-3), x


def test_reverse_and_invert():
    # (A Constant stretch can't flip exactly: its held value belongs to its start key.)
    keys = [(0.0, 0.0, "MONOTONE_CUBIC"), (0.25, 1.0, "LINEAR"), (1.0, 0.5, "LINEAR")]
    r = reverse(keys)
    assert [round(p, 6) for p, _, _ in r] == [0.0, 0.75, 1.0]
    for x in (0.1, 0.5, 0.8, 0.9):
        assert near(evaluate(r, x), evaluate(keys, 1 - x), 2e-3)
    assert reverse(r) == [(p, v, i) for p, v, i in sorted(keys)]
    assert [v for _, v, _ in invert(keys)] == [1.0, 0.0, 0.5]



def test_matches_houdini():
    """ramp.evaluate() against Houdini's own hou.Ramp (tests/houdini_ramps.json, made
    by running tests/houdini_ramps.py in hython). Skipped while that file is missing."""
    import json
    path = Path(__file__).with_name("houdini_ramps.json")
    if not path.exists():
        print("   (skipped: run `hython tests/houdini_ramps.py` to make houdini_ramps.json)")
        return
    wrong = []
    for case in json.loads(path.read_text()):
        keys = [(p, v, case["mode"]) for p, v in case["keys"]]
        worst = 0.0
        for x, y in zip(case["x"], case["y"]):
            if case["mode"] == "CONSTANT" and any(abs(x - p) < 1e-3 for p, _ in case["keys"]):
                continue  # right on a step: either side is fine
            worst = max(worst, abs(evaluate(keys, x) - y))
        if worst > 2e-3:
            wrong.append(f"{case['name']} / {case['mode']}: off by up to {worst:.4f}")
    assert not wrong, "differs from Houdini:\n  " + "\n  ".join(wrong)


ROUND_BEZIER = ((0.0, 0.0, "BEZIER"), (0.8, 0.0, "BEZIER"), (0.9, 0.0, "BEZIER"), (1.0, 1.0, "BEZIER"))  # Houdini's Round


def test_bezier_roles():
    # 7 keys: anchors 0, 3, 6; handles 1, 2 hang off 0 and 3, handles 4, 5 off 3 and 6.
    assert [i for i in range(7) if is_anchor(i, 7)] == [0, 3, 6]
    assert [anchor_of(i, 7) for i in range(7)] == [0, 0, 3, 3, 3, 6, 6]
    assert handles_of(3, 7) == [2, 4] and handles_of(0, 7) == [1] and handles_of(6, 7) == [5]


def test_to_bezier_keeps_the_shape():
    for mode in ("LINEAR", "CATMULL_ROM", "MONOTONE_CUBIC"):
        keys = [(0.0, 0.2, mode), (0.3, 0.9, mode), (0.7, 0.1, mode), (1.0, 0.6, mode)]
        b = to_bezier(keys)
        assert len(b) == 10 and all(i == "BEZIER" for _, _, i in b)
        for x in (0.05, 0.2, 0.5, 0.65, 0.9):
            assert near(evaluate(b, x), evaluate(keys, x), 2e-3), (mode, x, evaluate(b, x), evaluate(keys, x))
    back = from_bezier(to_bezier(keys), "LINEAR")
    assert [(round(p, 6), round(v, 6)) for p, v, _ in back] == [(p, v) for p, v, _ in keys]


def test_bezier_insert_keeps_the_shape():
    keys = list(ROUND_BEZIER)
    new, at = bezier_insert(keys, 0.6)
    assert len(new) == 7 and at == 3 and near(new[at][0], 0.6, 1e-6)
    for x in (0.1, 0.5, 0.6, 0.8, 0.95):
        assert near(evaluate(new, x), evaluate(keys, x), 1e-3)
    # Past the last anchor: a straight stretch.
    new, at = bezier_insert([(0.0, 0.0, "BEZIER"), (0.2, 0.3, "BEZIER"), (0.4, 0.6, "BEZIER"), (0.5, 1.0, "BEZIER")], 0.8)
    assert len(new) == 7 and at == 6 and new[6][:2] == (0.8, 1.0)


def test_bezier_delete_takes_the_handles():
    keys = [(i / 6, 0.0, "BEZIER") for i in range(7)]
    assert [p for p, _, _ in bezier_delete(keys, [3])] == [keys[i][0] for i in (0, 1, 5, 6)]
    assert [p for p, _, _ in bezier_delete(keys, [0])] == [keys[i][0] for i in (3, 4, 5, 6)]
    assert [p for p, _, _ in bezier_delete(keys, [6])] == [keys[i][0] for i in (0, 1, 2, 3)]
    assert bezier_delete(keys, [1, 2]) == keys  # handles alone: nothing
    assert len(bezier_delete(keys, [0, 3, 6])) == 1  # one anchor stays


def test_bezier_reverse_and_invert():
    keys = list(ROUND_BEZIER)
    r = reverse(keys)
    for x in (0.05, 0.3, 0.7):
        assert near(evaluate(r, x), evaluate(keys, 1 - x), 1e-3)
    assert [v for _, v, _ in invert(keys)] == [1.0, 1.0, 1.0, 0.0]  # point order kept


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(len(tests), "passed")
