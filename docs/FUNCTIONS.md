# BEX Functions

A glossary of everything BEX knows, grouped by what it's for. Back to the [README](../README.md).

Arguments in `[brackets]` are optional. Vectors work wherever it makes sense (per axis).

## Properties

| Function | What it does |
|---|---|
| `chf(name, [default])` | A number slider on the node |
| `chi(name, [default])` | A whole-number field on the node |
| `chv(name, [default])` | A vector field on the node |
| `chb(name, [default])` | A checkbox on the node |
| `chramp(name, position)` | A ramp on the node, read at position 0 to 1 |

## Maths

| Function | What it does |
|---|---|
| `abs(x)` | Drops the minus sign |
| `sign(x)` | -1, 0 or 1 |
| `floor(x)`, `ceil(x)` | Rounds down, rounds up |
| `round(x)`, `rint(x)` | Rounds to the nearest whole number |
| `trunc(x)` | Cuts off the decimals |
| `frac(x)` | Just the decimals: `frac(2.75)` is 0.75 |
| `min(a, b)`, `max(a, b)` | The smaller, the larger |
| `clamp(x, lo, hi)` | Keeps x between lo and hi |
| `mod(x, d)`, `fmod(x, d)` | Remainder after dividing. Never negative for a positive d |
| `wrap(x, lo, hi)` | Wraps x around to stay between lo and hi |
| `snap(x, step)` | Rounds down to a multiple of step |
| `pingpong(x, length)` | Goes from 0 to length and back, over and over |
| `sqrt(x)` | Square root |
| `pow(x, y)`, `x ** y` | x to the power of y |
| `exp(x)`, `log(x)`, `log10(x)` | e to the x, natural log, base-10 log |
| `sin cos tan (angle)` | In radians |
| `asin acos atan (x)`, `atan2(y, x)` | Back to an angle |
| `sinh cosh tanh (x)` | Hyperbolic |
| `radians(deg)`, `degrees(rad)` | Converts angles |
| `avg(a, b, ...)` | The average. Of one vector: the average of its axes |

Constants: `PI` (`pi`), `TAU` (`tau`).

## Remapping

| Function | What it does |
|---|---|
| `fit(x, old_min, old_max, new_min, new_max)` | Moves x from one range to another, clamped |
| `efit(...)` | Same, not clamped |
| `fit01(x, new_min, new_max)` | From 0–1 to the new range |
| `fit11(x, new_min, new_max)` | From -1–1 to the new range |
| `lerp(a, b, t)`, `mix(a, b, t)` | Blends from a (t = 0) to b (t = 1) |
| `invlerp(x, a, b)` | Where x sits between a (0) and b (1) |
| `smoothstep(lo, hi, x)`, `smooth` | 0 below lo, 1 above hi, a smooth curve between |
| `smootherstep(lo, hi, x)` | Same, with softer ends |

## Vectors

| Function | What it does |
|---|---|
| `(x, y, z)`, `vector(x, y, z)` | Makes a vector. `vector(1)` sets all three |
| `length(v)`, `length2(v)` | How long it is (squared: faster for comparing) |
| `distance(a, b)`, `distance2(a, b)` | Distance between two points (and squared) |
| `normalize(v)` | Same direction, length 1 |
| `dot(a, b)` | Dot product |
| `cross(a, b)` | A vector at right angles to both |
| `reflect(dir, n)` | Bounced off a surface |
| `refract(dir, n, ior)` | Bent through a surface |
| `faceforward(n, dir)` | n, flipped to face against dir |

## Colour

| Function | What it does |
|---|---|
| `hsvtorgb(hsv)`, `rgbtohsv(rgb)` | Hue/saturation/value ↔ RGB (each 0 to 1, hue wraps) |
| `hsltorgb(hsl)`, `rgbtohsl(rgb)` | Hue/saturation/lightness ↔ RGB |
| `luminance(rgb)` | How bright a colour is |
| `blackbody(kelvin, [brightness])` | The colour of something glowing at that temperature |

## Noise and random

| Function | What it does |
|---|---|
| `noise(p)` | Smooth noise, roughly 0 to 1 |
| `vnoise(p)` | Smooth noise, one value per axis |
| `rand(seed)` | A random number 0 to 1. Same seed, same number |
| `vrand(seed)` | A random vector |

## Types

| Function | What it does |
|---|---|
| `float(x)`, `int(x)`, `bool(x)` | Converts. `int` cuts off the decimals |
| `x: int = 2.7` | Converts on assignment |

## Bounding box

| Function | What it does |
|---|---|
| `getbbox_min([input])`, `getbbox_max([input])` | The lowest and highest corners |
| `getbbox_size([input])`, `getbbox_center([input])` | Size and centre |
| `relbbox([input], p)` | Where p sits in the box, 0 to 1 on each axis |

## Reading other geometry

| Function | What it does |
|---|---|
| `point(input, "name", i)` | Attribute `name` of point i. Type from the prefix: `"v@Cd"` |
| `prim(input, "name", i)` | Attribute of face i |
| `vertex(input, "name", i)` | Attribute of corner i |
| `npoints(input)`, `nprims(input)` | How many points, faces |
| `nvertices(input)`, `nedges(input)`, `ncurves(input)` | How many corners, edges, curves |
| `nearpoint(input, p)` | The number of the closest point |
| `xyzdist(input, p, [prim])` | Distance to the surface. A variable as `prim` gets the closest face |
| `minpos(input, p)` | The closest position on the surface |
| `intersect(input, origin, dir, [pos])` | The face a ray hits, or -1. The ray is as long as dir. A variable as `pos` gets the hit |

## Mesh topology

| Function | What it does |
|---|---|
| `primpoint(input, face, n)` | The point at a face's nth corner |
| `primvertexcount(input, face)` | How many corners a face has |
| `vertexpoint(input, corner)` | The point of a corner |
| `vertexprim(input, corner)` | The face of a corner |
| `vertexprimindex(input, corner)` | Which corner of its face it is: 0, 1, 2… |
| `neighbour(input, pt, n)` | The nth point connected to a point by an edge |
| `neighbourcount(input, pt)` | How many points are connected to it |

## Language

| | |
|---|---|
| Operators | `+ - * / // % **`, `== != < <= > >=` (chains like `0 < x <= 1`), `and or not`, `a if cond else b`, `+= -= *= /=` … |
| Control flow | `if` / `elif` / `else`, `for i in range(...)`, `while`, `break`, `continue`, `pass` |
| Functions | `def name(a, b=1):` … `return`. Call with keywords: `name(b=2, a=1)` |

Coming from VEX? `&&`, `!`, `? :`, `{ }` and `float x = …` give an error that tells you the Python way.
