"""Icons for the ramp widget that Blender doesn't have, drawn in its flat icon style
(light strokes, anti-aliased) and handed to the UI as preview icons (`icon_value`).

Houdini's ramp is the model: an interpolation menu whose icon is the current mode's
shape, flip buttons (drawn like Blender's double arrows, across a mirror bar), small
fold / unfold chevrons, and the preset thumbnails (`thumbnail()`, area only). `raster()` and
`thumbnail()` are plain Python; only `icon()` needs bpy.
"""

import math

from . import ramp as R

SIZE = 64                   # icon pixels: Blender scales them down, so they stay sharp on Retina and zoomed in
COLOR = (0.88, 0.88, 0.88)  # like Blender's dark-theme icons
LINE = 0.07                 # stroke width, as a fraction of the icon
ZOOM = 1.15                 # every shape grows this much around the icon's centre

_previews = None   # bpy.utils.previews collection, made on first use


# -- rasterizer ----------------------------------------------------------------
def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1.0)
    t = min(1.0, max(0.0, t))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def coverage(shapes, size=SIZE, zoom=ZOOM, line=LINE):
    """Alpha per pixel (rows bottom-up) of `shapes`; see `raster`."""
    alpha = [0.0] * (size * size)
    width = line * size

    def cover(x0, y0, x1, y1, fn):
        for py in range(max(0, int(y0)), min(size, int(math.ceil(y1)))):
            for px in range(max(0, int(x0)), min(size, int(math.ceil(x1)))):
                a = fn(px + 0.5, py + 0.5)
                if a > alpha[py * size + px]:
                    alpha[py * size + px] = a

    for kind, data in shapes:
        pts = [((0.5 + (x - 0.5) * zoom) * size, (0.5 + (y - 0.5) * zoom) * size) for x, y in data]
        if kind == "line":
            r = width / 2
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                cover(min(ax, bx) - r - 1, min(ay, by) - r - 1, max(ax, bx) + r + 1, max(ay, by) + r + 1,
                      lambda x, y, a=(ax, ay, bx, by): min(1.0, max(0.0, r - _seg_dist(x, y, *a) + 0.5)))
        else:
            (ax, ay), (bx, by), (cx, cy) = pts
            if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0:  # make it counter-clockwise
                (bx, by), (cx, cy) = (cx, cy), (bx, by)
            edges = []
            for (ex, ey), (fx, fy) in (((ax, ay), (bx, by)), ((bx, by), (cx, cy)), ((cx, cy), (ax, ay))):
                n = math.hypot(fx - ex, fy - ey) or 1.0
                edges.append((ex, ey, (fy - ey) / n, -(fx - ex) / n))  # outward normal

            def inside(x, y):
                d = max((x - ex) * nx + (y - ey) * ny for ex, ey, nx, ny in edges)
                return min(1.0, max(0.0, 0.5 - d))
            cover(min(ax, bx, cx) - 1, min(ay, by, cy) - 1, max(ax, bx, cx) + 1, max(ay, by, cy) + 1, inside)

    return alpha


def raster(shapes, size=SIZE, color=COLOR):
    """Premultiplied RGBA floats, rows bottom-up, for `shapes` in 0-1 coordinates (y up):
    ("line", [(x, y), ...]) is a stroke `LINE` wide with round ends, ("tri", (a, b, c))
    a filled triangle. Edges are anti-aliased from the pixel's distance to the shape."""
    pixels = []
    for a in coverage(shapes, size):  # premultiplied: Blender draws preview icons that way
        pixels += (color[0] * a, color[1] * a, color[2] * a, a)
    return pixels


THUMB = 128   # preset thumbnails (shown large in the preset popup)
THUMB_COLORS = {"back": (0.14, 0.14, 0.14), "fill": (0.62, 0.62, 0.61)}


