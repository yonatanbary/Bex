"""BEX compiler: AST -> a small graph IR whose nodes map 1:1 onto Blender nodes.

The script is executed *symbolically*: every variable and attribute holds a
`Val`, which is either a compile-time constant or an output of an `IRNode`.
Constants are folded and `if` becomes a per-element Switch. Loops are unrolled
when short or constant, else they become a Repeat Zone (see the loops section).
Variables work like Python's: assigning creates them, and they are visible until
the end of the script.
"""

import itertools
import math
import re
from dataclasses import dataclass, field

from .lexer import _PREFIX_TYPES, BexError
from .parser import N, parse, parse_group

MAX_LOOP_ITERATIONS = 1024  # longest loop run at compile time
UNROLL_MAX = 8  # loops this short are unrolled even when they make nodes
MAX_ZONE_ITERATIONS = 100_000  # a Repeat Zone never runs more often than this
WHILE_CAP = 1000  # iterations of a while loop whose end differs per element (inside another loop)
TEMP_PREFIX = ".bex"  # hidden attributes a chunked loop keeps its values in; removed at the end
CHUNK_FIRST, CHUNK_GROWTH = 32, 1.5
MAX_CALL_DEPTH = 50  # nested calls of your own functions (recursion has to end while compiling)
MAX_CALLS = 10_000  # calls of your own functions in one script  # chunk k of a loop that can end early runs floor(32 * 1.5^k) times
RANK = {"bool": 0, "int": 1, "float": 2, "vector": 3}


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
    type: str  # float | int | vector | bool
    const: object = None  # python value when known at compile time
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


@dataclass
class Store:
    """One write node: Store Named Attribute, or Set Position for `P`."""
    name: str
    type: str
    value: Val  # the value to write (Set Position: the Offset when `offset`)
    selection: object = None  # bool Val: which elements are written, None = all
    conds: list = field(default_factory=list)  # the bool Vals ANDed into `selection` (Group first)
    offset: bool = False  # Set Position: `value` goes into Offset, not Position


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
READ_ONLY = {"N", "ptnum", "elemnum", "primnum", "curvenum", "vtxnum", "edgenum", "index", "Time", "Frame", "curveu", "curvelength"} | set(COUNT_ATTRS)
KNOWN_TYPES = {"v": "vector", "Cd": "vector", "up": "vector", "radius": "float", "pscale": "float"}


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
CONSTANTS = {"PI": math.pi, "M_PI": math.pi, "pi": math.pi, "TAU": math.tau, "tau": math.tau}
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
COUNT_FUNCS = {"npoints": "POINT", "nprims": "FACE", "nvertices": "CORNER"}  # npoints(1)
# Socket names count from 1 like a wrangle's inputs: input 1 is the socket "Input 2".
INPUT_SOCKETS = tuple(f"Input {n + 1}" for n in range(1, MAX_INPUT + 1))
OPINPUT_RE = re.compile(r"opinput(\d)_(\w+)$")  # @opinput1_P: same element number on input 1

