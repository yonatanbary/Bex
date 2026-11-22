"""BEX compiler: AST -> a small graph IR whose nodes map 1:1 onto Blender nodes.

The script is executed *symbolically*: every variable and attribute holds a
`Val`, which is either a compile-time constant or an output of an `IRNode`.
Constants are folded and `if` becomes a per-element Switch. Loops are unrolled
when short or constant, else they become a Repeat Zone (see the loops section).
Variables work like Python's: assigning creates them, and they are visible until
the end of the script.
"""

import colorsys
import itertools
import math
import re
from dataclasses import dataclass, field

from .lexer import _PREFIX_TYPES, ALIASES, BexError
from .parser import N, parse, parse_group

MAX_LOOP_ITERATIONS = 1024  # longest loop run at compile time
UNROLL_MAX = 8  # loops this short are unrolled even when they make nodes
MAX_ZONE_ITERATIONS = 100_000  # a Repeat Zone never runs more often than this
WHILE_CAP = 1000  # iterations of a while loop whose end differs per element (inside another loop)
TEMP_PREFIX = ".bex"  # hidden attributes a chunked loop keeps its values in; removed at the end
CHUNK_FIRST, CHUNK_GROWTH = 32, 1.5
MAX_CALL_DEPTH = 50  # nested calls of your own functions (recursion has to end while compiling)
MAX_CALLS = 10_000  # calls of your own functions in one script  # chunk k of a loop that can end early runs floor(32 * 1.5^k) times
RANK = {"bool": 0, "int": 1, "float": 2, "vector": 3, "quaternion": 4, "matrix": 5}
# Types that never convert to or from another (a quaternion is a Blender Rotation, a matrix a 4x4 Matrix).
OPAQUE = ("quaternion", "matrix")
SIZES = {"vector": 3, "quaternion": 4}
PREFIXES = {"vector": "v", "quaternion": "p", "matrix": "4"}  # the prefix that gives an attribute this type  # components: v.x .y .z, a quaternion's .w too (Houdini order x, y, z, w)


class IRNode:
    """One future Blender node. Compared by identity; deduplicated by the compiler."""

    __slots__ = ("kind", "params", "inputs", "line")

    def __init__(self, kind, params, inputs, line=0):
        self.kind = kind
        self.params = params
        self.inputs = inputs
        self.line = line  # source line of the statement that first needed this node

    def __repr__(self):
        return f"IRNode({self.kind}, {self.params})"


@dataclass(frozen=True)
class Val:
    type: str  # float | int | vector | bool | quaternion | matrix
    # python value when known at compile time: a vector is (x, y, z), a quaternion (x, y, z, w),
    # a matrix 16 floats in Blender's order (column by column, translation in the last column)
    const: object = None
    node: object = None  # IRNode otherwise
    out: int = 0  # which output of the node

    @property
    def is_const(self):
        return self.node is None


@dataclass
class Program:
    params: list = field(default_factory=list)  # [(name, type, default)] -> node inputs
    writes: list = field(default_factory=list)  # [(attribute, type, Val)] in script order
    source: list = field(default_factory=list)  # script lines, used to label the generated nodes
    pending: list = field(default_factory=list)  # ch*() names held back on the line being edited
    ramps: list = field(default_factory=list)  # chramp() names, first-use order
    shared_ramps: set = field(default_factory=set)  # ramps read at more than one position
    reads: list = field(default_factory=list)  # [(attribute, type)] read from the incoming geometry
    group: object = None  # Val (bool) choosing the elements to write, None = all
    group_source: str = ""  # the Group expression, used to label its nodes
    warnings: list = field(default_factory=list)  # problems that don't stop the build (a bad Group)
    inputs: list = field(default_factory=list)  # extra geometry inputs the script reads (1-3), sorted
    stores: list = field(default_factory=list)  # [Store] in geometry-chain order (plan_stores)
    captures: list = field(default_factory=list)  # [(label, type, Val)] to capture on the incoming geometry
    preludes: list = field(default_factory=list)  # chunk_end IRNodes: chunked loops, run on the geometry first
    geo_ops: list = field(default_factory=list)  # [GeoOp]: added / removed elements, writes to other elements


@dataclass
class GeoOp:
    """One call site of addpoint() / addprim() / addvertex() / remove*() / set*attrib().
    Every element of the run-over domain where `mask` is true makes one record;
    `values` are fields of that element (read on the incoming geometry)."""
    kind: str  # addpoint | vertex | remove | set
    mask: Val
    values: dict  # name -> Val: pos or src (addpoint), prim + pt (vertex), target (+ value for set)
    info: dict  # addpoint: src; vertex: ptype (poly / polyline); remove: domain, mode; set: domain, name, type, mode
    line: int = 0


@dataclass
class Store:
    """One write node: Store Named Attribute, or Set Position for `P`."""
    name: str
    type: str
    value: Val  # the value to write (Set Position: the Offset when `offset`)
    selection: object = None  # bool Val: which elements are written, None = all
    conds: list = field(default_factory=list)  # the bool Vals ANDed into `selection` (Group first)
    offset: bool = False  # Set Position: `value` goes into Offset, not Position
    storage: str = None  # the attribute's data type when it isn't the type's own (Cd: FLOAT_COLOR)


# Built-in attributes that read from a field node instead of a named attribute.
# name -> (type, input kind, output index)
BUILTINS = {
    "P": ("vector", "position", 0),
    "N": ("vector", "normal", 0),
    "ptnum": ("int", "index", 0),
    "elemnum": ("int", "index", 0),
    "primnum": ("int", "index", 0),
    "curvenum": ("int", "index", 0),
    "vtxnum": ("int", "index", 0),
    "edgenum": ("int", "index", 0),
    "index": ("int", "index", 0),
    "id": ("int", "id", 0),
    "Time": ("float", "time", 0),
    "Frame": ("float", "time", 1),
    "curveu": ("float", "spline_parameter", 0),  # 0-1 along each curve (Spline Parameter's Factor)
    "curvelength": ("float", "spline_parameter", 1),  # distance along the curve from its start
    "tangent": ("vector", "tangent", 0),  # direction along the curve
    "area": ("float", "face_area", 0),
    "island": ("int", "mesh_island", 0),  # which connected piece of the mesh
    "edgeangle": ("float", "edge_angle", 0),  # angle between an edge's two faces, 0 when flat
    "cyclic": ("bool", "spline_cyclic", 0),  # the curve is closed
    "curveptnum": ("int", "curve_of_point", 1),  # a curve point's number within its curve: 0, 1, 2...
    "TimeInc": ("float", "time", 0),  # seconds per frame (read_original computes it)
}
# Index built-ins that depend on the Run Over domain, like Formula to Nodes / Houdini:
# name -> {domain: (input kind, output)}. @elemnum / @index are always the element's index.
DOMAIN_INDEX = {
    "ptnum": {"POINT": ("index", 0), "INSTANCE": ("index", 0), "CORNER": ("vertex_of_corner", 0)},
    "primnum": {"FACE": ("index", 0), "CURVE": ("index", 0), "CORNER": ("face_of_corner", 0)},
    "curvenum": {"CURVE": ("index", 0), "POINT": ("curve_of_point", 0)},
    "vtxnum": {"CORNER": ("index", 0)},
    "edgenum": {"EDGE": ("index", 0)},
}
INDEX_HINTS = {
    "ptnum": "@ptnum is a point's number; this runs over {d}. Use @elemnum for this element's index",
    "primnum": "@primnum is a face's or curve's number, and a point can belong to several faces. "
               "On curve points use @curvenum, or set Run Over to Faces or Curves",
    "curvenum": "@curvenum is the curve a point belongs to; run over Points or Curves",
    "vtxnum": "@vtxnum is a face corner's number; run over Corners",
    "edgenum": "@edgenum is an edge's number; run over Edges",
}

# Element counts of the incoming geometry (Houdini's @numpt...): name -> domain
COUNT_ATTRS = {"numpt": "POINT", "numprim": "FACE", "numvtx": "CORNER"}
READ_ONLY = {"ptnum", "elemnum", "primnum", "curvenum", "vtxnum", "edgenum", "index", "Time", "Frame",
             "curveu", "curvelength", "tangent", "area", "island", "edgeangle", "cyclic", "curveptnum",
             "TimeInc"} | set(COUNT_ATTRS)
KNOWN_TYPES = {"velocity": "vector", "Cd": "vector", "up": "vector", "radius": "float", "UVMap": "vector",
               "scale": "vector", "orient": "quaternion", "rot": "quaternion", "transform": "matrix",
               "Alpha": "float"}
# Attributes stored as another Blender data type than their BEX type, so the rest of Blender
# sees them for what they are: Cd as a colour attribute, the UV map as 2D vectors.
STORAGE = {"Cd": "FLOAT_COLOR", "UVMap": "FLOAT2"}


def _sign(x):
    return (x > 0) - (x < 0)


def _safe_div(a, b):
    return a / b if b else 0.0


def _floor_mod(a, b):
    return a - math.floor(a / b) * b if b else 0


def _wrap(x, hi, lo):  # argument order of the Math node's Wrap (Value, Max, Min)
    r = hi - lo
    return x - math.floor((x - lo) / r) * r if r else lo


def _map_range(x, a, b, c, d, interp, clamp):
    t = (x - a) / (b - a) if b != a else 0.0
    if clamp or interp != "LINEAR":
        t = min(max(t, 0.0), 1.0)
    if interp == "SMOOTHSTEP":
        t = t * t * (3 - 2 * t)
    elif interp == "SMOOTHERSTEP":
        t = t * t * t * (t * (t * 6 - 15) + 10)
    return c + t * (d - c)


# name -> (Math node operation, python implementation)
MATH1 = {
    "sin": ("SINE", math.sin),
    "cos": ("COSINE", math.cos),
    "tan": ("TANGENT", math.tan),
    "asin": ("ARCSINE", math.asin),
    "acos": ("ARCCOSINE", math.acos),
    "atan": ("ARCTANGENT", math.atan),
    "sinh": ("SINH", math.sinh),
    "cosh": ("COSH", math.cosh),
    "tanh": ("TANH", math.tanh),
    "sqrt": ("SQRT", lambda x: math.sqrt(x) if x > 0 else 0.0),
    "exp": ("EXPONENT", math.exp),
    "abs": ("ABSOLUTE", abs),
    "floor": ("FLOOR", math.floor),
    "ceil": ("CEIL", math.ceil),
    "round": ("ROUND", lambda x: math.floor(x + 0.5)),
    "trunc": ("TRUNC", math.trunc),
    "frac": ("FRACT", lambda x: x - math.floor(x)),
    "sign": ("SIGN", _sign),
    "rint": ("ROUND", lambda x: math.floor(x + 0.5)),
    "radians": ("RADIANS", math.radians),
    "degrees": ("DEGREES", math.degrees),
}
MATH2 = {
    "pow": ("POWER", math.pow),
    "atan2": ("ARCTAN2", math.atan2),
    "min": ("MINIMUM", min),
    "max": ("MAXIMUM", max),
    "mod": ("FLOORED_MODULO", _floor_mod),
    "fmod": ("FLOORED_MODULO", _floor_mod),
    "snap": ("SNAP", lambda x, s: math.floor(x / s) * s if s else 0.0),
    "pingpong": ("PINGPONG", lambda x, s: abs(_floor_mod(x - s, 2 * s) - s) if s else 0.0),
}
# Math operations the Vector Math node also offers (others fall back to per-component).
VMATH1 = {"ABSOLUTE": "ABSOLUTE", "FLOOR": "FLOOR", "CEIL": "CEIL", "ROUND": "ROUND", "FRACT": "FRACTION",
          "SIGN": "SIGN", "SINE": "SINE", "COSINE": "COSINE", "TANGENT": "TANGENT"}
VMATH2 = {"MINIMUM": "MINIMUM", "MAXIMUM": "MAXIMUM", "POWER": "POWER", "SNAP": "SNAP"}
IMATH2 = {"MINIMUM": "MINIMUM", "MAXIMUM": "MAXIMUM"}

# -- quaternions (x, y, z, w) and matrices (16 floats, column by column) for constant folding
_IDENTITY = tuple(float(c == r) for c in range(4) for r in range(4))


def _qnorm(q):
    """Blender's rotations are unit quaternions; a zero one is no rotation."""
    n = math.sqrt(sum(c * c for c in q))
    return tuple(float(c / n) for c in q) if n > 1e-12 else (0.0, 0.0, 0.0, 1.0)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _qrotate(q, v):
    u, w = q[:3], q[3]
    t = tuple(2 * c for c in _cross(u, v))
    c = _cross(u, t)
    return tuple(v[i] + w * t[i] + c[i] for i in range(3))


def _qaxis(axis, angle):
    n = math.sqrt(sum(c * c for c in axis))
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    s = math.sin(angle / 2) / n
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle / 2))


def _qeuler(angles, order):
    q = (0.0, 0.0, 0.0, 1.0)
    for axis in order:  # the first axis turns first
        k = "XYZ".index(axis)
        q = _qmul(_qaxis(tuple(float(i == k) for i in range(3)), angles[k]), q)
    return q


def _qinv(q):
    return (-q[0], -q[1], -q[2], q[3])


def _mat_mul(a, b):
    """Blender's A @ B."""
    return tuple(sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4))


def _mat_point(m, v):
    return tuple(sum(m[c * 4 + r] * v[c] for c in range(3)) + m[12 + r] for r in range(3))


def _mat_trs(t, q, s):
    cols = [_qrotate(q, tuple(s[k] * (i == k) for i in range(3))) for k in range(3)]
    return tuple(x for col in cols for x in (*col, 0.0)) + (*t, 1.0)


def _floor_div(a, b):
    return math.floor(a / b) if b else 0


def _safe_pow(a, b):
    try:
        r = a ** b
    except ZeroDivisionError:
        return 0.0
    return r if not isinstance(r, complex) else 0.0


# Python semantics: `/` always gives a float, `//` floors, `%` is a floored modulo.
ARITH = {
    "+": ("ADD", lambda a, b: a + b),
    "-": ("SUBTRACT", lambda a, b: a - b),
    "*": ("MULTIPLY", lambda a, b: a * b),
    "/": ("DIVIDE", _safe_div),
    "//": ("DIVIDE_FLOOR", _floor_div),
    "%": ("FLOORED_MODULO", _floor_mod),
    "**": ("POWER", _safe_pow),
}
CONSTANTS = {"XFORM_SRT": 0, "E": math.e, "M_E": math.e, "PI": math.pi, "M_PI": math.pi, "pi": math.pi, "TAU": math.tau, "tau": math.tau,
             # rotation orders, as in VEX (eulertoquaternion(r, XFORM_ZYX)): the first axis turns first
             "XFORM_XYZ": 0, "XFORM_XZY": 1, "XFORM_YXZ": 2, "XFORM_YZX": 3, "XFORM_ZXY": 4, "XFORM_ZYX": 5}
EULER_ORDERS = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
COMPARE = {
    "<": ("LESS_THAN", lambda a, b: a < b),
    "<=": ("LESS_EQUAL", lambda a, b: a <= b),
    ">": ("GREATER_THAN", lambda a, b: a > b),
    ">=": ("GREATER_EQUAL", lambda a, b: a >= b),
    "==": ("EQUAL", lambda a, b: a == b),
    "!=": ("NOT_EQUAL", lambda a, b: a != b),
}
CHANNELS = {"chf": "float", "chi": "int", "chv": "vector", "chb": "bool"}
RAMPS = {"chramp": "float"}  # a ramp on the node (a Float Curve), looked up at a 0-1 position
# Bounding box of the geometry that entered the node. The optional first argument
# is the input number, as in VEX (only input 0 exists).
BBOX = ("getbbox_min", "getbbox_max", "getbbox_size", "getbbox_center", "relbbox")
# Every function name Compiler.call accepts (used by the editor's highlighter). Keep in sync with call().
# Geometry inputs, like a wrangle's four: 0 is the incoming geometry, 1-3 are the
# node's "Input N" sockets (always there).
MAX_INPUT = 3
SAMPLE_FUNCS = {"point": "POINT", "prim": "FACE", "vertex": "CORNER"}  # point(1, "P", i) -> domain
COUNT_FUNCS = {"npoints": "POINT", "nprims": "FACE", "nprimitives": "FACE", "nvertices": "CORNER",
               "nedges": "EDGE", "ncurves": "CURVE"}  # npoints(1)
# Mesh topology on an input: name -> (node, output, index socket, domain of the index)
TOPOLOGY = {
    "primvertexcount": ("GeometryNodeCornersOfFace", "Total", "Face Index", "FACE"),
    "vertexpoint": ("GeometryNodeVertexOfCorner", "Vertex Index", "Corner Index", "CORNER"),
    "vertexprim": ("GeometryNodeFaceOfCorner", "Face Index", "Corner Index", "CORNER"),
    "vertexprimindex": ("GeometryNodeFaceOfCorner", "Index in Face", "Corner Index", "CORNER"),
    "neighbourcount": ("GeometryNodeEdgesOfVertex", "Total", "Vertex Index", "POINT"),
}
# Every output of the topology nodes, in socket order (an IR node's `out` indexes these).
NODE_OUTS = {
    "GeometryNodeCornersOfFace": ("Corner Index", "Total"),
    "GeometryNodeEdgesOfVertex": ("Edge Index", "Total"),
    "GeometryNodeVertexOfCorner": ("Vertex Index",),
    "GeometryNodeFaceOfCorner": ("Face Index", "Index in Face"),
    "GeometryNodeInputMeshEdgeVertices": ("Vertex Index 1", "Vertex Index 2"),
}
# Quaternion and matrix functions (Compiler.transform_call)
TRANSFORM_FUNCS = ("quaternion", "eulertoquaternion", "quaterniontoeuler", "qrotate", "qmultiply", "qinvert",
                   "slerp", "dihedral", "ident", "maketransform", "invert", "transpose", "determinant",
                   "translate", "rotate", "scale", "lookat", "cracktransform")
MATRIX_OUTS = tuple(f"Column {c} Row {r}" for c in range(1, 5) for r in range(1, 5))  # Separate Matrix
# On the Instance domain these read and write the instance's own transform (instance_transform)
INSTANCE_ATTRS = {"orient": ("quaternion", "instance_rotation"), "scale": ("vector", "instance_scale"),
                  "transform": ("matrix", "instance_transform")}
# Noise (Compiler.noise_call). name -> Noise Texture type; every one takes the NOISE_OPTS keywords.
NOISE_FUNCS = {"noise": "FBM", "vnoise": "FBM", "snoise": "FBM", "flownoise": "FBM",
               "ridgednoise": "RIDGED_MULTIFRACTAL", "hybridnoise": "HYBRID_MULTIFRACTAL",
               "heteronoise": "HETERO_TERRAIN", "multinoise": "MULTIFRACTAL"}
NOISE_OPTS = ("freq", "offset", "amp", "octaves", "rough", "lacunarity", "distortion")
FRACTAL_OPTS = ("gain", "ridge")  # the multifractal types' Gain and Offset
# Voronoi: name -> (feature, output). wnoise() also fills seed, f1, f2 out arguments, like VEX.
VORONOI_FUNCS = {"voronoi": ("F1", "Distance"), "voronoif2": ("F2", "Distance"),
                 "voronoiedge": ("DISTANCE_TO_EDGE", "Distance"), "voronoicell": ("F1", "Color"),
                 "voronoipos": ("F1", "Position"), "wnoise": ("F1", "Distance")}
VORONOI_OPTS = ("freq", "offset", "amp", "jitter", "metric", "exponent", "octaves", "rough", "lacunarity")
VORONOI_METRICS = {"euclidean": "EUCLIDEAN", "manhattan": "MANHATTAN", "chebyshev": "CHEBYCHEV",
                   "chebychev": "CHEBYCHEV", "minkowski": "MINKOWSKI"}
GABOR_OPTS = ("freq", "offset", "amp", "stripes", "anisotropy", "direction")
SAMPLE_DIRS = ("sample_direction_uniform", "sample_sphere_uniform", "sample_hemisphere",
               "sample_circle_uniform", "sample_circle_edge_uniform", "sample_direction_cone")