def thumbnail(keys, size=THUMB):
    """A ramp drawn as the area under it (no line), anti-aliased: opaque RGBA floats,
    rows bottom-up. Each pixel is sampled across its width too, so steep parts stay smooth."""
    line = R.polyline(keys)
    sub = 4  # samples across a pixel's width
    heights = [R.evaluate(keys, (c * sub + i + 0.5) / (size * sub), line) * size for c in range(size) for i in range(sub)]
    back, fill = THUMB_COLORS["back"], THUMB_COLORS["fill"]
    pixels = []
    for row in range(size):
        for col in range(size):
            f = sum(min(1.0, max(0.0, h - row)) for h in heights[col * sub:(col + 1) * sub]) / sub
            pixels += (*[b + (fl - b) * f for b, fl in zip(back, fill)], 1.0)
    return pixels


# -- shapes --------------------------------------------------------------------
def _arrowhead(tip, toward, length=0.22, width=0.3):
    """A filled triangle with its point at `tip`, pointing away from `toward`."""
    dx, dy = tip[0] - toward[0], tip[1] - toward[1]
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    bx, by = tip[0] - dx * length, tip[1] - dy * length
    return ("tri", (tip, (bx - dy * width / 2, by + dx * width / 2), (bx + dy * width / 2, by - dx * width / 2)))


def flip_shapes(vertical=False):
    """A mirror bar crossed by a straight double arrow with solid heads, like
    Blender's ARROW_LEFTRIGHT. The default flips left to right (a vertical bar);
    `vertical` is the same turned a quarter."""
    shapes = [("line", [(0.5, 0.2), (0.5, 0.8)]), ("line", [(0.24, 0.5), (0.76, 0.5)]),
              _arrowhead((0.08, 0.5), (0.5, 0.5)), _arrowhead((0.92, 0.5), (0.5, 0.5))]
    if vertical:
        shapes = [(k, [(y, x) for x, y in pts]) for k, pts in shapes]
    return shapes


def fold_shapes(fold):
    """Chevrons pointing in (fold the graph to a strip) or out (unfold it)."""
    if fold:
        return [("line", [(0.3, 0.84), (0.5, 0.64), (0.7, 0.84)]), ("line", [(0.3, 0.16), (0.5, 0.36), (0.7, 0.16)])]
    return [("line", [(0.3, 0.64), (0.5, 0.84), (0.7, 0.64)]), ("line", [(0.3, 0.36), (0.5, 0.16), (0.7, 0.36)])]


# Like Houdini: each mode drawn as a two-key ramp from (0, 0) to (1, 1).
_SHAPE = ((0.0, 0.0), (1.0, 1.0))


def interpolation_shapes(mode):
    keys = [(x, y, mode) for x, y in _SHAPE]
    line = R.polyline(keys)
    m, s = 0.16, 0.68  # margin and span of the 0-1 square inside the icon
    if mode == "CONSTANT":
        pts = [(m + s * x, m + s * y) for x, y in line]
    else:
        pts = [(m + s * x, m + s * R.evaluate(keys, x, line)) for x in (i / 24 for i in range(25))]
    return [("line", pts)]


SHAPES = {
    "FLIP_X": lambda: flip_shapes(False),
    "FLIP_Y": lambda: flip_shapes(True),
    "FOLD": lambda: fold_shapes(True),
    "UNFOLD": lambda: fold_shapes(False),
    **{"INTERP_" + mode: (lambda mode=mode: interpolation_shapes(mode)) for mode, _l, _d in R.INTERPOLATIONS},
}


# -- Blender side --------------------------------------------------------------
def icon(name):
    """The icon id of SHAPES[name] (drawn once per session)."""
    global _previews
    if _previews is None:
        import bpy.utils.previews
        _previews = bpy.utils.previews.new()
    pv = _previews.get(name)
    if pv is None:
        pv = _previews.new(name)
        pixels = raster(SHAPES[name]())
        pv.icon_size = pv.image_size = (SIZE, SIZE)
        pv.icon_pixels_float = pixels
        pv.image_pixels_float = pixels
    return pv.icon_id


def unregister():
    global _previews
    if _previews is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_previews)
        _previews = None