FUNCTION_NAMES = frozenset(MATH1) | frozenset(MATH2) | frozenset(CHANNELS) | frozenset(RAMPS) | frozenset({
    "log", "log10", "clamp", "lerp", "mix", "fit", "efit", "fit01", "fit11", "smoothstep",
    "length", "normalize", "dot", "cross", "distance", "set", "vector", "float", "bool", "int",
    "noise", "vnoise", "rand", "vrand", "smooth", "smootherstep", "wrap", "range",
    "nearpoint",
}) | frozenset(BBOX) | frozenset(SAMPLE_FUNCS) | frozenset(COUNT_FUNCS)

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
    "trunc": (("value",), "Cuts off the decimals."),
    "frac": (("value",), "Just the decimals: frac(2.75) is 0.75."),
    "min": (("a", "b"), "The smaller of the two."),
    "max": (("a", "b"), "The larger of the two."),
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
    # Vectors
    "set": (("x", "y", "z"), "Makes a vector."),
    "length": (("vector",), "How long the vector is."),
    "normalize": (("vector",), "Same direction, length 1."),
    "dot": (("a", "b"), "Dot product of two vectors."),
    "cross": (("a", "b"), "A vector at right angles to both."),
    "distance": (("a", "b"), "Distance between two points."),
    # Types
    "vector": (("x", "y", "z"), "Makes a vector. With one value, all three are set to it."),
    "float": (("value",), "Turns value into a float."),
    "int": (("value",), "Turns value into a whole number, cutting off the decimals."),
    "bool": (("value",), "True unless value is 0."),
    # Noise and random
    "noise": (("position",), "Smooth noise, roughly between 0 and 1."),
    "vnoise": (("position",), "Smooth noise as a vector, one value per axis."),
    "rand": (("seed",), "Random number between 0 and 1. Same seed, same number."),
    "vrand": (("seed",), "Random vector, each axis between 0 and 1."),
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
    "nearpoint": (("input", "position"), "Index of the point on the input closest to position."),
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
        if v.type == type:
            return v
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
    def arith(self, op, a, b):
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

    def member(self, v, i):
        if v.type != "vector":
            raise self.err(f"only vectors have components, this is a {v.type}")
        if v.is_const:
            return Val("float", v.const[i])
        if v.node.kind == "combine":
            return v.node.inputs[i]
        return self.node("separate", "float", (v,), out=i)

    def combine(self, comps):
        comps = [self.conv(c, "float") for c in comps]
        if all(c.is_const for c in comps):
            return Val("vector", tuple(c.const for c in comps))
        return self.node("combine", "vector", comps)

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

    def read_opinput(self, n, name, prefix_type):
        if n > MAX_INPUT:
            raise self.err(f"@opinput{n}_{name}: there are only inputs 0-{MAX_INPUT}")
        self.input_number(Val("int", n), f"@opinput{n}")
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
        kw = next((a for a in arg_nodes if a.kind == "kwarg"), None)
        if kw is not None:
            self.line = kw.line
            raise self.err(f"{name}() takes no keyword arguments (only your own functions do)")
        if name in CHANNELS:
            return self.channel(name, arg_nodes)
        if name in RAMPS:
            return self.ramp(name, arg_nodes)
        if name in SAMPLE_FUNCS:
            return self.sample_call(name, arg_nodes)
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
        if name in ("lerp", "mix"):
            want(3)
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
        if name in BBOX:
            return self.bbox(name, a, want)
        if name in ("length", "normalize"):
            want(1)
            v = self.conv(a[0], "vector")
            return self.node("vmath", "float" if name == "length" else "vector", (v,), op=name.upper())
        if name in ("dot", "cross", "distance"):
            want(2)
            v, w = self.conv(a[0], "vector"), self.conv(a[1], "vector")
            op = {"dot": "DOT_PRODUCT", "cross": "CROSS_PRODUCT", "distance": "DISTANCE"}[name]
            return self.node("vmath", "vector" if name == "cross" else "float", (v, w), op=op)
        if name == "set" or (name == "vector" and len(a) == 3):
            want(3)
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
        if name in ("noise", "vnoise"):
            want(1)
            p = self.conv(a[0], "vector")
            return self.node("noise", "float", (p,)) if name == "noise" else self.node("noise", "vector", (p,), out=1)
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
            return Val("float", CONSTANTS[name])
        if self.bare_attributes:  # in the Group field a bare name is an attribute (ptnum < 5)
            return self.read_attr(name, self.attr_type(name, None))
        if name in self.loop_only:
            raise self.err(f"'{name}' is only set inside the loop on line {self.loop_only[name]}; "
                           f"give it a value before the loop")
        if name in self.half_set:
            raise self.err(f"'{name}' is only set in one branch of the if on line {self.half_set[name]}; "
                           f"give it a value before the if")
        raise self.err(f"unknown variable '{name}' (give it a value first, e.g. {name} = 0)")

    def component_index(self, e):
        """`v[i]`: the component number (0-2) as an int Val; negative counts from the end."""
        i = self.conv(self.eval(e.index), "int")
        self.line = e.line
        if i.is_const:
            if not -3 <= i.const <= 2:
                raise self.err("component index must be 0, 1 or 2 (or -1, -2, -3)")
            return Val("int", i.const % 3)
        return self.arith("%", i, Val("int", 3))

    def assign(self, target, val):
        if target.kind == "index":
            cur, i = self.eval(target.obj), self.component_index(target)
            if i.is_const:
                return self.assign(N("member", target.line, obj=target.obj, comp=i.const), val)
            val = self.conv(val, "float")
            comps = [self.select(self.compare("==", i, Val("int", c)), val, self.member(cur, c)) for c in range(3)]
            return self.assign(target.obj, self.combine(comps))
        if target.kind == "member":
            cur = self.eval(target.obj)
            comps = [self.member(cur, i) for i in range(3)]
            comps[target.comp] = self.conv(val, "float")
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
        if OPINPUT_RE.match(name):
            raise self.err(f"@{name} reads another input, it cannot be written")
        type = self.attr_type(name, target.type)
        if name in READ_ONLY:
            raise self.err(f"@{name} is read-only")
        if name == "P" and self.domain != "POINT":
            raise self.err("@P can only be written when the wrangle runs over Points")
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

    def call_user(self, name, arg_nodes, want_value=True):
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
        if want_value and not recursive and not all(v.is_const for v in args):
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
        return self._result(d, name, result, returned, has_value)

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
        self.loops, self.half_set, self.loop_only = [], {}, {}
        self.exec(d.body)
        result, returned = self.vars["$ret"], self.vars["$returned"]
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
        if self.attrs != saved["attrs"] or len(self.preludes) != len(saved["preludes"]):
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
            return self.read_attr(e.name, self.attr_type(e.name, e.type))
        if k == "member":
            v = self.eval(e.obj)
            self.line = e.line
            return self.member(v, e.comp)
        if k == "index":
            v, i = self.eval(e.obj), self.component_index(e)
            if i.is_const:
                return self.member(v, i.const)
            comps = [self.member(v, c) for c in range(3)]
            return self.select(self.compare("==", i, Val("int", 2)), comps[2],
                               self.select(self.compare("==", i, Val("int", 1)), comps[1], comps[0]))
        if k == "call":
            return self.call(e.name, e.args)
        if k == "unop":
            a = self.eval(e.a)
            self.line = e.line
            if e.op == "-":
                return self.negate(a)  # else: not
            return self.not_(a)
        if k == "ternary":
            cond = self.conv(self.eval(e.cond), "bool")
            a, b = self.eval(e.a), self.eval(e.b)
            self.line = e.line
            return self.select(cond, a, b)
        if k == "binop":
            a, b = self.eval(e.a), self.eval(e.b)
            self.line = e.line
            if e.op in ARITH:
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
            if s.value is not None:
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
        short = values is not None and len(values) <= UNROLL_MAX
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
        if all_const or n <= UNROLL_MAX:
            return True
        self._restore(saved)
        return n

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
              "loop_count", "warnings")

    def _snapshot(self):
        """Copies of everything running statements can change (see _restore)."""
        return {k: _copy(getattr(self, k)) for k in self._STATE}

    def _restore(self, saved):
        for k, v in saved.items():
            setattr(self, k, _copy(v))

    def program(self):
        prog = Program(params=[(name, t, d) for name, (t, d) in self.params.items()], pending=list(self.pending),
                       ramps=list(self.ramps), shared_ramps={r for r, ps in self.ramps.items() if len(ps) > 1},
                       reads=list(self.reads), inputs=sorted(self.inputs), preludes=list(self.preludes))
        for name in self.written:
            type, val = self.attr_types[name], self.attrs[name]
            if val != self.read_original(name, type):  # `@P = @P;` writes nothing
                prog.writes.append((name, type, val))
        return prog

    # -- write plan ---------------------------------------------------------
    # Every written value means "computed from the incoming geometry". The write
    # nodes run one after another, so a value only needs a Capture when it reads
    # something an earlier write node in the chain has already changed.

    def plan_stores(self, prog, group=None):
        """Fill `prog.stores` and `prog.captures` from `prog.writes`."""
        written = {name for name, _, _ in prog.writes}
        for name, type, val in prog.writes:
            conds = [] if group is None else [group]
            val = self._peel_selection(name, type, val, conds)
            store = Store(name, type, val, conds=conds)
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
    return name.strip(), _PREFIX_TYPES.get(prefix.strip())


def _same(a, b):
    """The same node output (types may differ after an implicit conversion)."""
    return not a.is_const and not b.is_const and a.node is b.node and a.out == b.out


def _is_component(v, vec, i):
    return not v.is_const and v.node.kind == "separate" and v.out == i and _same(v.node.inputs[0], vec)


def _changes(name):
    """Attribute names a write node changes, as `_reads` spells them."""
    return {"P", "position"} if name in ("P", "position") else {name}


# What each built-in input node reads from the geometry.
_INPUT_READS = {"position": {"P", "position"}, "normal": {"P", "position"}, "id": {"id"},
                "spline_parameter": {"P", "position"}}


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
