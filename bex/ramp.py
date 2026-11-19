"""Ramps for chramp(): Houdini-style keys, evaluated and sampled in plain Python.

A ramp is a list of keys `(position, value, interpolation)`. A key's interpolation
shapes the stretch from it to the next key, like Houdini. The node graph cannot
run these interpolations, so `polyline()` samples the ramp into straight pieces
that a Float Curve (vector handles) reproduces; Blender bakes a Float Curve into a
256-step table anyway, so the extra points cost nothing when it runs.

No bpy here, so it can be tested outside Blender.
"""

import math

INTERPOLATIONS = (  # (id, label, description)
    ("CONSTANT", "Constant", "Hold the value until the next point"),
    ("LINEAR", "Linear", "Straight line to the next point"),
    ("CATMULL_ROM", "Catmull-Rom", "Smooth curve through the points; can overshoot"),
    ("MONOTONE_CUBIC", "Monotone Cubic", "Smooth curve through the points that never overshoots"),
    ("BSPLINE", "B-Spline", "Very smooth curve pulled towards the points, not through them"),
)
DEFAULT_INTERPOLATION = "MONOTONE_CUBIC"
STEP = 1e-4          # width of the jump a Constant key makes before the next key
SAMPLES = 256        # points per unit of position on a curved stretch, before simplify()
TOLERANCE = 1e-4     # simplify() drops points the line already passes this close to


def _sorted(keys):
    return sorted(((float(p), float(v), i) for p, v, i in keys), key=lambda k: k[0])


def _tangents(keys, monotone):
    """Slopes at each key for the cubic stretches: Catmull-Rom (finite
    differences) or monotone (Fritsch-Carlson, so no overshoot)."""
    n = len(keys)
    secant = []
    for a, b in zip(keys, keys[1:]):
        h = b[0] - a[0]
        secant.append((b[1] - a[1]) / h if h > 1e-9 else 0.0)
    m = [0.0] * n
    for k in range(n):
        if k == 0:
            m[k] = secant[0]
        elif k == n - 1:
            m[k] = secant[-1]
        else:
            h = keys[k + 1][0] - keys[k - 1][0]
            m[k] = (keys[k + 1][1] - keys[k - 1][1]) / h if h > 1e-9 else 0.0
    if monotone:
        for k in range(n - 1):
            d = secant[k]
            if abs(d) < 1e-12:
                m[k] = m[k + 1] = 0.0
                continue
            for j in (k, k + 1):
                if m[j] * d <= 0:
                    m[j] = 0.0
            a, b = m[k] / d, m[k + 1] / d
            s = a * a + b * b
            if s > 9.0:
                t = 3.0 / math.sqrt(s)
                m[k], m[k + 1] = t * a * d, t * b * d
    return m


def _hermite(p0, v0, m0, p1, v1, m1, x):
    h = p1 - p0
    t = (x - p0) / h
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1) * v0 + (t3 - 2 * t2 + t) * h * m0
            + (-2 * t3 + 3 * t2) * v1 + (t3 - t2) * h * m1)


def _bspline(keys, k, t):
    """Point on the uniform cubic B-spline stretch from key k to k+1 (the end
    keys are repeated, so the curve starts and ends near them)."""
    n = len(keys)
    pts = [keys[max(0, min(n - 1, j))] for j in (k - 1, k, k + 1, k + 2)]
    t2, t3 = t * t, t * t * t
    w = ((1 - t) ** 3 / 6, (3 * t3 - 6 * t2 + 4) / 6, (-3 * t3 + 3 * t2 + 3 * t + 1) / 6, t3 / 6)
    return (sum(wi * p[0] for wi, p in zip(w, pts)), sum(wi * p[1] for wi, p in zip(w, pts)))


