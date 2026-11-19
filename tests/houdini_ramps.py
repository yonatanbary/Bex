"""Reference values for bex/ramp.py, sampled from Houdini's own ramps.

Run with Houdini's Python (once, or after changing CASES):
    hython tests/houdini_ramps.py
It writes tests/houdini_ramps.json; tests/test_ramp.py compares ramp.evaluate()
against it (and skips that check while the file is missing).
"""

import json
from pathlib import Path

# (name, [(position, value)], BEX interpolation for every key)
CASES = [
    ("two keys", [(0.0, 0.0), (1.0, 1.0)]),
    ("hill", [(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)]),
    ("uneven", [(0.0, 0.2), (0.15, 0.9), (0.4, 0.1), (1.0, 0.7)]),
    ("plateau", [(0.0, 0.0), (0.4, 1.0), (0.5, 1.0), (1.0, 0.0)]),
    ("inside", [(0.2, 0.3), (0.6, 0.8), (0.9, 0.1)]),
]
MODES = ["CONSTANT", "LINEAR", "CATMULL_ROM", "MONOTONE_CUBIC", "BSPLINE"]
# Bezier ramps, keys in point order (Bezier reads point order, so these go through a
# real ramp parameter: a bare hou.Ramp sorts its keys).
BEZIER_CASES = [
    ("bezier round preset", [(0.0, 0.0), (0.8, 0.0), (0.9, 0.0), (1.0, 1.0)]),
    ("bezier sharp preset", [(0.0, 0.0), (0.0001, 1.0), (0.5, 1.0), (1.0, 1.0)]),
    ("bezier two segments", [(0.0, 0.2), (0.1, 0.9), (0.3, 0.9), (0.45, 0.4), (0.6, -0.1), (0.8, 1.3), (1.0, 0.6)]),
    ("bezier handles out of order", [(0.1, 0.0), (0.7, 1.0), (0.3, 1.0), (0.9, 0.2)]),
    ("bezier three keys", [(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)]),
    ("bezier two keys", [(0.2, 0.1), (0.8, 0.9)]),
    ("bezier five keys", [(0.0, 0.0), (0.2, 1.0), (0.4, 1.0), (0.6, 0.0), (1.0, 0.5)]),
]
SAMPLES = 101


def main():
    import hou
    basis = {"CONSTANT": hou.rampBasis.Constant, "LINEAR": hou.rampBasis.Linear,
             "CATMULL_ROM": hou.rampBasis.CatmullRom, "MONOTONE_CUBIC": hou.rampBasis.MonotoneCubic,
             "BSPLINE": hou.rampBasis.BSpline}
    out = []
    for name, keys in CASES:
        for mode in MODES:
            ramp = hou.Ramp([basis[mode]] * len(keys), [p for p, _ in keys], [v for _, v in keys])
            xs = [i / (SAMPLES - 1) for i in range(SAMPLES)]
            out.append({"name": name, "mode": mode, "keys": keys, "x": xs, "y": [ramp.lookup(x) for x in xs]})
    node = hou.node("/obj").createNode("null", "bex_bezier")
    group = node.parmTemplateGroup()
    group.append(hou.RampParmTemplate("bexramp", "bexramp", hou.rampParmType.Float))
    node.setParmTemplateGroup(group)
    parm = node.parm("bexramp")
    for name, keys in BEZIER_CASES:
        parm.set(len(keys))
        for i, (p, v) in enumerate(keys, 1):
            node.parm(f"bexramp{i}pos").set(p)
            node.parm(f"bexramp{i}value").set(v)
            node.parm(f"bexramp{i}interp").set("bezier")
        ramp = parm.evalAsRamp()
        xs = [i / (SAMPLES - 1) for i in range(SAMPLES)]
        out.append({"name": name, "mode": "BEZIER", "keys": keys, "x": xs, "y": [ramp.lookup(x) for x in xs]})
    path = Path(__file__).with_suffix(".json")
    path.write_text(json.dumps(out, indent=1))
    print(f"wrote {len(out)} ramps to {path}")


if __name__ == "__main__":
    main()