# Built-in functions that take keyword options: name -> allowed keywords
FRACTAL_SOCKETS = {"RIDGED_MULTIFRACTAL": FRACTAL_OPTS, "HYBRID_MULTIFRACTAL": FRACTAL_OPTS, "HETERO_TERRAIN": ("ridge",)}
KEYWORDS = {**{n: NOISE_OPTS + FRACTAL_SOCKETS.get(t, ()) for n, t in NOISE_FUNCS.items()},
            "curlnoise": NOISE_OPTS, **{n: VORONOI_OPTS for n in VORONOI_FUNCS}, "gabor": GABOR_OPTS}
# Making and removing elements, writing to other elements (Compiler.geo_call)
GEO_FUNCS = ("addpoint", "addprim", "addvertex", "removepoint", "removeprim", "setpointattrib", "setprimattrib",
             "setpointgroup", "setprimgroup", "setvertexattrib", "setdetailattrib")
GEO_UNROLL_MAX = 64  # a loop making elements is unrolled up to this many passes
PRIM_TYPES = {"poly": "poly", "polygon": "poly", "polyline": "polyline"}
SET_MODES = ("set", "add", "min", "max")
# Statistics over the run-over domain, optionally per group (Compiler.aggregate): name -> (node, output)
AGGREGATES = {"mean": ("GeometryNodeFieldAverage", "Mean"), "median": ("GeometryNodeFieldAverage", "Median"),
              "minof": ("GeometryNodeFieldMinAndMax", "Min"), "maxof": ("GeometryNodeFieldMinAndMax", "Max"),
              "stddev": ("GeometryNodeFieldVariance", "Standard Deviation"),
              "variance": ("GeometryNodeFieldVariance", "Variance"),
              "sumof": ("GeometryNodeAccumulateField", "Total"), "accumulate": ("GeometryNodeAccumulateField", "Leading")}
AGGREGATE_OUTS = {"GeometryNodeFieldAverage": ("Mean", "Median"), "GeometryNodeFieldMinAndMax": ("Min", "Max"),
                  "GeometryNodeFieldVariance": ("Standard Deviation", "Variance"),
                  "GeometryNodeAccumulateField": ("Leading", "Trailing", "Total")}
INT_AGGREGATES = ("GeometryNodeFieldMinAndMax", "GeometryNodeAccumulateField")  # also work on whole numbers
DATA_TYPES = {"float": "FLOAT", "int": "INT", "bool": "BOOLEAN", "vector": "FLOAT_VECTOR",
              "quaternion": "QUATERNION", "matrix": "FLOAT4X4"}
DOMAIN_NAMES = {"point": "POINT", "edge": "EDGE", "face": "FACE", "prim": "FACE", "primitive": "FACE",
                "corner": "CORNER", "vertex": "CORNER", "curve": "CURVE", "instance": "INSTANCE"}
COLOR_MODES = {"hsvtorgb": "HSV", "rgbtohsv": "HSV", "hsltorgb": "HSL", "rgbtohsl": "HSL"}
LUMINANCE = (0.2126, 0.7152, 0.0722)  # Rec. 709, for linear colours
# Socket names count from 1 like a wrangle's inputs: input 1 is the socket "Input 2".
INPUT_SOCKETS = tuple(f"Input {n + 1}" for n in range(1, MAX_INPUT + 1))
OPINPUT_RE = re.compile(r"opinput(\d)_(\w+)$")  # @opinput1_P: same element number on input 1

FUNCTION_NAMES = frozenset(MATH1) | frozenset(MATH2) | frozenset(CHANNELS) | frozenset(RAMPS) | frozenset({
    "log", "log10", "clamp", "lerp", "mix", "fit", "efit", "fit01", "fit11", "smoothstep",
    "length", "normalize", "dot", "cross", "distance", "set", "vector", "float", "bool", "int",
    "noise", "vnoise", "rand", "vrand", "smooth", "smootherstep", "wrap", "range",
    "nearpoint", "xyzdist", "minpos", "intersect", "primpoint", "neighbour",
    "reflect", "refract", "faceforward", "length2", "distance2", "avg", "invlerp",
    "luminance", "blackbody", "curlnoise", "gabor", "nrandom",
    "countof", "blur", "at", "promote", *GEO_FUNCS, "primuv", "uvsample", "isnan", "isfinite", "len", "getcomp", "setcomp",
}) | frozenset(AGGREGATES) | frozenset(NOISE_FUNCS) | frozenset(VORONOI_FUNCS) | frozenset(SAMPLE_DIRS) | frozenset(TRANSFORM_FUNCS) | frozenset(BBOX) | frozenset(SAMPLE_FUNCS) | frozenset(COUNT_FUNCS) | frozenset(TOPOLOGY) | frozenset(COLOR_MODES)

# Signature help shown while typing a call: name -> (parameters, one-line summary).
# BEX forms, not VEX: no types or overloads; an optional parameter is "[name]".
# Keep the summaries short and plain: what it gives you, in a few words.
SIGNATURES = {
    # Math
    "sin": (("angle",), "Sine of an angle in radians."),
    "cos": (("angle",), "Cosine of an angle in radians."),
    "tan": (("angle",), "Tangent of an angle in radians."),
    "asin": (("value",), "Inverse sine, in radians."),
    "acos": (("value",), "Inverse cosine, in radians."),
    "atan": (("value",), "Inverse tangent, in radians."),
    "atan2": (("y", "x"), "Angle of the point (x, y), in radians."),
    "sinh": (("value",), "Hyperbolic sine."),
    "cosh": (("value",), "Hyperbolic cosine."),
    "tanh": (("value",), "Hyperbolic tangent."),
    "radians": (("degrees",), "Degrees to radians."),
    "degrees": (("radians",), "Radians to degrees."),
    "sqrt": (("value",), "Square root. 0 for negative numbers."),
    "pow": (("base", "exponent"), "base to the power of exponent."),
    "exp": (("value",), "e to the power of value."),
    "log": (("value",), "Natural logarithm."),
    "log10": (("value",), "Base 10 logarithm."),
    "abs": (("value",), "Drops the minus sign."),
    "sign": (("value",), "-1, 0 or 1, depending on the sign."),
    "floor": (("value",), "Rounds down."),
    "ceil": (("value",), "Rounds up."),
    "round": (("value",), "Rounds to the nearest whole number."),
    "rint": (("value",), "Same as round()."),
    "trunc": (("value",), "Cuts off the decimals."),
    "frac": (("value",), "Just the decimals: frac(2.75) is 0.75."),
    "min": (("a", "b", "[more...]"), "The smallest value. min(v) of one vector: its smallest component."),
    "max": (("a", "b", "[more...]"), "The largest value. max(v) of one vector: its largest component."),
    "isnan": (("value",), "True when value is not a number (0 / 0 and the like)."),
    "isfinite": (("value",), "True for an ordinary number: not infinite, not NaN."),
    "len": (("vector",), "Number of components: 3 for a vector, 4 for a quaternion."),
    "clamp": (("value", "min", "max"), "Keeps value between min and max."),
    "mod": (("value", "divisor"), "Remainder after division. Never negative for a positive divisor."),
    "fmod": (("value", "divisor"), "Same as mod()."),
    "wrap": (("value", "min", "max"), "Wraps value around to stay between min and max."),
    "snap": (("value", "step"), "Rounds down to a multiple of step."),
    "pingpong": (("value", "length"), "Goes from 0 to length and back, over and over."),
    # Ranges and blending
    "fit": (("value", "old_min", "old_max", "new_min", "new_max"),
            "Moves value from the old range to the new one, clamped."),
    "efit": (("value", "old_min", "old_max", "new_min", "new_max"),
             "Like fit(), but not clamped."),
    "fit01": (("value", "new_min", "new_max"), "Moves value from 0 to 1 into the new range."),
    "fit11": (("value", "new_min", "new_max"), "Moves value from -1 to 1 into the new range."),
    "smoothstep": (("min", "max", "value"), "0 below min, 1 above max, a smooth curve between."),
    "smooth": (("min", "max", "value"), "Same as smoothstep()."),
    "smootherstep": (("min", "max", "value"), "Like smoothstep(), with softer ends."),
    "lerp": (("a", "b", "t"), "Blends from a (t = 0) to b (t = 1)."),
    "mix": (("a", "b", "t"), "Same as lerp()."),
    "invlerp": (("value", "a", "b"), "Where value sits between a (0) and b (1). Not clamped."),
    "avg": (("values...",), "Average of the values. With one vector, the average of its components."),
    # Vectors
    "set": (("x", "y", "z"), "Makes a vector."),
    "length": (("vector",), "How long the vector is."),
    "normalize": (("vector",), "Same direction, length 1."),
    "dot": (("a", "b"), "Dot product of two vectors."),
    "cross": (("a", "b"), "A vector at right angles to both."),
    "distance": (("a", "b"), "Distance between two points."),
    "length2": (("vector",), "Squared length: faster than length() for comparing."),
    "distance2": (("a", "b"), "Squared distance between two points."),
    "reflect": (("direction", "normal"), "The direction bounced off a surface with this normal."),
    "refract": (("direction", "normal", "ior"), "The direction bent through a surface; ior is the ratio of the two indices."),
    "faceforward": (("normal", "direction"), "The normal, flipped if needed to face against direction."),
    # Colour
    "hsvtorgb": (("hsv",), "Hue, saturation, value (each 0 to 1) to an RGB colour."),
    "rgbtohsv": (("rgb",), "An RGB colour to hue, saturation, value."),
    "hsltorgb": (("hsl",), "Hue, saturation, lightness (each 0 to 1) to an RGB colour."),
    "rgbtohsl": (("rgb",), "An RGB colour to hue, saturation, lightness."),
    "luminance": (("rgb",), "Brightness of a linear colour."),
    "blackbody": (("temperature", "[brightness]"), "Colour of a glowing body at this temperature in kelvin."),
    # Types
    "vector": (("x", "y", "z"), "Makes a vector. With one value, all three are set to it."),
    "float": (("value",), "Turns value into a float."),
    "int": (("value",), "Turns value into a whole number, cutting off the decimals."),
    "bool": (("value",), "True unless value is 0."),
    # Rotations and matrices
    "quaternion": (("angle", "axis"), "A rotation by angle (radians) around axis. Also quaternion(m), "
                                      "quaternion(x, y, z, w)."),
    "eulertoquaternion": (("angles", "[order]"), "Rotation from Euler angles in radians (order XFORM_XYZ...)."),
    "quaterniontoeuler": (("q", "[order]"), "Euler angles in radians, XYZ order."),
    "qrotate": (("q", "v"), "v turned by the rotation q. Same as v * q."),
    "qmultiply": (("a", "b"), "Both rotations: b first, then a. Same as a * b."),
    "qinvert": (("q",), "The opposite rotation."),
    "slerp": (("a", "b", "t"), "Turns smoothly from rotation a (t = 0) to b (t = 1)."),
    "dihedral": (("a", "b"), "The rotation that turns direction a onto b."),
    "ident": ((), "The identity matrix: no change."),
    "maketransform": (("zaxis", "yaxis", "[translate]"),
                      "Matrix facing zaxis with yaxis up. Also maketransform(translate, q, [scale])."),
    "invert": (("m",), "The matrix (or quaternion) that undoes m."),
    "transpose": (("m",), "Rows and columns swapped."),
    "determinant": (("m",), "How much the matrix scales volume."),
    "translate": (("m", "offset"), "Moves the matrix variable m by offset, after what it does."),
    "rotate": (("m", "angle", "axis"), "Turns the matrix variable m, after what it does."),
    "scale": (("m", "size"), "Scales the matrix variable m, after what it does."),
    "lookat": (("from", "to", "[up]"), "Rotation matrix turning -Z to look from one point at another."),
    "cracktransform": (("trs", "xyz", "c", "pivot", "m"),
                       "Part of a matrix: c = 0 translation, 1 rotation (degrees), 2 scale. trs, xyz, pivot: 0."),
    "getcomp": (("v", "i", "[j]"), "Component i of a vector, or element [i][j] of a matrix."),
    "setcomp": (("v", "value", "i", "[j]"), "Sets component i of the vector variable v (or [i][j] of a matrix)."),
    # Noise and random
    "noise": (("position", "[time]"), "Smooth noise, about 0 to 1. Options: freq=, offset=, amp=, octaves=, "
                                     "rough=, lacunarity=, distortion=."),
    "vnoise": (("position", "[time]"), "Smooth noise as a vector, one value per axis. Same options as noise()."),
    "snoise": (("position", "[time]"), "Signed noise, about -1 to 1. Same options as noise()."),
    "flownoise": (("position", "flow"), "Noise that changes smoothly as flow changes. Same options as noise()."),
    "ridgednoise": (("position", "[time]"), "Ridged multifractal noise: sharp creases. noise() options, plus gain=, ridge=."),
    "hybridnoise": (("position", "[time]"), "Hybrid multifractal noise. noise() options, plus gain=, ridge=."),
    "heteronoise": (("position", "[time]"), "Hetero terrain noise: smooth valleys, rough peaks. noise() options, plus ridge=."),
    "multinoise": (("position", "[time]"), "Multifractal noise. Same options as noise()."),
    "curlnoise": (("position", "[time]"), "A swirling vector field with no sources or sinks, for flows. "
                                         "Same options as noise()."),
    "voronoi": (("position", "[time]"), "Distance to the nearest cell point. Options: freq=, offset=, amp=, jitter=, "
                                       'metric="euclidean", exponent=, octaves=, rough=, lacunarity=.'),
    "voronoif2": (("position", "[time]"), "Distance to the second nearest cell point. Same options as voronoi()."),
    "voronoiedge": (("position", "[time]"), "Distance to the nearest cell edge. Same options as voronoi()."),
    "voronoicell": (("position", "[time]"), "A random colour per cell. Same options as voronoi()."),
    "voronoipos": (("position", "[time]"), "Position of the nearest cell point. Same options as voronoi()."),
    "wnoise": (("position", "[seed]", "[f1]", "[f2]"),
               "Worley noise: the nearest distance. Variables as seed, f1, f2 get the cell number and both distances."),
    "gabor": (("position",), "Gabor noise: stripes in random patches. Options: freq=, stripes=, anisotropy=, direction=, amp=."),
    "rand": (("seed", "[seed2]"), "Random number between 0 and 1. Same seed, same number."),
    "vrand": (("seed", "[seed2]"), "Random vector, each axis between 0 and 1."),
    "nrandom": (("seed",), "Random number with a normal (bell curve) spread: mean 0, deviation 1."),
    "sample_direction_uniform": (("u",), "A direction (length 1), spread evenly; u is a random vector."),
    "sample_sphere_uniform": (("u",), "A point inside the unit sphere, spread evenly; u is a random vector."),
    "sample_hemisphere": (("[normal]", "u"), "A direction on the half sphere around normal (default up)."),
    "sample_circle_uniform": (("u",), "A point inside the unit circle on the XY plane."),
    "sample_circle_edge_uniform": (("u",), "A point on the unit circle on the XY plane; u is a random number."),
    "sample_direction_cone": (("center", "angle", "u"), "A direction at most angle (radians) away from center."),
    # Node values
    "chf": (("name", "[default]"), "A float value on the node, set by hand or keyframed."),
    "chi": (("name", "[default]"), "A whole number value on the node."),
    "chv": (("name", "[default]"), "A vector value on the node."),
    "chb": (("name", "[default]"), "A checkbox on the node."),
    "chramp": (("name", "position"), "A ramp drawn on the node, read at position 0 to 1."),
    # Bounding box
    "getbbox_min": (("[input]",), "Lowest corner of the geometry's bounding box."),
    "getbbox_max": (("[input]",), "Highest corner of the geometry's bounding box."),
    "getbbox_size": (("[input]",), "Size of the geometry's bounding box."),
    "getbbox_center": (("[input]",), "Center of the geometry's bounding box."),
    "relbbox": (("[input]", "position"), "Where position sits in the bounding box, 0 to 1 on each axis."),
    # Other inputs
    "point": (("input", "name", "index"), 'Reads a point attribute from an input: point(1, "P", 0).'),
    "prim": (("input", "name", "index"), "Reads a face attribute from an input."),
    "vertex": (("input", "name", "index"), "Reads a corner attribute from an input."),
    "npoints": (("input",), "Number of points on an input."),
    "nprims": (("input",), "Number of faces on an input."),
    "nvertices": (("input",), "Number of corners on an input."),
    "nprimitives": (("input",), "Same as nprims()."),
    "nedges": (("input",), "Number of edges on an input."),
    "ncurves": (("input",), "Number of curves on an input."),
    "nearpoint": (("input", "position"), "Index of the point on the input closest to position."),
    "xyzdist": (("input", "position", "[prim]"),
                "Distance to the input's surface. A variable as prim gets the closest face."),
    "minpos": (("input", "position"), "The closest position on the input's surface."),
    "intersect": (("input", "origin", "direction", "[pos]"),
                  "Face a ray hits, or -1. The ray is as long as direction. A variable as pos gets the hit."),
    # Topology
    "primpoint": (("input", "prim", "n"), "Point number of a face's nth corner."),
    "primvertexcount": (("input", "prim"), "Number of corners of a face."),
    "vertexpoint": (("input", "vertex"), "Point number of a corner."),
    "vertexprim": (("input", "vertex"), "Face a corner belongs to."),
    "vertexprimindex": (("input", "vertex"), "Which corner of its face a corner is: 0, 1, 2..."),
    "neighbour": (("input", "point", "n"), "Point number of a point's nth neighbour along an edge."),
    "neighbourcount": (("input", "point"), "Number of points connected to a point by an edge."),
    # Statistics and other elements
    "mean": (("value", "[group]"), "Average of value over all elements, or over those with the same group number."),
    "median": (("value", "[group]"), "Middle value over all elements (or per group)."),
    "minof": (("value", "[group]"), "Smallest value over all elements (or per group)."),
    "maxof": (("value", "[group]"), "Largest value over all elements (or per group)."),
    "sumof": (("value", "[group]"), "Total of value over all elements (or per group)."),
    "stddev": (("value", "[group]"), "Standard deviation of value over all elements (or per group)."),
    "variance": (("value", "[group]"), "Variance of value over all elements (or per group)."),
    "countof": (("[group]",), "Number of elements (or of elements with this group number)."),
    "accumulate": (("value", "[group]"), "Running total: this element's value plus all before it (per group)."),
    "blur": (("value", "[iterations]", "[weight]"), "value averaged with the neighbours', iterations times."),
    "at": (("value", "index"), "value as computed at another element of this geometry."),
    "promote": (("value", "domain"), 'value computed on another domain ("face", "point"...) and read back here.'),
    "primuv": (("input", "name", "curve", "u"), 'An attribute along a curve, at u from 0 to 1: primuv(1, "P", 0, 0.5).'),
    "uvsample": (("input", "name", "uvname", "uv"), 'An attribute at a UV position: uvsample(1, "P", "UVMap", uv).'),
    # Making and removing elements
    "addpoint": (("geo", "position"), "Adds a point; gives its number. addpoint(0, ptnum) copies that point."),
    "addprim": (("geo", "type", "[points...]"), 'Adds a "poly" (a face) or "polyline" (a curve); gives its number.'),
    "addvertex": (("geo", "prim", "point"), "Adds a corner using point to a primitive addprim() made."),
    "removepoint": (("geo", "point"), "Removes a point (and the faces using it) after the script."),
    "removeprim": (("geo", "prim", "[andpoints]"), "Removes a face (or curve); andpoints = 1 also its unused points."),
    "setpointattrib": (("geo", "name", "point", "value", "[mode]"),
                       'Writes an attribute on any point. mode: "set", "add", "min" or "max".'),
    "setprimattrib": (("geo", "name", "prim", "value", "[mode]"), "Writes an attribute on any face (or curve)."),
    "setpointgroup": (("geo", "group", "point", "value"), "Puts a point in a group (a bool attribute) or takes it out."),
    "setprimgroup": (("geo", "group", "prim", "value"), "Puts a face in a group (a bool attribute) or takes it out."),
    "setvertexattrib": (("geo", "name", "prim", "vertex", "value"), "Not available in BEX."),
    "setdetailattrib": (("geo", "name", "value"), "Not available: Blender has no detail attributes."),
    # Loops
    "range": (("[start]", "stop", "[step]"), "The numbers a for loop counts through, e.g. for i in range(4)."),
}


