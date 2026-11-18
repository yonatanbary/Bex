"""BEX compiler: AST -> a small graph IR whose nodes map 1:1 onto Blender nodes.

The script is executed *symbolically*: every variable and attribute holds a
`Val`, which is either a compile-time constant or an output of an `IRNode`.
Constants are folded, `if` becomes a per-element Switch, and `for` / `while`
loops are unrolled (their range or condition has to be known at compile time).
Variables work like Python's: assigning creates them, and they are visible until
the end of the script.
"""

import math
import re
from dataclasses import dataclass, field

from .lexer import _PREFIX_TYPES, BexError
from .parser import N, parse, parse_group

MAX_LOOP_ITERATIONS = 1024
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
    reads: list = field(default_factory=list)  # [(attribute, type)] read from the incoming geometry
    group: object = None  # Val (bool) choosing the elements to write, None = all
    group_source: str = ""  # the Group expression, used to label its nodes
    warnings: list = field(default_factory=list)  # problems that don't stop the build (a bad Group)
    inputs: list = field(default_factory=list)  # extra geometry inputs the script reads (1-3), sorted
    stores: list = field(default_factory=list)  # [Store] in geometry-chain order (plan_stores)
    captures: list = field(default_factory=list)  # [(label, type, Val)] to capture on the incoming geometry


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
    "index": ("int", "index", 0),
    "id": ("int", "id", 0),
    "Time": ("float", "time", 0),
    "Frame": ("float", "time", 1),
}
# Element counts of the incoming geometry (Houdini's @numpt...): name -> domain
COUNT_ATTRS = {"numpt": "POINT", "numprim": "FACE", "numvtx": "CORNER"}
READ_ONLY = {"N", "ptnum", "elemnum", "primnum", "index", "Time", "Frame"} | set(COUNT_ATTRS)
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
# Bounding box of the geometry that entered the node. The optional first argument
# is the input number, as in VEX (only input 0 exists).
BBOX = ("getbbox_min", "getbbox_max", "getbbox_size", "getbbox_center", "relbbox")
# Every function name Compiler.call accepts (used by the editor's highlighter). Keep in sync with call().
# Geometry inputs, like a wrangle's four: 0 is the incoming geometry, 1-3 are the
# node's "Input N" sockets (always there).
MAX_INPUT = 3
SAMPLE_FUNCS = {"point": "POINT", "prim": "FACE", "vertex": "CORNER"}  # point(1, "P", i) -> domain
COUNT_FUNCS = {"npoints": "POINT", "nprims": "FACE", "nvertices": "CORNER"}  # npoints(1)
INPUT_SOCKETS = tuple(f"Input {n}" for n in range(1, MAX_INPUT + 1))  # their socket names
OPINPUT_RE = re.compile(r"opinput(\d)_(\w+)$")  # @opinput1_P: same element number on input 1

