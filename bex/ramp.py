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
    ("BEZIER", "Bezier", "Curve between points shaped by two handles; set for the whole ramp"),
)
DEFAULT_INTERPOLATION = "MONOTONE_CUBIC"
STEP = 1e-4          # width of the jump a Constant key makes before the next key
SAMPLES = 256        # points per unit of position on a curved stretch, before simplify()
TOLERANCE = 1e-4     # simplify() drops points the line already passes this close to


def _sorted(keys):
    return sorted(((float(p), float(v), i) for p, v, i in keys), key=lambda k: k[0])


def _catmull_rom(keys):
    """Uniform Catmull-Rom tangents, per stretch of parameter (not per unit of
    position): half the difference of the neighbours' values, an end key standing
    in for its missing neighbour. Like Houdini, two keys make a soft S whose ends
    run at half the straight line's slope."""
    v = [k[1] for k in keys]
    n = len(v)
    return [(v[min(k + 1, n - 1)] - v[max(k - 1, 0)]) / 2 for k in range(n)]


def _tangents(keys):
    """Slopes (per unit of position) at each key for Monotone Cubic: Fritsch-Carlson,
    so no overshoot. The first and last keys are flat, like Houdini: two keys make
    an S, not a straight line."""
    n = len(keys)
    secant = []
    for a, b in zip(keys, keys[1:]):
        h = b[0] - a[0]
        secant.append((b[1] - a[1]) / h if h > 1e-9 else 0.0)
    m = [0.0] * n
    for k in range(1, n - 1):
        h = keys[k + 1][0] - keys[k - 1][0]
        m[k] = (keys[k + 1][1] - keys[k - 1][1]) / h if h > 1e-9 else 0.0
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