def polyline(keys):
    """The ramp as [(x, y)] corners of straight pieces from x = 0 to 1, x rising.
    Before the first key and after the last the value is held."""
    keys = _sorted(keys)
    if not keys:
        return [(0.0, 0.0), (1.0, 0.0)]
    if len(keys) == 1:
        return [(0.0, keys[0][1]), (1.0, keys[0][1])]
    cubic = {i for _, _, i in keys}
    tangents = {
        "CATMULL_ROM": _tangents(keys, False) if "CATMULL_ROM" in cubic else None,
        "MONOTONE_CUBIC": _tangents(keys, True) if "MONOTONE_CUBIC" in cubic else None,
    }
    pts = [(0.0, keys[0][1])] if keys[0][0] > 0 else []
    for k in range(len(keys) - 1):
        (p0, v0, interp), (p1, v1, _) = keys[k], keys[k + 1]
        h = p1 - p0
        pts.append((p0, v0))
        if h <= 1e-9:
            continue
        if interp == "CONSTANT":
            pts.append((max(p0, p1 - STEP), v0))
        elif interp in tangents:
            m = tangents[interp]
            n = max(2, int(SAMPLES * h))
            pts += [(p0 + h * j / n, _hermite(p0, v0, m[k], p1, v1, m[k + 1], p0 + h * j / n)) for j in range(1, n)]
        elif interp == "BSPLINE":
            n = max(2, int(SAMPLES * h))
            for j in range(0, n + 1):
                x, y = _bspline(keys, k, j / n)
                pts.append((min(max(x, p0), p1), y))
    pts.append((keys[-1][0], keys[-1][1]))
    if keys[-1][0] < 1:
        pts.append((1.0, keys[-1][1]))
    # Keep x rising (a B-spline stretch can start a hair before its key).
    out = []
    for x, y in pts:
        x = min(max(x, 0.0), 1.0)
        if out and x < out[-1][0]:
            x = out[-1][0]
        if out and abs(x - out[-1][0]) < 1e-9 and abs(y - out[-1][1]) < 1e-9:
            continue
        out.append((x, y))
    return simplify(out)


def simplify(pts, tolerance=TOLERANCE):
    """`pts` without the points a straight line through their neighbours already
    passes within `tolerance` (vertically): a straight stretch keeps only its ends.
    Blender bakes a Float Curve into a 256-step table, so this loses nothing."""
    keep = [0, len(pts) - 1]
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (xa, ya), (xb, yb) = pts[a], pts[b]
        worst, at = tolerance, None
        for i in range(a + 1, b):
            x, y = pts[i]
            line = ya if xb - xa < 1e-12 else ya + (yb - ya) * (x - xa) / (xb - xa)
            if abs(y - line) > worst:
                worst, at = abs(y - line), i
        if at is not None:
            keep.append(at)
            stack += [(a, at), (at, b)]
    return [pts[i] for i in sorted(set(keep))]


def evaluate(keys, x, line=None):
    """The ramp's value at x (what the node graph gives, up to its table steps)."""
    pts = line or polyline(keys)
    x = min(max(x, 0.0), 1.0)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 - x0 < 1e-12 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def reverse(keys):
    """Flip left to right. Each stretch keeps its interpolation, which now
    belongs to the key at its other end."""
    keys = _sorted(keys)
    n = len(keys)
    return [(1.0 - keys[k][0], keys[k][1], keys[k - 1][2] if k > 0 else keys[-1][2]) for k in range(n - 1, -1, -1)]


def invert(keys):
    """Flip upside down (value -> 1 - value)."""
    return [(p, 1.0 - v, i) for p, v, i in _sorted(keys)]


def _sampled(fn, xs, interp=DEFAULT_INTERPOLATION):
    return tuple((x, round(fn(x), 4), interp) for x in xs)


# Houdini's default presets.
PRESETS = {
    "Flat": ((0.0, 1.0, "LINEAR"), (1.0, 1.0, "LINEAR")),
    "Hill": ((0.0, 0.0, "MONOTONE_CUBIC"), (0.5, 1.0, "MONOTONE_CUBIC"), (1.0, 0.0, "MONOTONE_CUBIC")),
    "Linear": ((0.0, 0.0, "LINEAR"), (1.0, 1.0, "LINEAR")),
    "Round": _sampled(lambda x: 1 - math.sqrt(max(0.0, 1 - x * x)), (0.0, 0.5, 0.8, 0.95, 1.0)),
    "Sharp": _sampled(lambda x: math.sqrt(max(0.0, 1 - (1 - x) ** 2)), (0.0, 0.05, 0.2, 0.5, 1.0)),
    "Smooth": _sampled(lambda x: x * x * (3 - 2 * x), (0.0, 0.15, 0.5, 0.85, 1.0)),
    "Steps": ((0.0, 0.0, "CONSTANT"), (0.25, 1 / 3, "CONSTANT"), (0.5, 2 / 3, "CONSTANT"), (0.75, 1.0, "CONSTANT")),
    "Valley": ((0.0, 1.0, "MONOTONE_CUBIC"), (0.5, 0.0, "MONOTONE_CUBIC"), (1.0, 1.0, "MONOTONE_CUBIC")),
}
DEFAULT_KEYS = ((0.0, 0.0, DEFAULT_INTERPOLATION), (1.0, 1.0, DEFAULT_INTERPOLATION))