class Compiler:
    def __init__(self, domain="POINT", pending_line=None, known_params=()):
        self.domain = domain
        # A ch*() on `pending_line` (the line being typed) whose name is not in
        # `known_params` compiles as its default constant instead of creating
        # an input, so half-typed names don't add and remove sockets.
        self.pending_line = pending_line
        self.known_params = frozenset(known_params)
        self.pending = []
        self.reads = []  # (name, type) read from the incoming geometry, first-read order
        self.cse = {}  # (kind, params, inputs) -> IRNode
        self.vars = {}  # variable name -> Val (Python-style: one flat namespace)
        self.half_set = {}  # name -> line of an `if` that set it in only one branch
        self.loop_only = {}  # name -> line of a Repeat Zone loop that first set it inside
        self.loops = []  # (break flag, continue flag) of each enclosing loop
        self.loop_count = 0
        self.group_sel = None  # the Group's bool Val: a per-element loop count only counts those
        self.zone_depth = 0  # Repeat Zones the statement being run sits in
        self.preludes = []  # chunk_end IRNodes, in script order
        self.funcs = {}  # name -> `def` statement of the script's own functions
        self.call_counts = {}  # (name, argument types) -> calls with a non-constant argument
        self.closure_keys = frozenset()  # those built once as a Closure (the second compile)
        self.closures = {}  # (name, argument types) -> closure_out IRNode, or None (inlined after all)
        self.frames = []  # one dict per running call of an own function (see call_user)
        self.calls = 0
        self.path = []  # conditions of the `if` branches being run (a loop starts stopped outside them)
        self.geo_ops = []  # GeoOp, in call order
        self.geo_counts = {}  # "point" / "poly" / "polyline" / "vertex" -> the next number (an int Val)
        self.prim_kinds = {}  # (IRNode, out) of a number addprim() gave -> "poly" / "polyline"
        self.reading = []  # per closure being built: what its body read from outside (see _closure)
        self.warnings = []
        self.attrs = {}  # attribute name -> current Val (only attributes that were written)
        self.attr_types = {}  # attribute name -> type, fixed on first use
        self.written = []  # attribute names in first-write order
        self.params = {}  # channel name -> (type, default)
        self.ramps = {}  # chramp() name -> {position Val} it is read at
        self.line = 0  # line used for error messages
        self.stmt_line = 0  # line of the statement being executed (0 = the Group expression)
        self.bare_attributes = False
        self.inputs = []  # extra geometry inputs (1-3) the script reads, first-use order
        self.storage = dict(STORAGE)  # attribute name -> Blender data type, when not the BEX type's own

    # -- IR construction ---------------------------------------------------
    def err(self, msg):
        return BexError(msg, self.line)

    def node(self, kind, type, inputs=(), out=0, **params):
        key = (kind, tuple(sorted(params.items())), tuple(inputs))
        n = self.cse.get(key)
        if n is None:
            n = self.cse[key] = IRNode(kind, params, tuple(inputs), self.stmt_line)
        return Val(type, node=n, out=out)

    def conv(self, v, type):
        """Implicit conversion. Node outputs rely on Blender's link conversion."""
        if v.type == "row":
            raise self.err("m[i] is a row of a matrix: pick one element with m[i][j]")
        if v.type == type:
            return v
        if v.type in OPAQUE or type in OPAQUE:
            hint = {"quaternion": " (make one with quaternion(), or (x, y, z, w))",
                    "matrix": " (make one with maketransform() or ident())"}.get(type, "")
            if v.type == "quaternion":
                hint = " (pick a component like .w, or use qrotate())"
            raise self.err(f"cannot use a {v.type} as {type}{hint}")
        if v.type == "vector":
            raise self.err(f"cannot use a vector as {type} (use length() or a component like .x)")
        if not v.is_const:
            return Val(type, node=v.node, out=v.out)
        c = v.const
        if type == "float":
            return Val(type, float(c))
        if type == "int":
            return Val(type, int(c))
        if type == "bool":
            return Val(type, c > 0)
        return Val(type, (float(c),) * 3)

    def fold(self, type, fn, *vals):
        """Evaluate `fn` now if every operand is constant, else return None."""
        if not all(v.is_const for v in vals):
            return None
        try:
            if type == "vector":
                r = tuple(float(fn(*(v.const[i] for v in vals))) for i in range(3))
            else:
                r = {"float": float, "int": int, "bool": bool}[type](fn(*(v.const for v in vals)))
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
        return Val(type, r)

    # -- operators ---------------------------------------------------------
    def bitop(self, op, a, b):
        """& | ^ << >> ~ on whole numbers (Bit Math); & | ^ on bools are and / or / xor."""
        if a.type == "bool" and b.type == "bool" and op in ("&", "|", "^"):
            if op == "^":
                return self.compare("!=", self.conv(a, "int"), self.conv(b, "int"))
            return self.logic("and" if op == "&" else "or", a, b)
        if RANK[a.type] > 1 or RANK[b.type] > 1:
            raise self.err(f"'{op}' works on whole numbers (int), not a {a.type if RANK[a.type] > 1 else b.type}")
        a, b = self.conv(a, "int"), self.conv(b, "int")
        fn = {"&": lambda x, y: x & y, "|": lambda x, y: x | y, "^": lambda x, y: x ^ y,
              "<<": lambda x, y: x << y if y >= 0 else x >> -y, ">>": lambda x, y: x >> y if y >= 0 else x << -y,
              "~": lambda x, y: ~x}[op]
        folded = self.fold("int", fn, a, b)
        if folded:
            return folded
        if op in ("<<", ">>"):
            shift = b if op == "<<" else self.negate(b)
            return self.gnode("int", "FunctionNodeBitMath", ("Value",), 0, (a, shift), ("A", "Shift"),
                              props=(("operation", "SHIFT"),))
        if op == "~":
            return self.gnode("int", "FunctionNodeBitMath", ("Value",), 0, (a,), ("A",), props=(("operation", "NOT"),))
        return self.gnode("int", "FunctionNodeBitMath", ("Value",), 0, (a, b), ("A", "B"),
                          props=(("operation", {"&": "AND", "|": "OR", "^": "XOR"}[op]),))

    def items(self, e):
        """The values of `(a, b, c)` or a vector's components (for `in`, `for ... in`, unpacking)."""
        if e.kind == "tuple" or (e.kind == "call" and e.name == "set"):
            return [self.eval(x) for x in (e.items if e.kind == "tuple" else e.args)]
        v = self.eval(e)
        if v.type not in SIZES:
            raise self.err(f"expected several values, like (a, b) or a vector, not a {v.type}")
        return [self.member(v, i) for i in range(SIZES[v.type])]

    def arith(self, op, a, b):
        if op in ("&", "|", "^", "<<", ">>"):
            return self.bitop(op, a, b)
        if a.type in OPAQUE or b.type in OPAQUE:
            return self.transform_arith(op, a, b)
        bop, fn = ARITH[op]
        if op == "//":
            # floor(a / b), kept integer when both sides are
            if "vector" not in (a.type, b.type) and RANK[a.type] <= 1 and RANK[b.type] <= 1:
                a, b = self.conv(a, "int"), self.conv(b, "int")
                return self.fold("int", fn, a, b) or self.node("imath", "int", (a, b), op="DIVIDE_FLOOR")
            return self.fn1("FLOOR", math.floor, self.arith("/", a, b))
        if op == "**":
            if "vector" not in (a.type, b.type) and RANK[a.type] <= 1 and RANK[b.type] <= 1 \
                    and not (b.is_const and b.const < 0):
                a, b = self.conv(a, "int"), self.conv(b, "int")
                return self.fold("int", fn, a, b) or self.node("imath", "int", (a, b), op="POWER")
            return self.fn2("POWER", fn, a, b)
        if op == "/" and "vector" not in (a.type, b.type):
            a, b = self.conv(a, "float"), self.conv(b, "float")
        if "vector" in (a.type, b.type):
            if op == "*" and a.type != b.type:
                vec, s = (a, b) if a.type == "vector" else (b, a)
                s = self.conv(s, "float")
                if vec.is_const and s.is_const:
                    return Val("vector", tuple(c * s.const for c in vec.const))
                return self.node("vmath", "vector", (vec, s), op="SCALE")
            a, b = self.conv(a, "vector"), self.conv(b, "vector")
            return self.fold("vector", fn, a, b) or self.node("vmath", "vector", (a, b), op=bop)
        if RANK[a.type] <= 1 and RANK[b.type] <= 1:
            a, b = self.conv(a, "int"), self.conv(b, "int")
            return self.fold("int", fn, a, b) or self.node("imath", "int", (a, b), op=bop)
        a, b = self.conv(a, "float"), self.conv(b, "float")
        return self.fold("float", fn, a, b) or self.node("math", "float", (a, b), op=bop)

    def compare(self, op, a, b):
        bop, fn = COMPARE[op]
        for x in (a, b):
            if x.type in OPAQUE:
                raise self.err(f"'{op}' cannot compare {x.type}s")
        if "vector" in (a.type, b.type):
            if op not in ("==", "!="):
                raise self.err(f"'{op}' cannot compare vectors (compare length() or a component)")
            t, dtype = "vector", "VECTOR"
        elif RANK[a.type] <= 1 and RANK[b.type] <= 1:
            t, dtype = "int", "INT"
        else:
            t, dtype = "float", "FLOAT"
        a, b = self.conv(a, t), self.conv(b, t)
        if a.is_const and b.is_const:
            return Val("bool", bool(fn(a.const, b.const)))
        return self.node("compare", "bool", (a, b), op=bop, dtype=dtype)

    def logic(self, op, a, b):
        a, b = self.conv(a, "bool"), self.conv(b, "bool")
        is_and = op == "and"
        if _negates(a, b) or _negates(b, a):  # x or not x, x and not x
            return Val("bool", not is_and)
        for x, y in ((a, b), (b, a)):
            if x.is_const:  # true && y -> y, false && y -> false, ...
                return y if x.const == is_and else Val("bool", not is_and)
        return self.node("bmath", "bool", (a, b), op="AND" if is_and else "OR")

    def negate(self, a):
        if a.type in OPAQUE:
            raise self.err(f"cannot negate a {a.type}" + (" (qinvert() turns it the other way)"
                                                          if a.type == "quaternion" else " (use invert())"))
        if a.type == "vector":
            return self.arith("*", a, Val("float", -1.0))
        if a.type == "float":
            return self.fold("float", lambda x: -x, a) or self.node("math", "float", (a, Val("float", -1.0)), op="MULTIPLY")
        a = self.conv(a, "int")
        return self.fold("int", lambda x: -x, a) or self.node("imath", "int", (a,), op="NEGATE")

    def not_(self, a):
        a = self.conv(a, "bool")
        return Val("bool", not a.const) if a.is_const else self.node("bmath", "bool", (a,), op="NOT")

    def select(self, cond, a, b):
        """cond ? a : b"""
        t = a.type if RANK[a.type] >= RANK[b.type] else b.type
        a, b = self.conv(a, t), self.conv(b, t)
        if cond.is_const:
            return a if cond.const else b
        if a == b:
            return a
        return self.node("switch", t, (cond, b, a), dtype=t)

    def size(self, v):
        """How many components `v` has (a vector 3, a quaternion 4)."""
        if v.type not in SIZES:
            raise self.err(f"only vectors and quaternions have components, this is a {v.type}")
        return SIZES[v.type]

    def member(self, v, i):
        if v.type == "matrix":  # m[i] is half of m[i][j]: a "row" until the second index
            if not -4 <= i < 4:
                raise self.err("a matrix has rows 0 to 3: m[i][j]")
            return Val("row", (v, i % 4))
        if v.type == "row":  # VEX's m[i][j] is Blender's column i, row j (VEX puts translation in row 3)
            m, r = v.const
            if not -4 <= i < 4:
                raise self.err("a matrix has columns 0 to 3: m[i][j]")
            k = r * 4 + i % 4
            if m.is_const:
                return Val("float", m.const[k])
            if m.node.kind == "node" and m.node.params["idname"] == "FunctionNodeCombineMatrix":
                return m.node.inputs[k]
            return self.gnode("float", "FunctionNodeSeparateMatrix", MATRIX_OUTS, k, (m,), ("Matrix",))
        n = self.size(v)
        if not -n <= i < n:
            raise self.err(f"a {v.type} has components 0 to {n - 1}" + (" (.x .y .z)" if n == 3 else ""))
        i %= n
        if v.is_const:
            return Val("float", v.const[i])
        if v.type == "quaternion":  # Blender's order is w, x, y, z
            if v.node.kind == "node" and v.node.params["idname"] == "FunctionNodeQuaternionToRotation":
                return v.node.inputs[(i + 1) % 4]
            return self.gnode("float", "FunctionNodeRotationToQuaternion", ("W", "X", "Y", "Z"), (i + 1) % 4,
                              (v,), ("Rotation",))
        if v.node.kind == "combine":
            return v.node.inputs[i]
        return self.node("separate", "float", (v,), out=i)

    def combine_matrix(self, elems):
        """A matrix from 16 floats, column by column (Blender's order)."""
        elems = [self.conv(e, "float") for e in elems]
        if all(e.is_const for e in elems):
            return Val("matrix", tuple(e.const for e in elems))
        return self.gnode("matrix", "FunctionNodeCombineMatrix", ("Matrix",), 0, elems, tuple(range(16)))

    def combine(self, comps):
        """A vector from three numbers, or a quaternion from four (x, y, z, w)."""
        comps = [self.conv(c, "float") for c in comps]
        if len(comps) == 4:
            if all(c.is_const for c in comps):
                return Val("quaternion", _qnorm(tuple(c.const for c in comps)))
            x, y, z, w = comps
            return self.gnode("quaternion", "FunctionNodeQuaternionToRotation", ("Rotation",), 0, (w, x, y, z),
                              ("W", "X", "Y", "Z"))
        if all(c.is_const for c in comps):
            return Val("vector", tuple(c.const for c in comps))
        return self.node("combine", "vector", comps)

    # -- rotations and matrices ---------------------------------------------
    # Operators follow VEX: v * q and v * m turn / transform a vector, q1 * q2 is
    # qmultiply (q2 turns first), and m1 * m2 applies m1 first, then m2 (row vectors,
    # as in Houdini; Blender's own order is the other way round).

    def transform_arith(self, op, a, b):
        types = (a.type, b.type)
        if op == "*":
            if types in (("vector", "quaternion"), ("quaternion", "vector")):
                q, v = (b, a) if a.type == "vector" else (a, b)
                return self.qrotate(q, v)
            if types == ("quaternion", "quaternion"):
                return self.qmultiply(a, b)
            if types == ("vector", "matrix"):
                return self.transform(a, b)
            if types == ("matrix", "matrix"):
                return self.matmul(a, b)
            if types == ("matrix", "vector"):
                raise self.err("write the vector first, as in VEX: v * m")
        other = {"quaternion": "qmultiply(), qrotate() or slerp()",
                 "matrix": "* with another matrix or a vector (v * m)"}
        t = a.type if a.type in OPAQUE else b.type
        raise self.err(f"'{op}' doesn't work on a {t} and a {b.type if t == a.type else a.type}; "
                       f"use {other[t]}")

    def quat(self, v, fn):
        if v.type != "quaternion":
            raise self.err(f"{fn}() needs a quaternion, got a {v.type}")
        return v

    def mat(self, v, fn):
        if v.type != "matrix":
            raise self.err(f"{fn}() needs a matrix, got a {v.type}")
        return v

    def qrotate(self, q, v):
        v = self.conv(v, "vector")
        if q.is_const and v.is_const:
            return Val("vector", _qrotate(q.const, v.const))
        return self.gnode("vector", "FunctionNodeRotateVector", ("Vector",), 0, (v, q), ("Vector", "Rotation"))

    def qmultiply(self, a, b):
        """Hamilton product a * b: b turns first, then a."""
        if a.is_const and b.is_const:
            return Val("quaternion", _qmul(a.const, b.const))
        return self.gnode("quaternion", "FunctionNodeRotateRotation", ("Rotation",), 0, (b, a),
                          ("Rotation", "Rotate By"), props=(("rotation_space", "GLOBAL"),))

    def axis_angle(self, angle, axis):
        angle, axis = self.conv(angle, "float"), self.conv(axis, "vector")
        if angle.is_const and axis.is_const:
            return Val("quaternion", _qaxis(axis.const, angle.const))
        return self.gnode("quaternion", "FunctionNodeAxisAngleToRotation", ("Rotation",), 0, (axis, angle),
                          ("Axis", "Angle"))

    def euler_order(self, v, fn):
        if not (v.is_const and v.type == "int" and 0 <= v.const < len(EULER_ORDERS)):
            raise self.err(f"{fn}()'s rotation order is a constant like XFORM_XYZ (0) to XFORM_ZYX (5)")
        return EULER_ORDERS[v.const]

    def euler(self, angles, order="XYZ"):
        """Euler angles in radians to a quaternion; the first axis of `order` turns first."""
        angles = self.conv(angles, "vector")
        if angles.is_const:
            return Val("quaternion", _qeuler(angles.const, order))
        if order == "XYZ":
            return self.gnode("quaternion", "FunctionNodeEulerToRotation", ("Rotation",), 0, (angles,), ("Euler",))
        q = None
        for axis in order:
            k = "XYZ".index(axis)
            step = self.axis_angle(self.member(angles, k), Val("vector", tuple(float(i == k) for i in range(3))))
            q = step if q is None else self.qmultiply(step, q)
        return q

    def transform(self, v, m):
        v = self.conv(v, "vector")
        if v.is_const and m.is_const:
            return Val("vector", _mat_point(m.const, v.const))
        return self.gnode("vector", "FunctionNodeTransformPoint", ("Vector",), 0, (v, m), ("Vector", "Transform"))

    def matmul(self, a, b):
        """VEX's a * b: a applies first, then b. Blender's Multiply Matrices is B @ A for that."""
        if a.is_const and b.is_const:
            return Val("matrix", _mat_mul(b.const, a.const))
        return self.gnode("matrix", "FunctionNodeMatrixMultiply", ("Matrix",), 0, (b, a), (0, 1))

    def trs(self, t, q, s):
        """Translate, rotate and scale as one matrix (scale first, then rotate, then translate)."""
        t, s = self.conv(t, "vector"), self.conv(s, "vector")
        if t.is_const and q.is_const and s.is_const:
            return Val("matrix", _mat_trs(t.const, q.const, s.const))
        return self.gnode("matrix", "FunctionNodeCombineTransform", ("Transform",), 0, (t, q, s),
                          ("Translation", "Rotation", "Scale"))

    def transform_call(self, name, a, arg_nodes, want):
        """The quaternion and matrix functions."""
        if name == "quaternion":
            want(1, 2, 4)
            if len(a) == 4:
                return self.combine(a)
            if len(a) == 2:
                return self.axis_angle(a[0], a[1])
            v = a[0]
            if v.type == "quaternion":
                return v
            if v.type == "matrix":
                return self.gnode("quaternion", "FunctionNodeSeparateTransform", ("Translation", "Rotation", "Scale"),
                                  1, (v,), ("Transform",))
            v = self.conv(v, "vector")  # angle * axis in one vector
            length = Val("float", math.hypot(*v.const)) if v.is_const else \
                self.node("vmath", "float", (v,), op="LENGTH")
            return self.axis_angle(length, v)
        if name == "eulertoquaternion":
            want(1, 2)
            return self.euler(a[0], self.euler_order(a[1], name) if len(a) == 2 else "XYZ")
        if name == "quaterniontoeuler":
            want(1, 2)
            if len(a) == 2 and self.euler_order(a[1], name) != "XYZ":
                raise self.err("quaterniontoeuler() only gives the XYZ order (XFORM_XYZ) in Blender")
            return self.gnode("vector", "FunctionNodeRotationToEuler", ("Euler",), 0, (self.quat(a[0], name),),
                              ("Rotation",))
        if name == "qrotate":
            want(2)
            return self.qrotate(self.quat(a[0], name), a[1])
        if name == "qmultiply":
            want(2)
            return self.qmultiply(self.quat(a[0], name), self.quat(a[1], name))
        if name == "qinvert" or (name == "invert" and a and a[0].type == "quaternion"):
            want(1)
            q = self.quat(a[0], name)
            if q.is_const:
                return Val("quaternion", _qinv(q.const))
            return self.gnode("quaternion", "FunctionNodeInvertRotation", ("Rotation",), 0, (q,), ("Rotation",))
        if name == "slerp":
            want(3)
            q1, q2 = self.quat(a[0], name), self.quat(a[1], name)
            return self.node("mix", "quaternion", (self.conv(a[2], "float"), q1, q2), dtype="quaternion")
        if name == "dihedral":
            want(2)
            v, w = self.conv(a[0], "vector"), self.conv(a[1], "vector")
            if v.is_const and w.is_const:
                axis = Val("vector", _cross(v.const, w.const))
                angle = Val("float", math.atan2(math.hypot(*axis.const), sum(x * y for x, y in zip(v.const, w.const))))
            else:
                axis = self.node("vmath", "vector", (v, w), op="CROSS_PRODUCT")
                angle = self.fn2("ARCTAN2", math.atan2, self.node("vmath", "float", (axis,), op="LENGTH"),
                                 self.node("vmath", "float", (v, w), op="DOT_PRODUCT"))
            return self.axis_angle(angle, axis)
        if name == "ident":
            want(0)
            return Val("matrix", _IDENTITY)
        if name == "maketransform":
            want(2, 3)
            if a[1].type == "quaternion":  # maketransform(translate, rotation, [scale])
                return self.trs(a[0], a[1], a[2] if len(a) == 3 else Val("vector", (1.0, 1.0, 1.0)))
            z, y = self.conv(a[0], "vector"), self.conv(a[1], "vector")  # VEX: maketransform(zaxis, yaxis, [t])
            rot = self.gnode("quaternion", "FunctionNodeAxesToRotation", ("Rotation",), 0, (z, y),
                             ("Primary Axis", "Secondary Axis"), props=(("primary_axis", "Z"), ("secondary_axis", "Y")))
            return self.trs(a[2] if len(a) == 3 else Val("vector", (0.0, 0.0, 0.0)), rot, Val("vector", (1.0, 1.0, 1.0)))
        if name == "lookat":  # VEX: the rotation that points -Z from `from` towards `to`, Y towards up
            want(2, 3)
            back = self.arith("-", self.conv(a[0], "vector"), self.conv(a[1], "vector"))
            up = self.conv(a[2], "vector") if len(a) == 3 else Val("vector", (0.0, 1.0, 0.0))
            rot = self.gnode("quaternion", "FunctionNodeAxesToRotation", ("Rotation",), 0, (back, up),
                             ("Primary Axis", "Secondary Axis"), props=(("primary_axis", "Z"), ("secondary_axis", "Y")))
            return self.trs(Val("vector", (0.0, 0.0, 0.0)), rot, Val("vector", (1.0, 1.0, 1.0)))
        if name == "cracktransform":
            want(5)
            trs, xyz, c, pivot = a[:4]
            if not (trs.is_const and trs.const == 0 and xyz.is_const and xyz.const == 0):
                raise self.err("cracktransform() only reads Blender's order: trs = 0 (XFORM_SRT), xyz = 0 (XFORM_XYZ)")
            if not (pivot.is_const and not any(pivot.const if pivot.type == "vector" else [pivot.const])):
                raise self.err("cracktransform()'s pivot has to be (0, 0, 0)")
            if not (c.is_const and c.const in (0, 1, 2)):
                raise self.err("cracktransform()'s c is 0 (translation), 1 (rotation in degrees) or 2 (scale)")
            m = self.mat(a[4], name)
            if c.const != 1:
                return self.gnode("vector", "FunctionNodeSeparateTransform", ("Translation", "Rotation", "Scale"),
                                  c.const, (m,), ("Transform",))
            q = self.gnode("quaternion", "FunctionNodeSeparateTransform", ("Translation", "Rotation", "Scale"), 1,
                           (m,), ("Transform",))
            return self.arith("*", self.call_val("quaterniontoeuler", q), Val("float", 180 / math.pi))
        if name == "invert":
            want(1)
            return self.gnode("matrix", "FunctionNodeInvertMatrix", ("Matrix", "Invertible"), 0, (self.mat(a[0], name),),
                              ("Matrix",))
        if name == "transpose":
            want(1)
            return self.gnode("matrix", "FunctionNodeTransposeMatrix", ("Matrix",), 0, (self.mat(a[0], name),),
                              ("Matrix",))
        if name == "determinant":
            want(1)
            return self.gnode("float", "FunctionNodeMatrixDeterminant", ("Determinant",), 0, (self.mat(a[0], name),),
                              ("Matrix",))
        # translate(m, t), rotate(m, angle, axis), scale(m, s): change the matrix variable, as in VEX
        want(3 if name == "rotate" else 2)
        m = self.mat(a[0], name)
        target = arg_nodes[0]
        if target.kind not in ("var", "attr"):
            raise self.err(f"{name}()'s first argument is the matrix it changes: give it a variable name")
        one, zero, no_turn = Val("vector", (1.0, 1.0, 1.0)), Val("vector", (0.0, 0.0, 0.0)), Val("quaternion", (0.0, 0.0, 0.0, 1.0))
        if name == "translate":
            step = self.trs(a[1], no_turn, one)
        elif name == "scale":
            step = self.trs(zero, no_turn, a[1])
        else:
            step = self.trs(zero, self.axis_angle(a[1], a[2]), one)
        result = self.matmul(m, step)
        self.assign(target, result)
        return result

    # -- functions ---------------------------------------------------------
    def fn1(self, op, fn, a):
        if a.type == "vector":
            if op in VMATH1:
                return self.fold("vector", fn, a) or self.node("vmath", "vector", (a,), op=VMATH1[op])
            return self.combine([self.fn1(op, fn, self.member(a, i)) for i in range(3)])
        if RANK[a.type] <= 1 and op in ("ABSOLUTE", "SIGN"):
            a = self.conv(a, "int")
            return self.fold("int", fn, a) or self.node("imath", "int", (a,), op=op)
        a = self.conv(a, "float")
        return self.fold("float", fn, a) or self.node("math", "float", (a,), op=op)

    def fn2(self, op, fn, a, b):
        if "vector" in (a.type, b.type):
            a, b = self.conv(a, "vector"), self.conv(b, "vector")
            if op in VMATH2:
                return self.fold("vector", fn, a, b) or self.node("vmath", "vector", (a, b), op=VMATH2[op])
            return self.combine([self.fn2(op, fn, self.member(a, i), self.member(b, i)) for i in range(3)])
        if RANK[a.type] <= 1 and RANK[b.type] <= 1 and op in IMATH2:
            a, b = self.conv(a, "int"), self.conv(b, "int")
            return self.fold("int", fn, a, b) or self.node("imath", "int", (a, b), op=IMATH2[op])
        a, b = self.conv(a, "float"), self.conv(b, "float")
        return self.fold("float", fn, a, b) or self.node("math", "float", (a, b), op=op)

    def map_range(self, a, interp, clamp):
        """fit()-style remap. Vectors remap per component."""
        t = "vector" if any(x.type == "vector" for x in a) else "float"
        a = [self.conv(x, t) for x in a]
        folded = self.fold(t, lambda *v: _map_range(*v, interp, clamp), *a)
        if folded:
            return folded
        return self.node("maprange", t, a, interp=interp, clamp=clamp, dtype=t)

    def bbox(self, name, a, want):
        """Bounding box of the incoming geometry: getbbox_min/max/size/center(), relbbox(p)."""
        n = 1 if name == "relbbox" else 0
        want(n, n + 1)
        geo = 0
        if len(a) == n + 1:
            geo = self.input_number(a[0], name)
            a = a[1:]
        if name == "relbbox":
            return self.node("group", "vector", (self.conv(a[0], "vector"),), name="relbbox", input=geo)
        out = ("getbbox_min", "getbbox_max", "getbbox_size", "getbbox_center").index(name)
        return self.node("group", "vector", (), out=out, name="bbox", input=geo)

    # -- other inputs ---------------------------------------------------------
    def input_number(self, v, fn):
        """The geometry input a function reads: a constant 0-3 (1-3 are recorded in `inputs`)."""
        if not (v.is_const and v.type == "int" and 0 <= v.const <= MAX_INPUT):
            raise self.err(f"{fn}()'s first argument is the input number: 0 (Geometry), "
                           f"or 1, 2, 3 for the sockets Input 2, Input 3, Input 4")
        if v.const and v.const not in self.inputs:
            self.inputs.append(v.const)
        return v.const

    def read_input(self, n, name, type):
        """A field that reads `name` on input `n`. Its own IR node (`src`), so it is
        never mixed up with, or captured as, the same read on this node's geometry."""
        if name in BUILTINS:
            _, kind, out = BUILTINS[name]
            return self.node("input", type, (), out=out, name=kind, src=n)
        return self.node("attr", type, (), name=name, dtype=type, src=n)

    def sample(self, n, name, type, index, domain):
        value = self.read_input(n, name, type)
        index = self.conv(index, "int")
        return self.node("sample", type, (value, index), input=n, domain=domain, dtype=type)

    def sample_call(self, fn, args):
        """point(1, "P", i) / prim() / vertex(): an attribute of element i on an input."""
        if len(args) != 3 or args[1].kind != "str":
            raise self.err(f'{fn}() takes an input number, an attribute name in quotes and an index, '
                           f'e.g. {fn}(1, "P", @ptnum)')
        n = self.input_number(self.eval(args[0]), fn)
        name, type = _typed_name(args[1].value)
        if not name:
            raise self.err(f"{fn}() needs an attribute name")
        if type is None:
            type = BUILTINS[name][0] if name in BUILTINS else KNOWN_TYPES.get(name, "float")
        return self.sample(n, name, type, self.eval(args[2]), SAMPLE_FUNCS[fn])

    def count(self, n, domain):
        return self.node("count", "int", (), input=n, domain=domain)

    def gnode(self, type, idname, outs, out=0, inputs=(), ins=(), **params):
        """One Blender node `idname` (IR `node`): `inputs` go to the sockets named `ins`,
        and the Val is output `out` of the sockets named `outs`. Other params: `props`
        ((property, value) pairs), `menus` ((socket, item) pairs), `geo` ((socket, input
        number): the geometry it reads), `implicit` (sockets that fall back to a field when
        unlinked), `field` (a field of the geometry it is evaluated on, even with constant
        inputs), `src` (the input it is evaluated on, inside a node that reads that input)."""
        return self.node("node", type, tuple(inputs), out=out, idname=idname, outs=tuple(outs),
                         ins=tuple(ins), **params)

    def topology(self, fn, n, idname, output, index_sock, index, domain, extra=()):
        """A mesh topology node read on input `n` at `index` (an element of `domain`).
        On this geometry it is a field taking the index directly; on another input it is
        evaluated there, at every element, and sampled at `index`. `extra`: other int
        inputs (socket, Val), which have to be constants on another input."""
        outs = NODE_OUTS[idname]
        index = self.conv(index, "int")
        socks = tuple(s for s, _ in extra)
        vals = tuple(self.conv(v, "int") for _, v in extra)
        if n == 0:
            return self.gnode("int", idname, outs, outs.index(output), (index,) + vals, (index_sock,) + socks,
                              implicit=(index_sock,), field=True)
        if not all(v.is_const for v in vals):
            raise self.err(f"on another input, {fn}()'s last argument has to be a constant")
        value = self.gnode("int", idname, outs, outs.index(output), vals, socks, src=n)
        return self.node("sample", "int", (value, index), input=n, domain=domain, dtype="int")

    def proximity(self, n, pos, out):
        """Geometry Proximity to input `n`'s faces: out 0 the closest position, 1 the distance."""
        return self.gnode("float" if out else "vector", "GeometryNodeProximity", ("Position", "Distance"), out,
                          (pos,), ("Sample Position",), geo=("Geometry", n), props=(("target_element", "FACES"),),
                          implicit=("Sample Position",))

    def out_arg(self, fn, arg, val):
        """VEX's output arguments: `xyzdist(1, @P, prim)` sets the variable `prim`."""
        if arg.kind not in ("var", "attr", "member", "index"):
            self.line = arg.line
            raise self.err(f"{fn}()'s last argument receives a value: give it a variable name")
        self.assign(arg, val)

    def query(self, fn, args):
        """xyzdist() and intersect(), which can fill in a last output argument."""
        need = 2 if fn == "xyzdist" else 3
        if len(args) not in (need, need + 1):
            usage = "xyzdist(1, @P) or xyzdist(1, @P, prim)" if fn == "xyzdist" else \
                "intersect(1, origin, direction) or intersect(1, origin, direction, pos)"
            raise self.err(f"{fn}() takes {need} or {need + 1} arguments: {usage}")
        a = [self.eval(x) for x in args[:need]]
        n = self.input_number(a[0], fn)
        # The face is read through Face of Corner: both nodes look values up on the corners.
        face = self.gnode("int", "GeometryNodeFaceOfCorner", NODE_OUTS["GeometryNodeFaceOfCorner"], 0, src=n)
        if fn == "xyzdist":
            pos = self.conv(a[1], "vector")
            if len(args) > need:
                prim = self.gnode("int", "GeometryNodeSampleNearestSurface", ("Value",), 0, (face, pos),
                                  ("Value", "Sample Position"), props=(("data_type", "INT"),), geo=("Mesh", n),
                                  implicit=("Sample Position",))
                self.out_arg(fn, args[need], prim)
            return self.proximity(n, pos, 1)
        origin, direction = self.conv(a[1], "vector"), self.conv(a[2], "vector")
        length = (Val("float", math.hypot(*direction.const)) if direction.is_const
                  else self.node("vmath", "float", (direction,), op="LENGTH"))

        def ray(type, out):
            return self.gnode(type, "GeometryNodeRaycast", ("Is Hit", "Hit Position", "Hit Normal", "Hit Distance",
                                                            "Attribute"), out,
                              (face, origin, direction, length),
                              ("Attribute", "Source Position", "Ray Direction", "Ray Length"),
                              props=(("data_type", "INT"),), menus=(("Interpolation", "Nearest"),),
                              geo=("Target Geometry", n), implicit=("Source Position",))
        if len(args) > need:
            self.out_arg(fn, args[need], ray("vector", 1))
        return self.select(ray("bool", 0), ray("int", 4), Val("int", -1))

    def color(self, name, v):
        """hsvtorgb() and friends. Blender's HSL order is hue, saturation, lightness."""
        mode, to_rgb = COLOR_MODES[name], name.endswith("torgb")
        if v.is_const:
            h, s, x = v.const
            if mode == "HSV":
                r = colorsys.hsv_to_rgb(h, s, x) if to_rgb else colorsys.rgb_to_hsv(h, s, x)
            elif to_rgb:
                r = colorsys.hls_to_rgb(h, x, s)
            else:
                h, l, s = colorsys.rgb_to_hls(h, s, x)
                r = (h, s, l)
            return Val("vector", tuple(float(c) for c in r))
        if to_rgb:  # hue wraps around like an angle (Combine Color would clamp it)
            comps = [self.member(v, i) for i in range(3)]
            comps[0] = self.fn1("FRACT", lambda x: x - math.floor(x), comps[0])
            return self.gnode("vector", "FunctionNodeCombineColor", ("Color",), 0, comps, ("Red", "Green", "Blue"),
                              props=(("mode", mode),))
        return self.combine([self.gnode("float", "FunctionNodeSeparateColor", ("Red", "Green", "Blue"), i, (v,),
                                        ("Color",), props=(("mode", mode),)) for i in range(3)])

    # -- making and removing elements ----------------------------------------
    # Each call site is a GeoOp. The builder turns every element where the call ran
    # into a record point, then, after the writes: adds the new points and
    # primitives, writes other elements' attributes, and removes elements.
    # Numbers given back follow Houdini's rule for one call site (in element order)
    # and continue across call sites in the order the script runs them.

    def geo_mask(self):
        """Where a call made now runs: the Group, the `if` branches around it, not
        after a break / continue / return, and where its function was called."""
        m = self._live()
        if self.frames:
            m = self.logic("and", m, self.frames[-1]["live"])
        for c in self.path:
            m = self.logic("and", m, c)
        if self.group_sel is not None:
            m = self.logic("and", m, self.group_sel)
        return m

    def geo_number(self, kind, mask):
        """The number the element's record gets: the next free number, plus how
        many elements before it made one at this call site."""
        if kind not in self.geo_counts:
            domain = {"point": "POINT", "poly": "FACE", "polyline": "CURVE", "vertex": "CORNER"}[kind]
            self.geo_counts[kind] = self.count(0, domain)
        made = self.conv(mask, "int")
        outs = AGGREGATE_OUTS["GeometryNodeAccumulateField"]
        acc = lambda out: self.gnode("int", "GeometryNodeAccumulateField", outs, out,  # noqa: E731
                                     (made, Val("int", 0)), ("Value", "Group ID"),
                                     props=(("domain", self.domain), ("data_type", "INT")), field=True)
        base = self.geo_counts[kind]
        self.geo_counts[kind] = self.arith("+", base, acc(2))
        return self.arith("+", base, acc(1))

    def geo_call(self, fn, args):
        usage = {
            "addpoint": "addpoint(0, position) or addpoint(0, point_number)",
            "addprim": 'addprim(0, "poly", pt0, pt1, pt2...) or addprim(0, "polyline", pt0, pt1...)',
            "addvertex": "addvertex(0, prim, point)",
            "removepoint": "removepoint(0, point)", "removeprim": "removeprim(0, prim, 1)",
            "setpointattrib": 'setpointattrib(0, "name", point, value, "set")',
            "setprimattrib": 'setprimattrib(0, "name", prim, value, "set")',
            "setpointgroup": 'setpointgroup(0, "name", point, 1)', "setprimgroup": 'setprimgroup(0, "name", prim, 1)',
        }
        if fn == "setdetailattrib":
            raise self.err("Blender has no detail attributes: store the value on every point (@name = value), "
                           "or read a statistic with mean() / sumof()")
        if fn == "setvertexattrib":
            raise self.err("setvertexattrib() isn't available; run over Corners and write @name instead")
        if self.zone_depth:
            raise self.err(f"{fn}() can't run inside a loop that becomes a Repeat Zone. Loops with a fixed "
                           f"count of up to {GEO_UNROLL_MAX} passes work")
        if not args:
            raise self.err(f"{fn}() takes: {usage[fn]}")
        geo = self.eval(args[0])
        if not (geo.is_const and geo.type == "int" and geo.const == 0):
            raise self.err(f"{fn}()'s first argument is the geometry it changes: 0 (this node's output)")
        line = self.line
        mask = self.geo_mask()
        if mask.is_const and not mask.const:
            return Val("int", -1)  # never runs
        if fn == "addpoint":
            if len(args) != 2:
                raise self.err(f"{fn}() takes: {usage[fn]}")
            x = self.eval(args[1])
            if x.type == "vector":
                values, info = {"pos": x}, {"src": False}
            elif RANK.get(x.type, 9) <= 1:
                values, info = {"src": self.conv(x, "int")}, {"src": True}
            else:
                raise self.err(f"addpoint() takes a position (a vector) or a point number, not a {x.type}")
            self.geo_ops.append(GeoOp("addpoint", mask, values, info, line))
            return self.geo_number("point", mask)
        if fn == "addprim":
            if len(args) < 2 or args[1].kind != "str" or args[1].value not in PRIM_TYPES:
                raise self.err(f'addprim()\'s second argument is "poly" or "polyline": {usage[fn]}')
            ptype = PRIM_TYPES[args[1].value]
            pts = [self.conv(self.eval(a), "int") for a in args[2:]]
            self.line = line
            prim = self.geo_number(ptype, mask)
            for pt in pts:
                self.geo_ops.append(GeoOp("vertex", mask, {"prim": prim, "pt": pt}, {"ptype": ptype}, line))
                self.geo_number("vertex", mask)
            if not prim.is_const:
                self.prim_kinds[(prim.node, prim.out)] = ptype
            return prim
        if fn == "addvertex":
            if len(args) != 3:
                raise self.err(f"{fn}() takes: {usage[fn]}")
            prim, pt = self.conv(self.eval(args[1]), "int"), self.conv(self.eval(args[2]), "int")
            self.line = line
            ptype = self._prim_kind(prim)
            if ptype is None:
                raise self.err("addvertex() adds to a primitive this script made: pass the number addprim() gave")
            self.geo_ops.append(GeoOp("vertex", mask, {"prim": prim, "pt": pt}, {"ptype": ptype}, line))
            return self.geo_number("vertex", mask)
        if fn in ("removepoint", "removeprim"):
            if len(args) not in (2, 3):
                raise self.err(f"{fn}() takes: {usage[fn]}")
            target = self.conv(self.eval(args[1]), "int")
            flag = self.eval(args[2]) if len(args) == 3 else Val("int", 1)
            if not flag.is_const:
                raise self.err(f"{fn}()'s last argument has to be a constant 0 or 1")
            self.line = line
            domain = "POINT" if fn == "removepoint" else self._prim_domain(target)
            mode = "ALL" if fn == "removepoint" or flag.const else "ONLY_FACE"
            self.geo_ops.append(GeoOp("remove", mask, {"target": target}, {"domain": domain, "mode": mode}, line))
            return Val("int", 0)
        # set*attrib / set*group
        group = fn.endswith("group")
        need = 4 if group else (4, 5)
        if len(args) not in ((need,) if group else need) or args[1].kind != "str":
            raise self.err(f"{fn}() takes: {usage[fn]}")
        name, type = _typed_name(args[1].value)
        name = ALIASES.get(name, name)
        target = self.conv(self.eval(args[2]), "int")
        value = self.eval(args[3])
        mode = "set"
        if len(args) == 5:
            if args[4].kind != "str" or args[4].value not in SET_MODES:
                raise self.err(f'{fn}()\'s mode is one of {", ".join(repr(m) for m in SET_MODES)}')
            mode = args[4].value
        self.line = line
        if group:
            type = "bool"
        elif type is None:
            type = self.attr_types.get(name) or BUILTINS.get(name, (None,))[0] or KNOWN_TYPES.get(name) or value.type
        if name in READ_ONLY or name in BUILTINS and name not in ("P",):
            raise self.err(f"{fn}() can't write @{name}")
        if mode != "set" and type in ("bool",) + OPAQUE:
            raise self.err(f'mode "{mode}" needs numbers or vectors, not a {type}')
        value = self.conv(value, type)
        domain = "POINT" if fn.startswith("setpoint") else self._prim_domain(target)
        if name == "P":
            if domain != "POINT":
                raise self.err("only points have a position")
            name = "position"
        self.geo_ops.append(GeoOp("set", mask, {"target": target, "value": value},
                                  {"domain": domain, "name": name, "type": type, "mode": mode,
                                   "storage": self.storage.get(name)}, line))
        return value

    def _prim_kind(self, prim):
        return None if prim.is_const else self.prim_kinds.get((prim.node, prim.out))

    def _prim_domain(self, prim):
        """A primitive number means a curve when running over curves or when addprim() made a polyline."""
        return "CURVE" if self.domain == "CURVE" or self._prim_kind(prim) == "polyline" else "FACE"

    # -- statistics and other elements -------------------------------------
    def aggregate(self, name, v, group):
        """mean(), sumof()...: one value over the whole run-over domain, or per group number."""
        idname, out = AGGREGATES[name]
        if v.type in ("bool", "int"):
            v = self.conv(v, "int" if idname in INT_AGGREGATES else "float")
        elif v.type not in ("float", "vector"):
            raise self.err(f"{name}() works on numbers and vectors, not a {v.type}")
        group = self.conv(group, "int") if group is not None else Val("int", 0)
        outs = AGGREGATE_OUTS[idname]
        return self.gnode(v.type, idname, outs, outs.index(out), (v, group), ("Value", "Group ID"),
                          props=(("domain", self.domain), ("data_type", DATA_TYPES[v.type])), field=True)

    def surface_sample(self, fn, args):
        """primuv(1, "P", curve, u) along a curve, uvsample(1, "P", "UVMap", uv) on a mesh by UV."""
        usage = ('primuv(1, "P", curve, u)' if fn == "primuv" else 'uvsample(1, "P", "UVMap", uv)')
        if len(args) != 4 or args[1].kind != "str" or (fn == "uvsample" and args[2].kind != "str"):
            raise self.err(f"{fn}() takes an input number, an attribute name in quotes, then: {usage}")
        n = self.input_number(self.eval(args[0]), fn)
        name, type = _typed_name(args[1].value)
        name = ALIASES.get(name, name)
        if type is None:
            type = BUILTINS[name][0] if name in BUILTINS else KNOWN_TYPES.get(name, "float")
        value = self.read_input(n, name, type)
        if fn == "primuv":
            curve, u = self.conv(self.eval(args[2]), "int"), self.eval(args[3])
            u = self.member(u, 0) if u.type == "vector" else self.conv(u, "float")
            return self.gnode(type, "GeometryNodeSampleCurve", ("Value", "Position", "Tangent", "Normal"), 0,
                              (value, u, curve), ("Value", "Factor", "Curve Index"),
                              props=(("data_type", DATA_TYPES[type]), ("mode", "FACTOR"), ("use_all_curves", False)),
                              geo=("Curves", n))
        uvname = ALIASES.get(args[2].value, args[2].value)
        uvmap = self.read_input(n, uvname, "vector")
        uv = self.conv(self.eval(args[3]), "vector")
        return self.gnode(type, "GeometryNodeSampleUVSurface", ("Value", "Is Valid"), 0, (value, uvmap, uv),
                          ("Value", "UV Map", "Sample UV"), props=(("data_type", DATA_TYPES[type]),), geo=("Mesh", n))

    # -- noise ---------------------------------------------------------------
    def call_val(self, name, *vals):
        """A built-in function called with values that are already computed."""
        return self.call(name, [N("val", self.line, val=v) for v in vals])

    def _opt(self, kw, key, default, type="float"):
        v = kw.get(key)
        if isinstance(v, str):
            raise self.err(f"{key}= takes a number, not text")
        return self.conv(v, type) if v is not None else Val(type, default)

    def _noise_pos(self, a, kw):
        """The position (moved by offset=) and, with a second argument, the 4D time."""
        p = self.conv(a[0], "vector")
        if "offset" in kw:
            p = self.arith("+", p, self._opt(kw, "offset", (0.0, 0.0, 0.0), "vector"))
        if len(a) > 1:
            return [p, self.conv(a[1], "float")], ["Vector", "W"], "4D"
        return [p], ["Vector"], "3D"

    def _amp(self, v, kw):
        return self.arith("*", v, kw["amp"]) if "amp" in kw else v

    def noise_call(self, name, a, kw):
        """noise() and its family: one Noise Texture. octaves=N is Blender's Detail N - 1."""
        ins, socks, dims = self._noise_pos(a, kw)
        octaves = self.arith("-", self._opt(kw, "octaves", 1.0), Val("float", 1.0))
        ins += [self._opt(kw, "freq", 1.0), octaves, self._opt(kw, "rough", 0.5), self._opt(kw, "lacunarity", 2.0),
                self._opt(kw, "distortion", 0.0)]
        socks += ["Scale", "Detail", "Roughness", "Lacunarity", "Distortion"]
        for key, sock, default in (("ridge", "Offset", 0.0), ("gain", "Gain", 1.0)):
            if key in KEYWORDS.get(name, ()):  # only the multifractal types have these sockets
                ins.append(self._opt(kw, key, default))
                socks.append(sock)
        if name == "curlnoise":
            if dims == "3D":
                ins.insert(1, Val("float", 0.0))
            return self._amp(self.node("group", "vector", tuple(ins[:7]), name="curl"), kw)
        vector = name == "vnoise"
        v = self.gnode("vector" if vector else "float", "ShaderNodeTexNoise", ("Factor", "Color"), int(vector),
                       ins, socks, props=(("noise_dimensions", dims), ("noise_type", NOISE_FUNCS[name])),
                       implicit=("Vector",))
        if name == "snoise":
            v = self.arith("*", self.arith("-", v, Val("float", 0.5)), Val("float", 2.0))
        return self._amp(v, kw)

    def _voronoi_node(self, a, kw, feature, out):
        ins, socks, dims = self._noise_pos(a, kw)
        octaves = self.arith("-", self._opt(kw, "octaves", 1.0), Val("float", 1.0))
        ins += [self._opt(kw, "freq", 1.0), octaves, self._opt(kw, "rough", 0.5), self._opt(kw, "lacunarity", 2.0),
                self._opt(kw, "jitter", 1.0)]
        socks += ["Scale", "Detail", "Roughness", "Lacunarity", "Randomness"]
        metric = kw.get("metric", "euclidean")
        if not isinstance(metric, str) or metric.lower() not in VORONOI_METRICS:
            raise self.err('metric= is one of "euclidean", "manhattan", "chebyshev", "minkowski"')
        if VORONOI_METRICS[metric.lower()] == "MINKOWSKI":
            ins.append(self._opt(kw, "exponent", 0.5))
            socks.append("Exponent")
        outs = ("Distance", "Color", "Position", "W", "Radius")
        type = "float" if out == "Distance" else "vector"
        return self.gnode(type, "ShaderNodeTexVoronoi", outs, outs.index(out), ins, socks,
                          props=(("voronoi_dimensions", dims), ("feature", feature),
                                 ("distance", VORONOI_METRICS[metric.lower()])), implicit=("Vector",))

    def voronoi(self, name, a, kw):
        feature, out = VORONOI_FUNCS[name]
        return self._amp(self._voronoi_node(a, kw, feature, out), kw)

    def wnoise(self, args, kw):
        """VEX's wnoise(p, seed, f1, f2): the nearest distance, filling the out arguments."""
        if not 1 <= len(args) <= 4:
            raise self.err("wnoise() takes a position, then optionally variables for seed, f1 and f2")
        p = [self.eval(args[0])]
        f1 = self._voronoi_node(p, kw, "F1", "Distance")
        if len(args) > 1:  # a number per cell, from the cell's random colour
            cell = self.member(self._voronoi_node(p, kw, "F1", "Color"), 0)
            self.out_arg("wnoise", args[1], self.conv(self.fn1(*MATH1["floor"], self.arith(
                "*", cell, Val("float", 16777215.0))), "int"))
        if len(args) > 2:
            self.out_arg("wnoise", args[2], self._amp(f1, kw))
        if len(args) > 3:
            self.out_arg("wnoise", args[3], self._amp(self._voronoi_node(p, kw, "F2", "Distance"), kw))
        return self._amp(f1, kw)

    def gabor(self, p, kw):
        p = self.conv(p, "vector")
        if "offset" in kw:
            p = self.arith("+", p, self._opt(kw, "offset", (0.0, 0.0, 0.0), "vector"))
        ins = [p, self._opt(kw, "freq", 1.0), self._opt(kw, "stripes", 2.0), self._opt(kw, "anisotropy", 1.0),
               self._opt(kw, "direction", (1.0, 0.0, 0.0), "vector")]
        v = self.gnode("float", "ShaderNodeTexGabor", ("Value", "Phase", "Intensity"), 0, ins, (0, 1, 2, 3, 5),
                       props=(("gabor_type", "3D"),), implicit=(0,))
        return self._amp(v, kw)

    def sample_dir(self, name, a, want):
        """VEX's sample_*(): turn random numbers 0-1 into evenly spread directions and points."""
        f = lambda x: Val("float", x)  # noqa: E731
        mul = lambda x, y: self.arith("*", x, y)  # noqa: E731
        sqrt, sin, cos = (lambda x: self.fn1(*MATH1["sqrt"], x)), (lambda x: self.fn1(*MATH1["sin"], x)), \
            (lambda x: self.fn1(*MATH1["cos"], x))

        def around(z, phi):  # the unit vector at height z and angle phi around the Z axis
            r = sqrt(self.fn2(*MATH2["max"], self.arith("-", f(1.0), mul(z, z)), f(0.0)))
            return self.combine([mul(r, cos(phi)), mul(r, sin(phi)), z])

        def turned(center, v):  # v with its Z axis turned onto center
            q = self.call_val("dihedral", Val("vector", (0.0, 0.0, 1.0)), self.conv(center, "vector"))
            return self.qrotate(q, v)

        if name == "sample_circle_edge_uniform":
            want(1)
            phi = mul(self.conv(a[0], "float"), f(math.tau))
            return self.combine([cos(phi), sin(phi), f(0.0)])
        if name == "sample_hemisphere":
            want(1, 2)
            u = self.conv(a[-1], "vector")
            v = around(self.member(u, 0), mul(self.member(u, 1), f(math.tau)))
            return turned(a[0], v) if len(a) == 2 else v
        if name == "sample_direction_cone":
            want(3)
            u = self.conv(a[2], "vector")
            span = self.arith("-", f(1.0), cos(self.conv(a[1], "float")))
            v = around(self.arith("-", f(1.0), mul(self.member(u, 0), span)), mul(self.member(u, 1), f(math.tau)))
            return turned(a[0], v)
        want(1)
        u = self.conv(a[0], "vector")
        if name == "sample_circle_uniform":
            r, phi = sqrt(self.member(u, 0)), mul(self.member(u, 1), f(math.tau))
            return self.combine([mul(r, cos(phi)), mul(r, sin(phi)), f(0.0)])
        d = around(self.arith("-", f(1.0), mul(self.member(u, 0), f(2.0))), mul(self.member(u, 1), f(math.tau)))
        if name == "sample_direction_uniform":
            return d
        return mul(d, self.fn2(*MATH2["pow"], self.member(u, 2), f(1.0 / 3.0)))  # sample_sphere_uniform

    def read_opinput(self, n, name, prefix_type):
        if n > MAX_INPUT:
            raise self.err(f"@opinput{n}_{name}: there are only inputs 0-{MAX_INPUT}")
        self.input_number(Val("int", n), f"@opinput{n}")
        name = ALIASES.get(name, name)
        type = BUILTINS[name][0] if name in BUILTINS else prefix_type or KNOWN_TYPES.get(name, "float")
        index = self.read_original("index", "int")
        return self.sample(n, name, type, index, self.domain)

    def channel(self, name, args):
        type = CHANNELS[name]
        if not args or args[0].kind != "str":
            raise self.err(f'{name}() needs a name in quotes, e.g. {name}("amount")')
        label = args[0].value.strip()
        if not label or label == "Geometry" or label in INPUT_SOCKETS:
            raise self.err(f"'{label}' cannot be used as a {name}() name")
        if len(args) > 2:
            raise self.err(f"{name}() takes a name and an optional default value")
        default = Val(type, {"float": 0.0, "int": 0, "bool": False, "vector": (0.0, 0.0, 0.0)}[type])
        if len(args) == 2:
            default = self.conv(self.eval(args[1]), type)
            if not default.is_const:
                raise self.err(f"the default value of {name}() must be a constant")
        if args[0].line == self.pending_line and label not in self.known_params and label not in self.params:
            if label not in self.pending:
                self.pending.append(label)
            return default
        known = self.params.get(label)
        if known and known[0] != type:
            raise self.err(f"input '{label}' is already used as {known[0]}")
        if not known or len(args) == 2:
            self.params[label] = (type, default.const)
        return self.node("param", type, (), name=label)

    def ramp(self, name, args):
        if not args or args[0].kind != "str":
            raise self.err(f'{name}() needs a name in quotes, e.g. {name}("falloff", u)')
        if len(args) != 2:
            raise self.err(f"{name}() takes a name and a position, e.g. {name}(\"falloff\", u)")
        label = args[0].value.strip()
        if not label:
            raise self.err(f"'{label}' cannot be used as a {name}() name")
        pos = self.conv(self.eval(args[1]), "float")
        if args[0].line == self.pending_line and label not in self.known_params and label not in self.ramps:
            # Still being typed: act as the default (straight) ramp, and make no node yet.
            if label not in self.pending:
                self.pending.append(label)
            return pos
        self.ramps.setdefault(label, set()).add(pos)
        return self.node("ramp", RAMPS[name], (pos,), name=label)

    def call(self, name, arg_nodes):
        if name in self.funcs:
            return self.call_user(name, arg_nodes)
        kw = {}
        for k in (a for a in arg_nodes if a.kind == "kwarg"):
            self.line = k.line
            allowed = KEYWORDS.get(name, ())
            if not allowed:
                raise self.err(f"{name}() takes no keyword arguments")
            if k.name not in allowed:
                raise self.err(f"{name}() has no option '{k.name}'; it takes {', '.join(n + '=' for n in allowed)}")
            if k.name in kw:
                raise self.err(f"{name}() got {k.name}= twice")
            kw[k.name] = k.value.value if k.value.kind == "str" else self.eval(k.value)
        arg_nodes = [a for a in arg_nodes if a.kind != "kwarg"]
        if name in CHANNELS:
            return self.channel(name, arg_nodes)
        if name in RAMPS:
            return self.ramp(name, arg_nodes)
        if name in SAMPLE_FUNCS:
            return self.sample_call(name, arg_nodes)
        if name in ("xyzdist", "intersect"):
            return self.query(name, arg_nodes)
        if name == "wnoise":
            return self.wnoise(arg_nodes, kw)
        if name in ("primuv", "uvsample"):
            return self.surface_sample(name, arg_nodes)
        if name in GEO_FUNCS:
            return self.geo_call(name, arg_nodes)
        if name == "setcomp":  # setcomp(v, value, i, [j]): v[i] = value, v[i][j] = value
            if len(arg_nodes) not in (3, 4) or arg_nodes[0].kind not in ("var", "attr"):
                raise self.err("setcomp() takes a variable, a value and one or two constant indexes")
            target = arg_nodes[0]
            for i in arg_nodes[2:]:
                k = self.eval(i)
                if not (k.is_const and k.type == "int"):
                    raise self.err("setcomp()'s indexes have to be whole-number constants")
                target = N("member", i.line, obj=target, comp=k.const)
            val = self.eval(arg_nodes[1])
            self.assign(target, val)
            return val
        if name == "promote":
            if len(arg_nodes) != 2 or arg_nodes[1].kind != "str" or arg_nodes[1].value.lower() not in DOMAIN_NAMES:
                raise self.err('promote() takes a value and a domain in quotes: "point", "edge", "face", '
                               '"corner", "curve" or "instance"')
            v = self.eval(arg_nodes[0])
            return self.gnode(v.type, "GeometryNodeFieldOnDomain", ("Value",), 0, (v,), ("Value",),
                              props=(("domain", DOMAIN_NAMES[arg_nodes[1].value.lower()]),
                                     ("data_type", DATA_TYPES[v.type])), field=True)
        a = [self.eval(x) for x in arg_nodes]
        line = self.line = arg_nodes[0].line if arg_nodes else self.line

        def want(*counts):
            if len(a) not in counts:
                expected = " or ".join(str(c) for c in counts)
                raise BexError(f"{name}() takes {expected} argument(s), got {len(a)}", line)

        def floats():
            return [self.conv(x, "float") for x in a]

        if name == "range":
            raise self.err("range() can only be used in a for-loop: for i in range(8):")
        if name == "$pick":  # for x in (a, b, c): the item for this pass
            i = self.conv(a[-1], "int")
            if i.is_const:
                return a[i.const]
            result = a[0]
            for c in range(1, len(a) - 1):
                result = self.select(self.compare("==", i, Val("int", c)), a[c], result)
            return result
        if name == "$len":
            if a[0].type not in SIZES:
                raise self.err(f"a for loop goes over range(), (a, b, c) or a vector's components, not a {a[0].type}")
            return Val("int", SIZES[a[0].type])
        if name in ("min", "max") and len(a) != 2:  # min(a, b, c...), or of one vector's components
            if len(a) == 1 and a[0].type == "vector":
                a = [self.member(a[0], i) for i in range(3)]
            if len(a) < 2:
                raise BexError(f"{name}() takes two or more values, or one vector", line)
            result = a[0]
            for x in a[1:]:
                result = self.fn2(*MATH2[name], result, x)
            return result
        if name == "getcomp":
            want(2, 3)
            idx = [x for x in a[1:]]
            if not all(i.is_const and i.type == "int" for i in idx):
                raise BexError("getcomp()'s indexes have to be whole-number constants", line)
            v = a[0]
            for i in idx:
                v = self.member(v, i.const)
            return self.conv(v, "float")
        if name in ("isnan", "isfinite"):
            want(1)
            x = self.conv(a[0], "float")
            if x.is_const:
                return Val("bool", math.isnan(x.const) if name == "isnan" else math.isfinite(x.const))
            nan = self.not_(self.node("compare", "bool", (x, x), op="EQUAL", dtype="FLOAT"))  # NaN never equals itself
            if name == "isnan":
                return nan
            big = self.node("compare", "bool", (self.fn1("ABSOLUTE", abs, x), Val("float", 3.4e38)),
                            op="LESS_THAN", dtype="FLOAT")
            return big  # NaN compares false too
        if name == "len":
            want(1)
            if a[0].type not in SIZES:
                raise BexError(f"len() of a {a[0].type}: only vectors and quaternions have components", line)
            return Val("int", SIZES[a[0].type])
        if name in MATH1:
            want(1)
            return self.fn1(*MATH1[name], a[0])
        if name in MATH2:
            want(2)
            return self.fn2(*MATH2[name], a[0], a[1])
        if name in ("log", "log10"):
            want(1)
            base = Val("float", math.e if name == "log" else 10.0)
            return self.fn2("LOGARITHM", lambda x, b: math.log(x, b), a[0], base)
        if name == "clamp":
            want(3)
            if "vector" in (a[0].type, a[1].type, a[2].type) or all(RANK[x.type] <= 1 for x in a):
                return self.fn2("MINIMUM", min, self.fn2("MAXIMUM", max, a[0], a[1]), a[2])
            x, lo, hi = floats()
            return self.fold("float", lambda v, l, h: min(max(v, l), h), x, lo, hi) or self.node("clamp", "float", (x, lo, hi))
        if name in TRANSFORM_FUNCS:
            return self.transform_call(name, a, arg_nodes, want)
        if name in ("lerp", "mix"):
            want(3)
            if "quaternion" in (a[0].type, a[1].type):
                return self.transform_call("slerp", a, arg_nodes, want)
            t = "vector" if "vector" in (a[0].type, a[1].type) else "float"
            x, y, f = self.conv(a[0], t), self.conv(a[1], t), self.conv(a[2], "float")
            return self.node("mix", t, (f, x, y), dtype=t)
        if name in ("fit", "efit", "fit01", "fit11", "smoothstep", "smooth", "smootherstep"):
            if name == "fit01":
                want(3)
                a[1:1] = [Val("float", 0.0), Val("float", 1.0)]
            elif name == "fit11":
                want(3)
                a[1:1] = [Val("float", -1.0), Val("float", 1.0)]
            elif name in ("smoothstep", "smooth", "smootherstep"):
                want(3)
                a = [a[2], a[0], a[1], Val("float", 0.0), Val("float", 1.0)]
            else:
                want(5)
            interp = {"smootherstep": "SMOOTHERSTEP", "fit": "LINEAR", "efit": "LINEAR",
                      "fit01": "LINEAR", "fit11": "LINEAR"}.get(name, "SMOOTHSTEP")
            return self.map_range(a, interp, clamp=name != "efit")
        if name == "wrap":
            want(3)
            x, lo, hi = a
            if "vector" in (x.type, lo.type, hi.type):
                x, lo, hi = (self.conv(v, "vector") for v in a)
                return self.fold("vector", _wrap, x, hi, lo) or self.node("vmath", "vector", (x, hi, lo), op="WRAP")
            x, lo, hi = floats()
            return self.fold("float", _wrap, x, hi, lo) or self.node("math", "float", (x, hi, lo), op="WRAP")
        if name in COUNT_FUNCS:
            want(1)
            return self.count(self.input_number(a[0], name), COUNT_FUNCS[name])
        if name == "nearpoint":
            want(2)
            n = self.input_number(a[0], name)
            return self.node("nearest", "int", (self.conv(a[1], "vector"),), input=n, domain="POINT")
        if name == "minpos":
            want(2)
            return self.proximity(self.input_number(a[0], name), self.conv(a[1], "vector"), 0)
        if name in TOPOLOGY:
            want(2)
            idname, output, sock, domain = TOPOLOGY[name]
            return self.topology(name, self.input_number(a[0], name), idname, output, sock, a[1], domain)
        if name == "primpoint":
            want(3)
            n = self.input_number(a[0], name)
            corner = self.topology(name, n, "GeometryNodeCornersOfFace", "Corner Index", "Face Index", a[1], "FACE",
                                   [("Sort Index", a[2])])
            return self.topology(name, n, "GeometryNodeVertexOfCorner", "Vertex Index", "Corner Index", corner,
                                 "CORNER")
        if name == "neighbour":
            want(3)
            n = self.input_number(a[0], name)
            edge = self.topology(name, n, "GeometryNodeEdgesOfVertex", "Edge Index", "Vertex Index", a[1], "POINT",
                                 [("Sort Index", a[2])])
            outs = NODE_OUTS["GeometryNodeInputMeshEdgeVertices"]
            ends = [self.node("sample", "int", (self.gnode("int", "GeometryNodeInputMeshEdgeVertices", outs, i, src=n),
                                                edge), input=n, domain="EDGE", dtype="int") for i in (0, 1)]
            return self.select(self.compare("==", ends[0], self.conv(a[1], "int")), ends[1], ends[0])
        if name in ("reflect", "faceforward"):
            want(2)
            v, w = self.conv(a[0], "vector"), self.conv(a[1], "vector")
            if name == "reflect":
                return self.node("vmath", "vector", (v, w), op="REFLECT")
            return self.node("vmath", "vector", (v, w, v), op="FACEFORWARD")  # flips v when dot(v, w) > 0
        if name == "refract":
            want(3)
            v, w, ior = self.conv(a[0], "vector"), self.conv(a[1], "vector"), self.conv(a[2], "float")
            return self.node("vmath", "vector", (v, w, ior), op="REFRACT")
        if name in ("length2", "distance2"):
            want(1 if name == "length2" else 2)
            v = self.conv(a[0], "vector")
            if name == "distance2":
                v = self.arith("-", v, self.conv(a[1], "vector"))
            if v.is_const:
                return Val("float", sum(c * c for c in v.const))
            return self.node("vmath", "float", (v, v), op="DOT_PRODUCT")
        if name == "avg":
            if not a:
                raise BexError("avg() needs at least one value", line)
            if len(a) == 1 and a[0].type == "vector":
                a = [self.member(a[0], i) for i in range(3)]
            total = a[0]
            for x in a[1:]:
                total = self.arith("+", total, x)
            return self.arith("/", total, Val("float", float(len(a))))
        if name == "invlerp":
            want(3)
            return self.map_range([a[0], a[1], a[2], Val("float", 0.0), Val("float", 1.0)], "LINEAR", clamp=False)
        if name in COLOR_MODES:
            want(1)
            return self.color(name, self.conv(a[0], "vector"))
        if name == "luminance":
            want(1)
            v = self.conv(a[0], "vector")
            if v.is_const:
                return Val("float", sum(c * w for c, w in zip(v.const, LUMINANCE)))
            return self.node("vmath", "float", (v, Val("vector", LUMINANCE)), op="DOT_PRODUCT")
        if name == "blackbody":
            want(1, 2)
            c = self.gnode("vector", "ShaderNodeBlackbody", ("Color",), 0, (self.conv(a[0], "float"),), ("Temperature",))
            return self.arith("*", c, a[1]) if len(a) == 2 else c
        if name in BBOX:
            return self.bbox(name, a, want)
        if name in ("length", "normalize"):
            want(1)
            if name == "normalize" and a[0].type == "quaternion":
                return a[0]  # Blender's rotations are always unit quaternions
            v = self.conv(a[0], "vector")
            return self.node("vmath", "float" if name == "length" else "vector", (v,), op=name.upper())
        if name in ("dot", "cross", "distance"):
            want(2)
            v, w = self.conv(a[0], "vector"), self.conv(a[1], "vector")
            op = {"dot": "DOT_PRODUCT", "cross": "CROSS_PRODUCT", "distance": "DISTANCE"}[name]
            return self.node("vmath", "vector" if name == "cross" else "float", (v, w), op=op)
        if name == "set" or (name == "vector" and len(a) == 3):
            want(3, 4) if name == "set" else want(3)
            return self.combine(a)
        if name == "vector":
            want(1, 3)
            return self.conv(a[0], "vector")
        if name == "float":
            want(1)
            return self.conv(a[0], "float")
        if name == "bool":
            want(1)
            return self.conv(a[0], "bool")
        if name == "int":
            want(1)
            if a[0].type == "float" and not a[0].is_const:
                return self.conv(self.node("math", "float", (a[0],), op="TRUNC"), "int")
            return self.conv(a[0], "int")
        if name in AGGREGATES:
            want(1, 2)
            return self.aggregate(name, a[0], a[1] if len(a) == 2 else None)
        if name == "countof":
            want(0, 1)
            return self.aggregate("sumof", Val("int", 1), a[0] if a else None)
        if name == "blur":
            want(1, 2, 3)
            v = a[0] if a[0].type in ("float", "int", "vector", "quaternion", "matrix") else self.conv(a[0], "int")
            its = self.conv(a[1], "int") if len(a) > 1 else Val("int", 1)
            weight = self.conv(a[2], "float") if len(a) > 2 else Val("float", 1.0)
            return self.gnode(v.type, "GeometryNodeBlurAttribute", ("Value",), 0, (v, its, weight),
                              ("Value", "Iterations", "Weight"), props=(("data_type", DATA_TYPES[v.type]),), field=True)
        if name == "at":
            want(2)
            v = a[0]
            return self.gnode(v.type, "GeometryNodeFieldAtIndex", ("Value",), 0, (v, self.conv(a[1], "int")),
                              ("Value", "Index"), props=(("domain", self.domain), ("data_type", DATA_TYPES[v.type])),
                              field=True)
        if name in NOISE_FUNCS or name == "curlnoise":
            want(2) if name == "flownoise" else want(1, 2)
            return self.noise_call(name, a, kw)
        if name in VORONOI_FUNCS:
            want(1, 2)
            return self.voronoi(name, a, kw)
        if name == "gabor":
            want(1)
            return self.gabor(a[0], kw)
        if name in SAMPLE_DIRS:
            return self.sample_dir(name, a, want)
        if name == "nrandom":
            want(1)
            u = self.call_val("vrand", a[0])
            r = self.fn1(*MATH1["sqrt"], self.arith("*", Val("float", -2.0), self.fn2(
                "LOGARITHM", math.log, self.fn2(*MATH2["max"], self.member(u, 0), Val("float", 1e-7)),
                Val("float", math.e))))
            return self.arith("*", r, self.fn1(*MATH1["cos"], self.arith("*", self.member(u, 1), Val("float", math.tau))))
        if name in ("rand", "vrand") and len(a) == 2:  # two seeds: 2D (two numbers) or 4D (a vector and a number)
            if a[0].type == "vector":
                ins, socks, dims = (a[0], self.conv(a[1], "float")), ("Vector", "W"), "4D"
            else:
                ins, socks, dims = (self.combine([self.conv(a[0], "float"), self.conv(a[1], "float"),
                                                  Val("float", 0.0)]),), ("Vector",), "2D"
            return self.gnode("float" if name == "rand" else "vector", "ShaderNodeTexWhiteNoise", ("Value", "Color"),
                              int(name == "vrand"), ins, socks, props=(("noise_dimensions", dims),),
                              implicit=("Vector",))
        if name in ("rand", "vrand"):
            want(1)
            if a[0].type == "vector":
                seed, dims = a[0], "3D"
            else:
                seed, dims = self.conv(a[0], "float"), "1D"
            if name == "rand":
                return self.node("white_noise", "float", (seed,), dims=dims)
            return self.node("white_noise", "vector", (seed,), out=1, dims=dims)
        raise BexError(f"unknown function '{name}()'", line)

    # -- attributes and variables -----------------------------------------
    def note_storage(self, e):
        """`u@name`: a vector kept as a 2D vector attribute."""
        storage = getattr(e, "storage", None)
        if storage:
            self.storage[e.name] = storage

    def attr_type(self, name, prefix_type):
        known = self.attr_types.get(name)
        builtin = BUILTINS.get(name)
        if builtin:
            known = builtin[0]
        if known is None:
            known = self.attr_types[name] = prefix_type or KNOWN_TYPES.get(name, "float")
        elif prefix_type and prefix_type != known:
            raise self.err(f"@{name} is a {known}, it cannot be used as {prefix_type}")
        self.attr_types[name] = known
        return known

    def read_original(self, name, type):
        if name in DOMAIN_INDEX:
            found = DOMAIN_INDEX[name].get(self.domain)
            if found is None:
                raise self.err(INDEX_HINTS[name].format(d=self.domain.lower() + "s"))
            return self.node("input", "int", (), out=found[1], name=found[0])
        if self.domain == "INSTANCE" and name in INSTANCE_ATTRS:
            return self.node("input", INSTANCE_ATTRS[name][0], (), out=0, name=INSTANCE_ATTRS[name][1])
        if name == "TimeInc":  # seconds / frame; at frame 0 assume 24 frames a second
            seconds, frame = self.node("input", "float", (), out=0, name="time"), \
                self.node("input", "float", (), out=1, name="time")
            return self.select(self.compare("==", frame, Val("float", 0.0)), Val("float", 1 / 24),
                               self.arith("/", seconds, frame))
        if name in BUILTINS:
            _, kind, out = BUILTINS[name]
            return self.node("input", type, (), out=out, name=kind)
        return self.node("attr", type, (), name=name, dtype=type)

    def read_attr(self, name, type):
        val = self.attrs.get(name)
        for deps in self.reading:  # a closure being built depends on this attribute's value
            deps.append(("attr", name, val))
        if val is not None:
            return val
        if (name, type) not in self.reads:
            self.reads.append((name, type))
        return self.read_original(name, type)

    def lookup(self, name):
        val = self.vars.get(name)
        if val is not None:
            return val
        if self.frames and name in self.frames[0]["globals"]:  # inside a function: the script's variables
            val = self.frames[0]["globals"][name]
            for deps in self.reading:
                deps.append(("var", name, val))
            return val
        if name in CONSTANTS:
            c = CONSTANTS[name]
            return Val("int" if isinstance(c, int) else "float", c)
        if self.bare_attributes:  # in the Group field a bare name is an attribute (ptnum < 5)
            return self.read_attr(name, self.attr_type(name, None))
        if name in self.loop_only:
            raise self.err(f"'{name}' is only set inside the loop on line {self.loop_only[name]}; "
                           f"give it a value before the loop")
        if name in self.half_set:
            raise self.err(f"'{name}' is only set in one branch of the if on line {self.half_set[name]}; "
                           f"give it a value before the if")
        raise self.err(f"unknown variable '{name}' (give it a value first, e.g. {name} = 0)")

    def component_index(self, e, n):
        """`v[i]`: the component number (0 to n - 1) as an int Val; negative counts from the end."""
        i = self.conv(self.eval(e.index), "int")
        self.line = e.line
        if i.is_const:
            if not -n <= i.const < n:
                raise self.err(f"component index must be 0 to {n - 1} (or negative, from the end)")
            return Val("int", i.const % n)
        return self.arith("%", i, Val("int", n))

    def assign(self, target, val):
        if target.kind == "index":
            cur = self.eval(target.obj)
            n = self.size(cur)
            i = self.component_index(target, n)
            if i.is_const:
                return self.assign(N("member", target.line, obj=target.obj, comp=i.const), val)
            val = self.conv(val, "float")
            comps = [self.select(self.compare("==", i, Val("int", c)), val, self.member(cur, c)) for c in range(n)]
            return self.assign(target.obj, self.combine(comps))
        if target.kind == "member":
            cur = self.eval(target.obj)
            self.line = target.line
            if cur.type == "row":  # m[i][j] = x
                m, r = cur.const
                self.member(cur, target.comp)
                elems = [self.member(Val("row", (m, k // 4)), k % 4) for k in range(16)]
                elems[r * 4 + target.comp % 4] = self.conv(val, "float")
                return self.assign(target.obj.obj, self.combine_matrix(elems))
            if cur.type == "matrix":
                raise self.err("m[i] is a row of a matrix: set one element with m[i][j] = value")
            comps = [self.member(cur, i) for i in range(self.size(cur))]
            self.member(cur, target.comp)  # checks the component exists
            comps[target.comp % len(comps)] = self.conv(val, "float")
            return self.assign(target.obj, self.combine(comps))
        live = self._live()
        if target.kind == "var":
            old = self.vars.get(target.name)
            if old is not None and not (live.is_const and live.const):
                val = self.select(live, val, old)  # after a break / continue: keep the old value
            self.vars[target.name] = val
            self.half_set.pop(target.name, None)
            self.loop_only.pop(target.name, None)
            return
        name = target.name
        self.note_storage(target)
        if OPINPUT_RE.match(name):
            raise self.err(f"@{name} reads another input, it cannot be written")
        fresh = not target.type and name not in self.attr_types and name not in KNOWN_TYPES and name not in BUILTINS
        type = self.attr_type(name, target.type)
        if name in READ_ONLY:
            raise self.err(f"@{name} is read-only")
        if name == "P" and self.domain not in ("POINT", "INSTANCE"):
            raise self.err("@P can only be written when the wrangle runs over Points (or Instances)")
        if name == "N" and self.domain not in ("POINT", "FACE", "CORNER"):
            raise self.err("@N (custom normals) can be written on Points, Faces or Corners")
        if fresh and val.type != type and val.type in PREFIXES:  # @a = @P: a new attribute is a float
            raise self.err(f"@{name} is a {type}; write {PREFIXES[val.type]}@{name} to store a {val.type}")
        val = self.conv(val, type)
        if self.frames:  # inside a call: only where the call itself runs
            live = self.logic("and", live, self.frames[-1]["live"])
        if not (live.is_const and live.const):
            val = self.select(live, val, self.attrs.get(name) or self.read_original(name, type))
        self.attrs[name] = val
        if name not in self.written:
            self.written.append(name)

    # -- the script's own functions ---------------------------------------------
    # A call runs the function's body right there (inlined), in its own variables,
    # reading the script's variables as they are at the call. `return` sets the
    # hidden `$ret` / `$returned` variables like `break` sets its flag, so an early
    # return under a per-element `if` works. Recursion works when it ends while
    # compiling (its condition is known then).

    def call_user(self, name, arg_nodes, want_value=True, multi=False):
        d = self.funcs[name]
        line = self.line
        values = self._bind_args(d, name, arg_nodes)
        self.line = line
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise self.err(f"more than {MAX_CALLS} function calls (a recursion that grows too fast?)")
        if len(self.frames) >= MAX_CALL_DEPTH:
            raise self.err(f"{name}() calls itself too deeply: recursion has to end while compiling, so its "
                           f"condition can't depend on attributes or inputs (use a while loop instead)")
        args = [values[p] for p, _ in d.params]
        # one closure per argument types, and per which arguments differ per element
        key = (name, tuple(v.type for v in args), tuple(_is_field(v) for v in args))
        recursive = any(f["name"] == name for f in self.frames)
        if want_value and not multi and not recursive and not all(v.is_const for v in args):
            self.call_counts[key] = self.call_counts.get(key, 0) + 1
            if key in self.closure_keys:
                out = self._find_closure(d, key)
                if out is not None:
                    self.line = line
                    return self.node("closure_call", out.params["ret"], tuple(args), closure=out)
        live = self._live() if not self.frames else self.logic("and", self._live(), self.frames[-1]["live"])
        result, returned, has_value = self._run_body(d, values, live)
        self.line = line
        if not want_value:
            return None
        result = self._result(d, name, result, returned, has_value)
        if isinstance(result, list) != multi:
            raise self.err(f"{name}() returns {len(result)} values: unpack them, e.g. a, b = {name}(...)"
                           if isinstance(result, list) else f"{name}() returns one value, not several")
        return result

    def _bind_args(self, d, name, arg_nodes):
        names = [p for p, _ in d.params]
        pos = [a for a in arg_nodes if a.kind != "kwarg"]
        if len(pos) > len(names):
            raise self.err(f"{name}() takes {len(names)} argument{'s' * (len(names) != 1)}, got {len(pos)}")
        values = {p: self.eval(a) for p, a in zip(names, pos)}
        for a in arg_nodes:
            if a.kind == "kwarg":
                self.line = a.line
                if a.name not in names:
                    raise self.err(f"{name}() has no parameter '{a.name}'")
                if a.name in values:
                    raise self.err(f"{name}() got two values for '{a.name}'")
                values[a.name] = self.eval(a.value)
        for p, default in d.params:
            if p not in values:
                if default is None:
                    raise self.err(f"{name}() is missing the argument '{p}'")
                caller = self.vars
                if self.frames:  # a default sees the script's variables, not the caller's
                    self.vars = self.frames[0]["globals"]
                values[p] = self.eval(default)
                self.vars = caller
        return values

    def _run_body(self, d, values, live):
        """Run a function body in its own variables: (return value, $returned, returns a value)."""
        caller = (self.vars, self.loops, self.half_set, self.loop_only)
        self.frames.append({"name": d.name, "live": live, "value": False,
                            "globals": self.frames[0]["globals"] if self.frames else self.vars})
        self.vars = dict(values, **{"$returned": Val("bool", False), "$ret": Val("bool", False)})
        arity = _tuple_arity(d.body)
        for n in range(arity):  # return a, b: one hidden variable per value
            self.vars[f"$ret{n}"] = Val("bool", False)
        self.loops, self.half_set, self.loop_only = [], {}, {}
        self.exec(d.body)
        result, returned = self.vars["$ret"], self.vars["$returned"]
        if arity:
            result = [self.vars[f"$ret{n}"] for n in range(arity)]
        frame = self.frames.pop()
        self.vars, self.loops, self.half_set, self.loop_only = caller
        return result, returned, frame["value"]

    def _result(self, d, name, result, returned, has_value):
        if not has_value:
            raise self.err(f"{name}() doesn't return a value")
        if not (returned.is_const and returned.const) and not _always_returns(d.body):
            raise self.err(f"{name}() doesn't return a value on every path (add a return at the end)")
        return result

    def _find_closure(self, d, key):
        """A closure for this call: one built before whose body read the same
        script variables and attribute values as they are now, else a new one.
        None: the function has to be inlined."""
        for deps, out in self.closures.get(key, []):
            if all((self.frames[0]["globals"] if self.frames else self.vars).get(n) is v if kind == "var"
                   else self.attrs.get(n) is v for kind, n, v in deps):
                return out
        deps, out = self._closure(d, key)
        if deps is None:  # can't be a closure: inline every call of it
            return None
        self.closures.setdefault(key, []).append((deps, out))
        return out

    def _closure(self, d, key):
        """Build the function body once, in a Closure zone, for these argument
        types: (what it read from outside, closure_out IRNode), or (None, None)
        when it has to be inlined (it writes attributes or runs a chunked loop,
        which need the geometry)."""
        name, types, fields = key
        line = self.line
        saved = self._snapshot()
        zone_in = IRNode("closure_in", {"items": types, "fields": fields}, (), d.line)
        values = {p: Val(t, node=zone_in, out=k) for k, ((p, _), t) in enumerate(zip(d.params, types))}
        self.reading.append([])
        result, returned, has_value = self._run_body(d, values, Val("bool", True))
        deps = self.reading.pop()
        self.line = line
        if self.attrs != saved["attrs"] or len(self.preludes) != len(saved["preludes"]) or isinstance(result, list) \
                or len(self.geo_ops) != len(saved["geo_ops"]):
            self._restore(saved)
            return None, None
        result = self._result(d, name, result, returned, has_value)
        return deps, IRNode("closure_out", {"input": zone_in, "items": types, "ret": result.type, "name": name},
                            (result,), d.line)

    # -- evaluation --------------------------------------------------------
    def eval(self, e):
        self.line = e.line
        k = e.kind
        if k == "num":
            return Val("int" if isinstance(e.value, int) else "float", e.value)
        if k == "bool":
            return Val("bool", e.value)
        if k == "val":  # an already computed value, see call_val()
            return e.val
        if k == "tuple":
            raise self.err("several values in brackets are only for: x in (a, b), for x in (a, b), a, b = ... "
                           "(a vector needs three components: (x, y, z); a quaternion four)")
        if k == "swizzle":  # v.zyx; two components give a vector with z = 0
            v = self.eval(e.obj)
            self.line = e.line
            comps = [self.member(v, c) for c in e.comps]
            return self.combine(comps + [Val("float", 0.0)] * (len(comps) == 2))
        if k == "str":
            raise self.err("text can only be used as a name inside chf(), chi(), chv() or chb()")
        if k == "var":
            return self.lookup(e.name)
        if k == "attr":
            m = OPINPUT_RE.match(e.name)
            if m:
                return self.read_opinput(int(m.group(1)), m.group(2), e.type)
            if e.name in COUNT_ATTRS:
                return self.count(0, COUNT_ATTRS[e.name])
            self.note_storage(e)
            return self.read_attr(e.name, self.attr_type(e.name, e.type))
        if k == "member":
            v = self.eval(e.obj)
            self.line = e.line
            return self.member(v, e.comp)
        if k == "index":
            v = self.eval(e.obj)
            n = self.size(v)
            i = self.component_index(e, n)
            if i.is_const:
                return self.member(v, i.const)
            comps = [self.member(v, c) for c in range(n)]
            result = comps[0]
            for c in range(1, n):
                result = self.select(self.compare("==", i, Val("int", c)), comps[c], result)
            return result
        if k == "call":
            return self.call(e.name, e.args)
        if k == "unop":
            a = self.eval(e.a)
            self.line = e.line
            if e.op == "-":
                return self.negate(a)
            if e.op == "~":
                return self.bitop("~", a, a)
            return self.not_(a)
        if k == "ternary":
            cond = self.conv(self.eval(e.cond), "bool")
            a, b = self.eval(e.a), self.eval(e.b)
            self.line = e.line
            return self.select(cond, a, b)
        if k == "binop" and e.op in ("in", "not in"):
            a = self.eval(e.a)
            items = self.items(e.b)
            self.line = e.line
            found = Val("bool", False)
            for item in items:
                found = self.logic("or", found, self.compare("==", a, item))
            return self.not_(found) if e.op == "not in" else found
        if k == "binop":
            a, b = self.eval(e.a), self.eval(e.b)
            self.line = e.line
            if e.op in ARITH or e.op in ("&", "|", "^", "<<", ">>"):
                return self.arith(e.op, a, b)
            if e.op in COMPARE:
                return self.compare(e.op, a, b)
            return self.logic(e.op, a, b)
        raise self.err(f"cannot evaluate {k}")

    def exec(self, s):
        self.line = s.line
        k = s.kind
        if k != "def":
            live = self._live()
            if live.is_const and not live.const:  # after a constant break / continue / return
                return
        if k in ("assign", "expr"):
            self.stmt_line = s.line
        if k == "block":
            for child in s.body:
                self.exec(child)
        elif k == "assign" and s.target.kind == "tuple":  # a, b = b, a: every value first, then the targets
            n = len(s.target.items)
            if s.value.kind == "call" and s.value.name in self.funcs:
                vals = self.call_user(s.value.name, s.value.args, multi=True)
            else:
                vals = self.items(s.value)
            self.line = s.line
            if len(vals) != n:
                raise self.err(f"{n} names but {len(vals)} values")
            for t, v in zip(s.target.items, vals):
                self.assign(t, v)
        elif k == "assign":
            val = self.eval(s.value)
            if s.op != "=":
                val = self.arith(s.op[:-1], self.eval(s.target), val)
            self.line = s.line
            if s.annot:
                val = self.conv(val, s.annot)
            self.assign(s.target, val)
        elif k == "expr":
            if s.expr.kind == "call" and s.expr.name in self.funcs:
                self.call_user(s.expr.name, s.expr.args, want_value=False)
            else:
                self.eval(s.expr)
        elif k == "if":
            self.exec_if(s)
        elif k == "for":
            self.exec_for(s)
        elif k == "while":
            self.exec_while(s)
        elif k in ("break", "continue"):
            self.stmt_line = s.line
            if not self.loops:
                raise self.err(f"'{k}' can only be used inside a loop")
            brk, cont = self.loops[-1]
            self._raise_flags(brk if k == "break" else cont)
        elif k == "return":
            self.stmt_line = s.line
            if not self.frames:
                raise self.err("'return' can only be used inside a function (def)")
            if s.value is not None and s.value.kind == "tuple":  # return a, b
                vals = [self.eval(x) for x in s.value.items]
                self.line = s.line
                arity = self.frames[-1].get("arity")
                if self.frames[-1]["value"] and arity != len(vals):
                    raise self.err("every return of a function has to give the same number of values")
                for n, val in enumerate(vals):
                    self.assign(N("var", s.line, name=f"$ret{n}"), val)
                self.frames[-1]["value"], self.frames[-1]["arity"] = True, len(vals)
            elif s.value is not None:
                if self.frames[-1].get("arity"):
                    raise self.err("every return of a function has to give the same number of values")
                val = self.eval(s.value)
                self.line = s.line
                self.assign(N("var", s.line, name="$ret"), val)
                self.frames[-1]["value"] = True
            self._raise_flags("$returned", *(brk for brk, _ in self.loops))  # and leaves every loop
        elif k == "def":
            if s.name in FUNCTION_NAMES or s.name in ("range", "set"):
                raise self.err(f"'{s.name}' is a built-in function, pick another name")
            self.funcs[s.name] = s

    def exec_if(self, s):
        self.stmt_line = s.line
        cond = self.conv(self.eval(s.cond), "bool")
        if cond.is_const:
            branch = s.then if cond.const else s.orelse
            if branch is not None:
                self.exec(branch)
            return
        # Run both branches from the same starting state, then merge with switches.
        base_vars, base_attrs = dict(self.vars), dict(self.attrs)
        self.path.append(cond)
        self.exec(s.then)
        self.path[-1] = self.not_(cond)
        then_vars, then_attrs = self.vars, self.attrs
        self.vars, self.attrs = dict(base_vars), base_attrs
        if s.orelse is not None:
            self.exec(s.orelse)
        self.path.pop()
        self.line = self.stmt_line = s.line
        for name in set(then_vars) | set(self.vars):
            a, b = then_vars.get(name), self.vars.get(name)
            if a is None or b is None:  # set in one branch only, so undefined on the other
                self.vars.pop(name, None)
                self.half_set[name] = s.line
            else:
                self.vars[name] = self.select(cond, a, b)
        for name in set(then_attrs) | set(self.attrs):
            type = self.attr_types[name]
            a = then_attrs.get(name) or self.read_original(name, type)
            b = self.attrs.get(name) or self.read_original(name, type)
            self.attrs[name] = self.select(cond, a, b)

    # -- loops --------------------------------------------------------------
    # A loop runs now (unrolled) when it is short or everything it changes stays
    # constant. Otherwise it is one Repeat Zone whose items are the values the body
    # changes, carried as fields, so attributes are still written once, after it.
    # `break` / `continue` set hidden bool variables (one pair per loop); while
    # one is set, assign() keeps the old values (_live), so `if` merging and zone
    # items handle them like any other variable. A loop whose length differs per
    # element runs the longest length, and each element stops changing once done.

    def _new_flags(self):
        self.loop_count += 1
        return f"$break{self.loop_count}", f"$continue{self.loop_count}"

    def _live(self):
        """Whether statements here run: no break / continue of an enclosing loop
        (in this function) is set, and the function hasn't returned."""
        live = Val("bool", True)
        for name in [n for flags in self.loops for n in flags] + ["$returned"]:
            f = self.vars.get(name)
            if f is not None:
                live = self.logic("and", live, self.not_(f))
        return live

    def _raise_flags(self, *names):
        """break / continue / return: the flags are set wherever statements run now."""
        live = self._live()
        for name in names:
            self.vars[name] = self.logic("or", self.vars.get(name, Val("bool", False)), live)

    def _range(self, s):
        args = []
        for arg in s.args:
            v = self.eval(arg)
            if v.type not in ("int", "bool"):
                raise BexError("range() needs whole numbers", s.line)
            args.append(self.conv(v, "int"))
        self.line = self.stmt_line = s.line
        if len(args) == 1:
            args = [Val("int", 0)] + args
        start, stop, step = (args + [Val("int", 1)])[:3]
        if not step.is_const:
            raise BexError("the range() step must be a fixed number", s.line)
        if step.const == 0:
            raise BexError("range() step cannot be 0", s.line)
        return start, stop, step

    def exec_for(self, s):
        start, stop, step = self._range(s)
        brk, cont = self._new_flags()
        self.vars[brk] = Val("bool", False)
        if start.is_const and stop.is_const:
            r = range(start.const, stop.const, step.const)
            if len(r) <= MAX_LOOP_ITERATIONS and self._unroll(s, list(r), brk, cont) is True:
                self._end_loop(brk, cont)
                return
        # Iterations: ceil((stop - start) / step), at least 0
        span = self.arith("-", stop, start) if step.const > 0 else self.arith("-", start, stop)
        size = abs(step.const)
        count = self.arith("//", self.arith("+", span, Val("int", size - 1)), Val("int", size))
        count = self.fn2("MAXIMUM", max, count, Val("int", 0))
        per_element = _is_field(count)
        total = count
        if per_element:  # run the longest; each element stops at its own count
            if _uses_items(count):
                raise BexError("a loop count that differs per element can't depend on values an outer "
                               "loop changes (loop to a fixed maximum and skip with if)", s.line)
            sel = self.group_sel if self.group_sel is not None else Val("bool", True)
            total = self.conv(self.node("stat", "float", (self.conv(count, "float"), sel), domain=self.domain), "int")
        names, _ = _assigned(s.body, brk, cont, [b for b, _ in self.loops])
        can_break = brk in names
        if can_break:  # elements outside the if around it, or already returned, start stopped
            self.vars[brk] = self._stopped_here()
        carry = per_element or can_break  # the loop variable keeps its value when an element stops
        chunked = can_break and self.zone_depth == 0 and not (total.is_const and total.const <= CHUNK_FIRST)
        if carry and s.var not in self.vars:
            self.vars[s.var] = start

        def begin(it):
            self.vars[cont] = Val("bool", False)
            if per_element or chunked:  # chunks can run past the count
                done = self.not_(self.compare("<", it, count))
                self.vars[brk] = self.logic("or", self.vars[brk], done)
            value = self.arith("+", self.arith("*", it, step), start)
            if carry:
                self.assign(N("var", s.line, name=s.var), value)
            else:
                self.vars[s.var] = value
                self.half_set.pop(s.var, None)

        self._zone(s, total, brk, cont, begin, ((s.var,) if carry else ()) + ((brk,) if chunked else ()), chunked)
        if not carry:  # Python keeps the loop variable at its last value (unchanged after no iterations)
            last = self.arith("-", count, Val("int", 1))
            value = self.arith("+", self.arith("*", last, step), start)
            before = self.vars.get(s.var)
            if before is not None:
                value = self.select(self.compare(">", count, Val("int", 0)), value, before)
            self.vars[s.var] = value
        self._end_loop(brk, cont)

    def exec_while(self, s):
        brk, cont = self._new_flags()
        self.vars[brk] = Val("bool", False)
        n = self._unroll(s, None, brk, cont)
        if n is not True:
            self.vars[brk] = self._stopped_here()
            def begin(it):
                self.vars[cont] = Val("bool", False)
                cond = self.conv(self.eval(s.cond), "bool")
                self.line = self.stmt_line = s.line
                self.vars[brk] = self.logic("or", self.vars[brk], self.not_(cond))

            # n: the exact count, when the condition was known each time but the body was not
            chunked = not n and self.zone_depth == 0
            count = n or (MAX_ZONE_ITERATIONS if chunked else WHILE_CAP)
            if not n and not chunked:
                self.warnings.append(f"line {s.line}: a while loop inside another loop stops after "
                                     f"{WHILE_CAP} iterations")
            self._zone(s, Val("int", count), brk, cont, begin, (brk,), chunked)
        self._end_loop(brk, cont)

    def _stopped_here(self):
        """Where a loop starting now doesn't run at all: outside the `if` branches
        around it, or after a break / continue / return (so a chunked loop can
        skip those elements instead of running them to the cap)."""
        here = self._live()
        if self.frames:
            here = self.logic("and", here, self.frames[-1]["live"])
        for c in self.path:
            here = self.logic("and", here, c)
        return self.not_(here)

    def _end_loop(self, brk, cont):
        self.vars.pop(brk, None)
        self.vars.pop(cont, None)

    def _begin_unrolled(self, s, cont, value):
        self.vars[cont] = Val("bool", False)
        if s.kind == "for":
            self.assign(N("var", s.line, name=s.var), Val("int", value))

    def _unroll(self, s, values, brk, cont):
        """Run the loop now: `values` are a for-loop's values; None for a while
        loop, whose condition is checked before each iteration. The result is kept
        (returns True) when everything the loop changes stays constant, or it ran
        at most UNROLL_MAX times. Otherwise everything is undone, and it returns the
        iteration count when that was known (a while loop with a constant
        condition but a non-constant body), else False."""
        saved = self._snapshot()
        names, attrs = _assigned(s.body, brk, cont, [b for b, _ in self.loops])
        limit = GEO_UNROLL_MAX if self._makes_geometry(s.body) else UNROLL_MAX  # zones can't add elements
        short = values is not None and len(values) <= limit
        n, all_const = 0, True
        for value in (values if values is not None else itertools.count()):
            run = self.not_(self.vars[brk])
            if s.kind == "while":
                run = self.logic("and", run, self.conv(self.eval(s.cond), "bool"))
                self.line = self.stmt_line = s.line
                if not run.is_const:  # its end differs per element
                    self._restore(saved)
                    return False
            if run.is_const and not run.const:
                break
            if n == MAX_LOOP_ITERATIONS:
                if all_const:
                    raise BexError(f"the loop runs more than {MAX_LOOP_ITERATIONS} times", s.line)
                self._restore(saved)
                return False
            n += 1
            self.loops.append((brk, cont))
            self._begin_unrolled(s, cont, value)
            self.exec(s.body)
            self.loops.pop()
            if all_const:  # attributes: also those a called function wrote
                changed = [self.vars.get(x) for x in names] + \
                    [v for a, v in self.attrs.items() if saved["attrs"].get(a) is not v]
                all_const = all(v is None or v.is_const for v in changed)
            if not all_const and values is not None and not short:
                self._restore(saved)
                return False
        if all_const or n <= limit:
            return True
        self._restore(saved)
        return n

    def _makes_geometry(self, s, seen=()):
        """Statement `s` calls addpoint() & co., directly or through the script's functions."""
        found = []

        def walk(x):
            if isinstance(x, N):
                if x.kind == "call" and (x.name in GEO_FUNCS or (x.name in self.funcs and x.name not in seen and
                                                               self._makes_geometry(self.funcs[x.name].body,
                                                                                    seen + (x.name,)))):
                    found.append(x)
                for v in x.__dict__.values():
                    walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    walk(v)
        walk(s)
        return bool(found)

    def _zone(self, s, count, brk, cont, begin, carry=(), chunked=False):
        """One Repeat Zone running the loop body `count` times (one value, not a
        field). `begin(it)` starts each iteration (it: the Iteration output).
        Items: the variables (set before the loop) and attributes the body assigns,
        plus `carry`. Their types widen, and they become fields, as the body needs.

        `chunked` (a loop that can end early, `brk` carried): an outer zone runs
        chunks of growing size, each an inner zone; between chunks the items are
        stored on the geometry (hidden attributes), and once no element is still
        running (`brk` unset) the remaining chunks are skipped. Blender's Repeat
        Zone has no stop condition, so this is how a loop ends early."""
        line = s.line
        if count.is_const and count.const > MAX_ZONE_ITERATIONS:
            raise BexError(f"the loop runs more than {MAX_ZONE_ITERATIONS} times", line)
        count = self.fn2("MINIMUM", min, count, Val("int", MAX_ZONE_ITERATIONS))
        names, attrs = _assigned(s.body, brk, cont, [b for b, _ in self.loops])
        names = (names | set(carry)) - {cont} - ({s.var} if s.kind == "for" and s.var not in carry else set())
        local = [n for n in names if n not in self.vars]  # created inside the loop
        items = [("var", n, self.vars[n]) for n in sorted(names) if n in self.vars]
        for name, prefix in attrs.items():
            if name in READ_ONLY or OPINPUT_RE.match(name):
                continue  # assign() reports it while running the body
            items.append(("attr", name, self.read_attr(name, self.attr_type(name, prefix))))
        types = [v.type for _, _, v in items]
        fields = [_is_field(v) for _, _, v in items]
        temp = [f"{TEMP_PREFIX}{self.loop_count}_{k}" for k in range(len(items))]  # chunked: hidden attribute names
        saved = self._snapshot()
        while True:
            starts = tuple(self.conv(v, t) for (_, _, v), t in zip(items, types))
            if chunked:
                fields = [True] * len(items)  # read back from the geometry
                chunks = 1
                longest = count.const if count.is_const else MAX_ZONE_ITERATIONS
                while CHUNK_FIRST * (CHUNK_GROWTH ** chunks - 1) / (CHUNK_GROWTH - 1) < longest:
                    chunks += 1
                chunk = IRNode("chunk", {"names": tuple(temp), "items": tuple(types), "count": chunks,
                                         "domain": self.domain}, starts, line)
                size, offset = Val("int", node=chunk, out=0), Val("int", node=chunk, out=1)
                starts = tuple(self.node("attr", t, name=n, dtype=t) for n, t in zip(temp, types))
                zone = IRNode("repeat_in", {"items": tuple(types), "fields": tuple(fields)}, (size,) + starts, line)
                it = self.arith("+", offset, Val("int", node=zone, out=0))
            else:
                zone = IRNode("repeat_in", {"items": tuple(types), "fields": tuple(fields)}, (count,) + starts, line)
                it = Val("int", node=zone, out=0)
            for k, ((kind, name, _), t) in enumerate(zip(items, types)):
                (self.vars if kind == "var" else self.attrs)[name] = Val(t, node=zone, out=k + 1)
            self.loops.append((brk, cont))
            self.zone_depth += 1
            begin(it)
            self.exec(s.body)
            self.zone_depth -= 1
            self.loops.pop()
            finals = [(self.vars if kind == "var" else self.attrs).get(name) for kind, name, _ in items]
            # e.g. x = 0 before the loop and x += 0.5 inside: carry a float
            wider = [f.type if f is not None and RANK[f.type] > RANK[t] else t for f, t in zip(finals, types)]
            varying = [d or (f is not None and _is_field(f)) for f, d in zip(finals, fields)]
            carried = {name for kind, name, _ in items if kind == "attr"}
            missed = [a for a, v in self.attrs.items() if a not in carried and saved["attrs"].get(a) is not v]
            if wider == types and varying == fields and not missed:
                break
            types, fields = wider, varying
            missed = [(a, self.attr_types[a]) for a in missed]
            self._restore(saved)
            for name, type in missed:  # written by a function the body calls
                v = self.read_attr(name, self.attr_type(name, type))
                items.append(("attr", name, v))
                types.append(v.type)
                fields.append(True)
                temp.append(f"{TEMP_PREFIX}{self.loop_count}_{len(temp)}")
            saved = self._snapshot()
        self.line = self.stmt_line = line
        finals = [self.conv(f, t) for f, t in zip(finals, types)]
        out = IRNode("repeat_out", {"zone": zone}, tuple(finals), line)
        for k, ((kind, name, _), t) in enumerate(zip(items, types)):
            if chunked:  # the values the chunks left on the geometry
                val = self.node("attr", t, name=temp[k], dtype=t)
            else:
                val = Val(t, node=out, out=k)
            (self.vars if kind == "var" else self.attrs)[name] = val
        if chunked:
            running = next(k for k, (kind, name, _) in enumerate(items) if name == brk)
            sel = self.group_sel if self.group_sel is not None else Val("bool", True)
            self.preludes.append(IRNode("chunk_end", {"chunk": chunk, "flag": running},
                                        tuple(Val(t, node=out, out=k) for k, t in enumerate(types)) + (sel,), line))
        for name in local:  # its value would come from inside the zone
            self.vars.pop(name, None)
            self.loop_only[name] = line
        return out

    _STATE = ("vars", "half_set", "loop_only", "attrs", "attr_types", "written", "params", "ramps",
              "reads", "pending", "inputs", "cse", "preludes", "closures", "call_counts", "calls",
              "loop_count", "warnings", "geo_ops", "geo_counts", "prim_kinds")

    def _snapshot(self):
        """Copies of everything running statements can change (see _restore)."""
        return {k: _copy(getattr(self, k)) for k in self._STATE}

    def _restore(self, saved):
        for k, v in saved.items():
            setattr(self, k, _copy(v))

    def program(self):
        prog = Program(params=[(name, t, d) for name, (t, d) in self.params.items()], pending=list(self.pending),
                       ramps=list(self.ramps), shared_ramps={r for r, ps in self.ramps.items() if len(ps) > 1},
                       reads=list(self.reads), inputs=sorted(self.inputs), preludes=list(self.preludes),
                       geo_ops=list(self.geo_ops))
        for name in self.written:
            type, val = self.attr_types[name], self.attrs[name]
            if val != self.read_original(name, type):  # `@P = @P;` writes nothing
                prog.writes.append((name, type, val))
        if self.domain == "INSTANCE":
            self._instance_transform(prog)
        return prog

    def _instance_transform(self, prog):
        """On instances, @P, @orient, @scale and @transform are parts of one instance_transform."""
        parts = {n: v for n, _, v in prog.writes if n in INSTANCE_ATTRS or n == "P"}
        if not parts:
            return
        first = next(k for k, (n, _, _) in enumerate(prog.writes) if n in parts)
        m = parts.get("transform") or self.read_original("transform", "matrix")
        if set(parts) - {"transform"}:
            sep = lambda k, t: self.gnode(t, "FunctionNodeSeparateTransform",  # noqa: E731
                                          ("Translation", "Rotation", "Scale"), k, (m,), ("Transform",))
            m = self.trs(parts.get("P") or sep(0, "vector"), parts.get("orient") or sep(1, "quaternion"),
                         parts.get("scale") or sep(2, "vector"))
        rest = [w for w in prog.writes if w[0] not in parts]
        rest.insert(min(first, len(rest)), ("instance_transform", "matrix", m))
        prog.writes[:] = rest

    # -- write plan ---------------------------------------------------------
    # Every written value means "computed from the incoming geometry". The write
    # nodes run one after another, so a value only needs a Capture when it reads
    # something an earlier write node in the chain has already changed.

    def plan_stores(self, prog, group=None):
        """Fill `prog.stores` and `prog.captures` from `prog.writes`."""
        written = {name for name, _, _ in prog.writes}
        for name, type, val in prog.writes:
            conds = [] if group is None else [group]
            if name == "N":  # Set Mesh Normal has no Selection: keep the old normal outside the Group
                if group is not None:
                    val = self.select(group, val, self.read_original("N", "vector"))
                conds = []
            else:
                val = self._peel_selection(name, type, val, conds)
            store = Store(name, type, val, conds=conds, storage=self.storage.get(name))
            if name == "P" and "position" not in written:
                off = self._position_offset(val)
                if off is not None:
                    store.value, store.offset = off, True
            store.selection = self._and_all(conds)
            prog.stores.append(store)
        # Set Position goes last: nothing after it sees the moved points.
        prog.stores.sort(key=lambda st: st.name == "P")
        changed, seen = set(), set()
        for st in prog.stores:
            units = [(("@" + st.name) + (" offset" if st.offset else ""), st.type, st.value)]
            units += [("Group" if group is not None and c is group else f"@{st.name} if", "bool", c)
                      for c in st.conds]
            for label, type, val in units:
                key = (val.node, val.out)
                if not val.is_const and key not in seen and _reads(val) & changed:
                    seen.add(key)
                    prog.captures.append((label, type, val))
            changed |= _changes(st.name)

    def _peel_selection(self, name, type, val, conds):
        """`select(c, v, original)` writes `v` where `c`, which is the write node's
        Selection: no Switch, no read of the old value."""
        orig = self.read_original(name, type)
        while not val.is_const and val.node.kind == "switch":
            c, f, t = val.node.inputs
            self.stmt_line = val.node.line
            if _same(f, orig):
                conds.append(c)
                val = t
            elif _same(t, orig):
                conds.append(self.node("bmath", "bool", (c,), op="NOT"))
                val = f
            else:
                break
        return val

    def _position_offset(self, val):
        """The offset when `val` is the position plus something (`@P += x`,
        `@P.z -= 1`, also repeated: `(@P.z + a) + b`), so Set Position's Offset
        input does the adding."""
        if val.is_const:
            return None
        pos = self.read_original("P", "vector")
        self.stmt_line = val.node.line
        if val.node.kind == "vmath":
            return self._strip_base(val, lambda v: _same(v, pos), "vmath", Val("vector", (0.0, 0.0, 0.0)))
        if val.node.kind != "combine":
            return None
        parts = []
        for i, c in enumerate(val.node.inputs):
            off = self._strip_base(c, lambda v, i=i: _is_component(v, pos, i), "math", Val("float", 0.0))
            if off is None:
                return None
            parts.append(off)
        self.stmt_line = val.node.line
        return self.combine(parts)

    def _strip_base(self, v, is_base, kind, zero):
        """`v` as base + offset through a chain of ADD / SUBTRACT nodes of `kind`:
        the offset, or None when `v` isn't the base plus something."""
        if is_base(v):
            return zero
        if v.is_const or v.node.kind != kind or v.node.params.get("op") not in ("ADD", "SUBTRACT"):
            return None
        a, b = v.node.inputs
        add = v.node.params["op"] == "ADD"
        self.stmt_line = v.node.line
        off = self._strip_base(a, is_base, kind, zero)
        if off is not None:
            return self.arith("+" if add else "-", off, b)
        off = self._strip_base(b, is_base, kind, zero) if add else None
        return None if off is None else self.arith("+", off, a)

    def _and_all(self, conds):
        sel = None
        for c in conds:
            if not c.is_const:
                self.stmt_line = c.node.line or self.stmt_line
            sel = c if sel is None else self.logic("and", sel, c)
        return sel


def _copy(x):
    if isinstance(x, dict):  # ramps hold sets, closures hold lists
        return {k: set(v) if isinstance(v, set) else list(v) if isinstance(v, list) else v for k, v in x.items()}
    if isinstance(x, list):
        return list(x)
    return x  # counters


def _assigned(body, brk=None, cont=None, outer=()):
    """Names a loop body assigns: ({variable}, {attribute: prefix type}). A
    `break` / `continue` of this loop assigns its flag `brk` / `cont`; a `return`
    also the break flags `outer` of the loops around it (in the same function)."""
    names, attrs = set(), {}

    def target(t):
        if t.kind == "tuple":
            for item in t.items:
                target(item)
            return
        while t.kind in ("member", "index"):
            t = t.obj
        if t.kind == "var":
            names.add(t.name)
        elif t.kind == "attr":
            attrs.setdefault(t.name, t.type)

    def walk(s, inner):
        if s is None:
            return
        k = s.kind
        if k == "assign":
            target(s.target)
        elif k == "return":
            names.update(("$ret", "$returned", *outer))
            if s.value is not None and s.value.kind == "tuple":
                names.update(f"$ret{n}" for n in range(len(s.value.items)))
            if brk:
                names.add(brk)
        elif k == "break" and not inner:
            names.add(brk)
        elif k == "continue" and not inner:
            names.add(cont)
        elif k == "for":
            names.add(s.var)
            walk(s.body, True)
        elif k == "while":
            walk(s.body, True)
        elif k == "if":
            walk(s.then, inner)
            walk(s.orelse, inner)
        elif k == "block":
            for c in s.body:
                walk(c, inner)

    walk(body, False)
    names.discard(None)
    return names, attrs


def _tuple_arity(s):
    """How many values `return a, b, ...` gives in this function body (0: none such)."""
    if s is None:
        return 0
    if s.kind == "return":
        return len(s.value.items) if s.value is not None and s.value.kind == "tuple" else 0
    for child in (getattr(s, "body", None) if s.kind in ("block",) else
                  [s.then, s.orelse] if s.kind == "if" else [s.body] if s.kind in ("for", "while") else []) or []:
        n = _tuple_arity(child)
        if n:
            return n
    return 0


def _always_returns(s):
    """Every path through statement `s` ends in a return (judged from the code)."""
    if s is None:
        return False
    if s.kind == "return":
        return True
    if s.kind == "block":
        return bool(s.body) and _always_returns(s.body[-1])
    if s.kind == "if":
        return _always_returns(s.then) and _always_returns(s.orelse)
    if s.kind == "while":  # `while True:` left only by return
        return s.cond.kind == "bool" and s.cond.value and not _breaks(s.body)
    return False


def _breaks(s):
    """Statement `s` has a `break` of the loop it sits in (not of a loop inside it)."""
    if s is None or s.kind in ("for", "while"):
        return False
    if s.kind == "break":
        return True
    if s.kind == "if":
        return _breaks(s.then) or _breaks(s.orelse)
    return s.kind == "block" and any(_breaks(c) for c in s.body)


def _negates(a, b):
    """`a` is NOT `b`."""
    return not a.is_const and a.node.kind == "bmath" and a.node.params.get("op") == "NOT" \
        and _same(a.node.inputs[0], b)


def _walk_outputs(val, visit):
    """Walk the (node, output) pairs `val` depends on. `visit(node, out)` returns
    True to stop with True, False to skip the node's inputs, or None to go on."""
    seen, stack = set(), [(val.node, val.out)]
    while stack:
        n, out = stack.pop()
        if n is None or (id(n), out) in seen:
            continue
        seen.add((id(n), out))
        r = visit(n, out)
        if r is True:
            return True
        if r is False:
            continue
        if n.kind == "closure_call":  # and the function body
            stack.append((n.params["closure"], 0))
        # Sample Index: the sampled value is read on the other geometry, only the index matters here
        stack.extend((v.node, v.out) for v in (n.inputs[1:] if n.kind == "sample" else n.inputs))
    return False


def _is_field(val):
    """Whether `val` can differ per element (a Repeat Zone count has to be one value)."""
    def visit(n, out):
        k = n.kind
        if "src" in n.params and k == "node":  # evaluated on another input, inside a node reading it
            return False
        if k == "node" and n.params.get("field"):
            return True
        if k == "attr" and "src" not in n.params or k == "input" and n.params["name"] != "time":
            return True
        if k == "repeat_in":
            if out == 0:  # Iteration: depends only on the count
                return _is_field(n.inputs[0]) or False
            return n.params["fields"][out - 1] or False
        if k == "repeat_out":
            return n.params["zone"].params["fields"][out] or False
        if k in ("stat", "count"):  # one value measured over the geometry
            return False
        if k == "closure_in":  # an argument: as the calls pass it (closures are per field-ness)
            return n.params["fields"][out] or False
        return None
    return _walk_outputs(val, visit)


def _uses_items(val):
    """Whether `val` reads an item of a Repeat Zone it sits in."""
    return _walk_outputs(val, lambda n, out: (out > 0 or None) if n.kind == "repeat_in" else
                         (False if n.kind == "repeat_out" else None))


def _typed_name(text):
    """`"v@vel"` -> ("vel", "vector"); `"P"` or `"@P"` -> ("P", None)."""
    prefix, _, name = text.strip().rpartition("@")
    name = name.strip()
    return ALIASES.get(name, name), _PREFIX_TYPES.get(prefix.strip())


def _same(a, b):
    """The same node output (types may differ after an implicit conversion)."""
    return not a.is_const and not b.is_const and a.node is b.node and a.out == b.out


def _is_component(v, vec, i):
    return not v.is_const and v.node.kind == "separate" and v.out == i and _same(v.node.inputs[0], vec)


def _changes(name):
    """Attribute names a write node changes, as `_reads` spells them."""
    return {"P", "position"} if name in ("P", "position") else {name}


# What each built-in input node reads from the geometry.
_INSTANCE_READS = {"instance_transform", "P", "position"}
_INPUT_READS = {"position": {"P", "position", "instance_transform"}, "normal": {"P", "position", "N"}, "id": {"id"},
                "instance_rotation": _INSTANCE_READS, "instance_scale": _INSTANCE_READS,
                "instance_transform": _INSTANCE_READS,
                "spline_parameter": {"P", "position"}, "tangent": {"P", "position"},
                "face_area": {"P", "position"}, "edge_angle": {"P", "position"}}


def _reads(val):
    """Attribute names `val` reads from the geometry it is evaluated on.
    Library groups measure the incoming geometry, so only their inputs count."""
    found, seen, stack = set(), set(), [val.node]
    while stack:
        n = stack.pop()
        if n is None or id(n) in seen:
            continue
        seen.add(id(n))
        if "src" in n.params or n.kind == "stat":  # evaluated on another input / the incoming geometry
            continue
        if n.kind == "attr":
            found.add(n.params["name"])
        elif n.kind == "input":
            found |= _INPUT_READS.get(n.params["name"], set())
        elif n.kind == "repeat_out":  # zero iterations pass the starting values through
            stack.append(n.params["zone"])
        elif n.kind == "closure_call":  # what the function body reads
            stack.append(n.params["closure"])
        stack.extend(v.node for v in n.inputs)
    return found


def _mark_groups(e):
    """In the Group field, a name used as a whole condition (`top`, `@top`,
    `not top`, `top and x`) is a group: a bool attribute. Inside a comparison or
    arithmetic it keeps its usual type, so `@mask > 0.5` reads a float."""
    if e.kind == "var" and e.name not in CONSTANTS:
        return N("attr", e.line, name=e.name, type="bool")
    if e.kind == "attr" and e.type is None and e.name not in BUILTINS and e.name not in KNOWN_TYPES:
        return N("attr", e.line, name=e.name, type="bool")
    if e.kind == "binop" and e.op in ("and", "or"):
        return N("binop", e.line, op=e.op, a=_mark_groups(e.a), b=_mark_groups(e.b))
    if e.kind == "unop" and e.op == "not":
        return N("unop", e.line, op="not", a=_mark_groups(e.a))
    return e


def compile_source(src, domain="POINT", pending_line=None, known_params=(), group=""):
    """`pending_line` (1-based) and `known_params`: see Compiler.

    `group`: which elements the script writes (Houdini's group field): a pattern
    like `0-9 top !bottom` or `* ^top` (parse_group), or an expression like
    `@P.y > 0`. A bare name is a bool attribute.
    It is read from the incoming geometry, before the script runs.
    """
    c, prog = _compile(src, domain, pending_line, known_params, group, frozenset())
    shared = frozenset(k for k, n in c.call_counts.items() if n > 1)
    if shared:  # functions called more than once: built once each, as Closures
        c, prog = _compile(src, domain, pending_line, known_params, group, shared)
    return prog


def _compile(src, domain, pending_line, known_params, group, closure_keys):
    c = Compiler(domain, pending_line, known_params)
    c.closure_keys = closure_keys
    selection, warnings = None, []
    if group.strip():
        types = dict(c.attr_types)
        try:
            c.bare_attributes, c.pending_line = True, None  # the Group is not a script line
            selection = c.conv(c.eval(_mark_groups(parse_group(group))), "bool")
        except BexError as e:
            # Like Houdini: a group that doesn't make sense selects nothing, it doesn't
            # break the node (the node shows a warning). Start over so nothing
            # half-compiled from it remains.
            c = Compiler(domain, pending_line, known_params)
            c.closure_keys = closure_keys
            selection = Val("bool", False)
            warnings.append(f"group: {e.msg} (selects nothing)")
        # Types the Group guessed (`top` is a bool) don't bind the script.
        c.bare_attributes, c.pending_line, c.attr_types = False, pending_line, types
        if selection.is_const and selection.const:
            selection = None  # always true: every element
    c.group_sel = selection
    for stmt in parse(src):
        c.exec(stmt)
    prog = c.program()
    prog.source = src.splitlines()
    prog.group, prog.group_source, prog.warnings = selection, group.strip(), warnings + c.warnings
    c.plan_stores(prog, selection)
    return c, prog