def _bspline_curve(keys):
    """Houdini's B-spline: one uniform cubic B-spline with every key as a control
    point (so it is pulled towards the keys, not through them) and the end keys
    tripled, so it starts and ends exactly on them. (x, y) samples, x rising."""
    cps = [keys[0]] * 2 + list(keys) + [keys[-1]] * 2
    per = max(8, SAMPLES // (len(cps) - 3))
    out = []
    for s in range(len(cps) - 3):
        quad = cps[s:s + 4]
        for j in range(0 if s == 0 else 1, per + 1):
            t = j / per
            t2, t3 = t * t, t * t * t
            w = ((1 - t) ** 3 / 6, (3 * t3 - 6 * t2 + 4) / 6, (-3 * t3 + 3 * t2 + 3 * t + 1) / 6, t3 / 6)
            out.append((sum(wi * c[0] for wi, c in zip(w, quad)), sum(wi * c[1] for wi, c in zip(w, quad))))
    return out


def polyline(keys):
    """The ramp as [(x, y)] corners of straight pieces from x = 0 to 1, x rising.
    Before the first key and after the last the value is held. A Bezier ramp is
    read in point order (see `bezier_segments`), every other ramp in position order."""
    keys = [(float(p), float(v), i) for p, v, i in keys]
    if not keys:
        return [(0.0, 0.0), (1.0, 0.0)]
    if len(keys) == 1:
        return [(0.0, keys[0][1]), (1.0, keys[0][1])]
    if is_bezier(keys):
        return _bezier_polyline(keys)
    keys = _sorted(keys)
    cubic = {i for _, _, i in keys}
    monotone = _tangents(keys) if "MONOTONE_CUBIC" in cubic else None
    bspline = _bspline_curve(keys) if "BSPLINE" in cubic else None
    catmull = _catmull_rom(keys) if "CATMULL_ROM" in cubic else None
    pts = []
    for k in range(len(keys) - 1):
        (p0, v0, interp), (p1, v1, _) = keys[k], keys[k + 1]
        h = p1 - p0
        if interp != "BSPLINE":  # a B-spline stretch doesn't pass through its key
            pts.append((p0, v0))
        if h <= 1e-9:
            continue
        if interp == "CONSTANT":
            pts.append((max(p0, p1 - STEP), v0))
        elif interp in ("CATMULL_ROM", "MONOTONE_CUBIC"):
            if interp == "CATMULL_ROM":
                m0, m1 = catmull[k] / h, catmull[k + 1] / h  # per stretch -> per unit of position
            else:
                m0, m1 = monotone[k], monotone[k + 1]
            n = max(2, int(SAMPLES * h))
            pts += [(p0 + h * j / n, _hermite(p0, v0, m0, p1, v1, m1, p0 + h * j / n)) for j in range(1, n)]
        elif interp == "BSPLINE":  # the part of the whole curve over this stretch
            pts += [(x, y) for x, y in bspline if p0 <= x <= p1]
    pts.append((keys[-1][0], keys[-1][1]))
    return _finish(pts)


def _finish(pts):
    """Hold the ends out to x = 0 and 1, keep x rising, drop what a straight line covers."""
    if pts[0][0] > 0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < 1:
        pts.append((1.0, pts[-1][1]))
    out = []
    for x, y in pts:
        x = min(max(x, 0.0), 1.0)
        if out and x < out[-1][0]:
            x = out[-1][0]
        if out and abs(x - out[-1][0]) < 1e-9 and abs(y - out[-1][1]) < 1e-9:
            continue
        out.append((x, y))
    return simplify(out)


# -- Bezier ---------------------------------------------------------------------
# Like Houdini, a Bezier ramp is read in point order, not position order: from the
# first key, each key and the next three form one cubic (anchor, handle, handle,
# anchor), and that end anchor starts the next one. Missing control points repeat
# the last key, so 2 keys are a straight line and 3 a curve with one handle. Keys
# 0, 3, 6... and the last are anchors, the rest handles. BEX keeps a Bezier ramp
# whole (3k + 1 keys): handles are added and removed with their anchor.
def is_bezier(keys):
    return bool(keys) and all(k[2] == "BEZIER" for k in keys)


def is_anchor(i, n):
    return i % 3 == 0 or i == n - 1


def anchor_of(i, n):
    """The anchor a key belongs to: itself, or the anchor a handle hangs off."""
    if is_anchor(i, n):
        return i
    return i - 1 if i % 3 == 1 else min(i + 1, n - 1)


def handles_of(i, n):
    """The handles hanging off anchor i (in a whole ramp: the one before and after)."""
    return [j for j in (i - 1, i + 1) if 0 <= j < n and not is_anchor(j, n) and anchor_of(j, n) == i]


def bezier_segments(keys):
    """[(P0, P1, P2, P3)] of (x, y), in point order (see above)."""
    pts = [(float(p), float(v)) for p, v, _i in keys]
    segs = []
    for s in range(0, len(pts) - 1, 3):
        quad = pts[s:s + 4]
        segs.append(tuple(quad + [quad[-1]] * (4 - len(quad))))
    return segs


def _cubic(seg, t):
    u = 1 - t
    w = (u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t)
    return sum(wi * p[0] for wi, p in zip(w, seg)), sum(wi * p[1] for wi, p in zip(w, seg))


def _bezier_polyline(keys):
    pts = []
    for seg in bezier_segments(keys):
        pts += [_cubic(seg, j / SAMPLES) for j in range(0 if not pts else 1, SAMPLES + 1)]
    return _finish(pts)


def _split_cubic(seg, t):
    """de Casteljau: the two halves of a cubic at t, same shape."""
    def lerp(a, b):
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    p0, p1, p2, p3 = seg
    a, b, c = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
    d, e = lerp(a, b), lerp(b, c)
    f = lerp(d, e)
    return (p0, a, d, f), (f, e, c, p3)


def _t_at(seg, x):
    """The parameter where the cubic reaches x (bisection: x rises along a ramp's cubic)."""
    lo, hi = 0.0, 1.0
    rising = seg[3][0] >= seg[0][0]
    for _ in range(50):
        mid = (lo + hi) / 2
        if (_cubic(seg, mid)[0] < x) == rising:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bezier_insert(keys, x):
    """(keys, index): a new anchor at position x on the curve, the cubic under it split
    in two so the shape stays. Past the ends a straight stretch is added."""
    keys = [(float(p), float(v), "BEZIER") for p, v, _i in keys]
    B = "BEZIER"
    for s, seg in zip(range(0, len(keys) - 1, 3), bezier_segments(keys)):
        lo, hi = sorted((seg[0][0], seg[3][0]))
        if lo <= x <= hi and len(keys) >= s + 4:
            a, b = _split_cubic(seg, _t_at(seg, x))
            mid = [(px, py, B) for px, py in (a[1], a[2], a[3], b[1], b[2])]
            return keys[:s + 1] + mid + keys[s + 3:], s + 3
    if not keys:
        return [(x, 0.0, B)], 0
    first, last = keys[0], keys[-1]
    end = last if abs(x - last[0]) <= abs(x - first[0]) else first
    y = end[1]
    third = [(end[0] + (x - end[0]) * f, y, B) for f in (1 / 3, 2 / 3)]
    if end is last:
        return keys + third + [(x, y, B)], len(keys) + 2
    return [(x, y, B)] + third[::-1] + keys, 0


def bezier_delete(keys, doomed):
    """`keys` without the anchors in `doomed` and their handles (handles in `doomed`
    are ignored). At least one anchor stays."""
    keys = list(keys)
    for i in sorted({anchor_of(i, len(keys)) for i in doomed if is_anchor(i, len(keys))}, reverse=True):
        n = len(keys)
        if n <= 1:
            break
        span = (0, 3) if i == 0 else (n - 3, n) if i == n - 1 else (i - 1, i + 2)
        span = (max(0, span[0]), min(n, span[1]))
        if span[1] - span[0] >= n:
            span = (1, n) if i == 0 else (0, n - 1)
        del keys[span[0]:span[1]]
    return keys


def to_bezier(keys):
    """A Bezier ramp of the same shape: the keys become anchors (at the curve's value
    there) and each stretch gets two handles a third of the way in, along the
    curve's slope. Constant steps and B-splines are only approximated."""
    keys = _sorted(keys)
    B = "BEZIER"
    if len(keys) < 2:
        return [(p, v, B) for p, v, _i in keys]
    line = polyline(keys)
    monotone, catmull = _tangents(keys), _catmull_rom(keys)
    bspline = _bspline_curve(keys)

    def bspline_at(x):
        for (x0, y0), (x1, y1) in zip(bspline, bspline[1:]):
            if x0 <= x <= x1:
                return y0 if x1 - x0 < 1e-12 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return bspline[-1][1] if x > bspline[-1][0] else bspline[0][1]

    out = [(keys[0][0], evaluate(keys, keys[0][0], line), B)]
    for k in range(len(keys) - 1):
        (p0, v0, interp), (p1, v1, _) = keys[k], keys[k + 1]
        h = p1 - p0
        y0, y1 = (bspline_at(p0), bspline_at(p1)) if interp == "BSPLINE" else (v0, v1)
        if interp == "CONSTANT":
            h1, h2 = (p0 + h * 2 / 3, v0), (p1, v0)
        else:
            if interp == "LINEAR" or h <= 1e-9:
                m0 = m1 = (y1 - y0) / h if h > 1e-9 else 0.0
            elif interp == "CATMULL_ROM":
                m0, m1 = catmull[k] / h, catmull[k + 1] / h
            elif interp == "MONOTONE_CUBIC":
                m0, m1 = monotone[k], monotone[k + 1]
            else:  # B-spline: the slope of the sampled curve
                e = h / 50
                m0, m1 = (bspline_at(p0 + e) - y0) / e, (y1 - bspline_at(p1 - e)) / e
            h1, h2 = (p0 + h / 3, y0 + m0 * h / 3), (p1 - h / 3, y1 - m1 * h / 3)
        out += [(h1[0], h1[1], B), (h2[0], h2[1], B), (p1, evaluate(keys, p1, line) if k + 1 < len(keys) - 1 else y1, B)]
    return out


def from_bezier(keys, mode):
    """The anchors of a Bezier ramp, as keys of `mode`."""
    return [(p, v, mode) for i, (p, v, _i) in enumerate(keys) if is_anchor(i, len(keys))]


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
    belongs to the key at its other end. A Bezier ramp keeps its point order, reversed."""
    if is_bezier(keys):
        return [(1.0 - p, v, i) for p, v, i in reversed(keys)]
    keys = _sorted(keys)
    n = len(keys)
    return [(1.0 - keys[k][0], keys[k][1], keys[k - 1][2] if k > 0 else keys[-1][2]) for k in range(n - 1, -1, -1)]


def invert(keys):
    """Flip upside down (value -> 1 - value)."""
    return [(p, 1.0 - v, i) for p, v, i in (keys if is_bezier(keys) else _sorted(keys))]


# The cubic Bezier closest to a quarter circle has its handles this far along the
# tangents, as a fraction of the radius: 4(sqrt 2 - 1) / 3 (within 0.03% of the circle).
KAPPA = 4 * (math.sqrt(2) - 1) / 3

# Houdini's default presets (its houdini/ramp/float.ramp), but Round and Sharp.
PRESETS = {
    "Flat": ((0.0, 1.0, "CONSTANT"),),
    "Hill": ((0.0, 0.0, "MONOTONE_CUBIC"), (0.5, 1.0, "MONOTONE_CUBIC"), (1.0, 0.0, "MONOTONE_CUBIC")),
    "Linear": ((0.0, 0.0, "LINEAR"), (1.0, 1.0, "LINEAR")),
    # Round and Sharp: quarter circles, each the other turned half around (see KAPPA).
    "Round": ((0.0, 0.0, "BEZIER"), (KAPPA, 0.0, "BEZIER"), (1.0, 1.0 - KAPPA, "BEZIER"), (1.0, 1.0, "BEZIER")),
    "Sharp": ((0.0, 0.0, "BEZIER"), (0.0, KAPPA, "BEZIER"), (1.0 - KAPPA, 1.0, "BEZIER"), (1.0, 1.0, "BEZIER")),
    "Smooth": ((0.0, 0.0, "MONOTONE_CUBIC"), (1.0, 1.0, "MONOTONE_CUBIC")),  # flat ends make the S
    "Steps": ((0.0, 0.0, "CONSTANT"), (0.25, 0.3333, "CONSTANT"), (0.5, 0.6666, "CONSTANT"), (0.75, 1.0, "CONSTANT")),
    "Valley": ((0.0, 1.0, "MONOTONE_CUBIC"), (0.5, 0.0, "MONOTONE_CUBIC"), (1.0, 1.0, "MONOTONE_CUBIC")),
}
DEFAULT_KEYS = PRESETS["Linear"]  # a new ramp is a straight line, like Houdini's