FUNCTION_NAMES = frozenset(MATH1) | frozenset(MATH2) | frozenset(CHANNELS) | frozenset({
    "log", "log10", "clamp", "lerp", "mix", "fit", "efit", "fit01", "fit11", "smoothstep",
    "length", "normalize", "dot", "cross", "distance", "set", "vector", "float", "bool", "int",
    "noise", "vnoise", "rand", "vrand", "smooth", "smootherstep", "wrap", "range",
    "nearpoint",
}) | frozenset(BBOX) | frozenset(SAMPLE_FUNCS) | frozenset(COUNT_FUNCS)


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
        self.attrs = {}  # attribute name -> current Val (only attributes that were written)
        self.attr_types = {}  # attribute name -> type, fixed on first use
        self.written = []  # attribute names in first-write order
        self.params = {}  # channel name -> (type, default)
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
            raise self.err(f"{fn}()'s first argument is the input number: 0 (this node's geometry), 1, 2 or 3")
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

    def call(self, name, arg_nodes):
        if name in CHANNELS:
            return self.channel(name, arg_nodes)
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
        if name in BUILTINS:
            _, kind, out = BUILTINS[name]
            return self.node("input", type, (), out=out, name=kind)
        return self.node("attr", type, (), name=name, dtype=type)

    def read_attr(self, name, type):
        val = self.attrs.get(name)
        if val is not None:
            return val
        if (name, type) not in self.reads:
            self.reads.append((name, type))
        return self.read_original(name, type)

    def lookup(self, name):
        val = self.vars.get(name)
        if val is not None:
            return val
        if name in CONSTANTS:
            return Val("float", CONSTANTS[name])
        if self.bare_attributes:  # in the Group field a bare name is an attribute (ptnum < 5)
            return self.read_attr(name, self.attr_type(name, None))
        if name in self.half_set:
            raise self.err(f"'{name}' is only set in one branch of the if on line {self.half_set[name]}; "
                           f"give it a value before the if")
        raise self.err(f"unknown variable '{name}' (give it a value first, e.g. {name} = 0)")

    def assign(self, target, val):
        if target.kind == "member":
            cur = self.eval(target.obj)
            comps = [self.member(cur, i) for i in range(3)]
            comps[target.comp] = self.conv(val, "float")
            return self.assign(target.obj, self.combine(comps))
        if target.kind == "var":
            self.vars[target.name] = val
            self.half_set.pop(target.name, None)
            return
        name = target.name
        if OPINPUT_RE.match(name):
            raise self.err(f"@{name} reads another input, it cannot be written")
        type = self.attr_type(name, target.type)
        if name in READ_ONLY:
            raise self.err(f"@{name} is read-only")
        if name == "P" and self.domain != "POINT":
            raise self.err("@P can only be written when the wrangle runs over Points")
        self.attrs[name] = self.conv(val, type)
        if name not in self.written:
            self.written.append(name)

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
        if k == "call":
            return self.call(e.name, e.args)
        if k == "unop":
            a = self.eval(e.a)
            self.line = e.line
            if e.op == "-":
                return self.negate(a)  # else: not
            a = self.conv(a, "bool")
            return Val("bool", not a.const) if a.is_const else self.node("bmath", "bool", (a,), op="NOT")
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
            self.eval(s.expr)
        elif k == "if":
            self.exec_if(s)
        elif k == "for":
            self.exec_for(s)
        elif k == "while":
            self.exec_while(s)

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
        self.exec(s.then)
        then_vars, then_attrs = self.vars, self.attrs
        self.vars, self.attrs = dict(base_vars), base_attrs
        if s.orelse is not None:
            self.exec(s.orelse)
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

    def loop_guard(self, count, s):
        if count > MAX_LOOP_ITERATIONS:
            raise BexError(f"the loop runs more than {MAX_LOOP_ITERATIONS} times", s.line)

    def exec_for(self, s):
        bounds = []
        for arg in s.args:
            v = self.eval(arg)
            if not v.is_const or v.type not in ("int", "bool"):
                raise BexError("range() needs whole numbers known at compile time "
                               "(not attributes or inputs)", s.line)
            bounds.append(int(v.const))
        if len(bounds) > 2 and bounds[2] == 0:
            raise BexError("range() step cannot be 0", s.line)
        r = range(*bounds)
        self.loop_guard(len(r), s)
        for i in r:
            self.vars[s.var] = Val("int", i)
            self.half_set.pop(s.var, None)
            self.exec(s.body)

    def exec_while(self, s):
        count = 0
        while True:
            cond = self.conv(self.eval(s.cond), "bool")
            if not cond.is_const:
                raise BexError("a while-loop condition must be known at compile time "
                               "(it cannot depend on attributes or inputs)", s.line)
            if not cond.const:
                break
            count += 1
            self.loop_guard(count, s)
            self.exec(s.body)

    def program(self):
        prog = Program(params=[(name, t, d) for name, (t, d) in self.params.items()], pending=list(self.pending),
                       reads=list(self.reads), inputs=sorted(self.inputs))
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
        `@P.z -= 1`), so Set Position's Offset input does the adding."""
        if val.is_const:
            return None
        n, pos = val.node, self.read_original("P", "vector")
        self.stmt_line = n.line
        if n.kind == "vmath" and n.params.get("op") in ("ADD", "SUBTRACT"):
            a, b = n.inputs
            if _same(a, pos):
                return b if n.params["op"] == "ADD" else self.negate(b)
            if _same(b, pos) and n.params["op"] == "ADD":
                return a
            return None
        if n.kind != "combine":
            return None
        parts = []
        for i, c in enumerate(n.inputs):
            if _is_component(c, pos, i):
                parts.append(Val("float", 0.0))
            elif not c.is_const and c.node.kind == "math" and c.node.params.get("op") in ("ADD", "SUBTRACT"):
                a, b = c.node.inputs
                self.stmt_line = c.node.line
                if _is_component(a, pos, i):
                    parts.append(b if c.node.params["op"] == "ADD" else self.negate(b))
                elif _is_component(b, pos, i) and c.node.params["op"] == "ADD":
                    parts.append(a)
                else:
                    return None
            else:
                return None
        self.stmt_line = n.line
        return self.combine(parts)

    def _and_all(self, conds):
        sel = None
        for c in conds:
            if not c.is_const:
                self.stmt_line = c.node.line or self.stmt_line
            sel = c if sel is None else self.logic("and", sel, c)
        return sel


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
_INPUT_READS = {"position": {"P", "position"}, "normal": {"P", "position"}, "id": {"id"}}


def _reads(val):
    """Attribute names `val` reads from the geometry it is evaluated on.
    Library groups measure the incoming geometry, so only their inputs count."""
    found, seen, stack = set(), set(), [val.node]
    while stack:
        n = stack.pop()
        if n is None or id(n) in seen:
            continue
        seen.add(id(n))
        if "src" in n.params:  # evaluated on another input, not on this geometry
            continue
        if n.kind == "attr":
            found.add(n.params["name"])
        elif n.kind == "input":
            found |= _INPUT_READS.get(n.params["name"], set())
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
    c = Compiler(domain, pending_line, known_params)
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
            selection = Val("bool", False)
            warnings.append(f"group: {e.msg} (selects nothing)")
        # Types the Group guessed (`top` is a bool) don't bind the script.
        c.bare_attributes, c.pending_line, c.attr_types = False, pending_line, types
        if selection.is_const and selection.const:
            selection = None  # always true: every element
    for stmt in parse(src):
        c.exec(stmt)
    prog = c.program()
    prog.source = src.splitlines()
    prog.group, prog.group_source, prog.warnings = selection, group.strip(), warnings
    c.plan_stores(prog, selection)
    return prog
