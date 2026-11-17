# BEX

Python-style wrangle scripting for Blender Geometry Nodes (Blender 5.2+), with VEX-style `@attributes`.

A **BEX Wrangle** node holds a script. The addon compiles the script into
ordinary geometry nodes inside the node, and recompiles whenever the text changes.

```python
amp = chf("amplitude", 0.2)              # adds an "amplitude" input to the node
@P += @N * sin(@P.x * 4 + @Time) * amp   # move points
if @P.z > 0:
    f@mask = fit(@P.z, 0, 1, 0, 1)       # write an attribute
else:
    f@mask = 0
```

## Develop with VS Code

1. Open this folder in VS Code (the *Blender Development* extension is configured in `.vscode/settings.json`).
2. `Cmd+Shift+P` → **Blender: Start**. Blender 5.2 launches with BEX loaded; its output shows in the VS Code terminal.
3. Saving a Python file reloads the addon (or run **Blender: Reload Addons**).

In Blender: Geometry Nodes editor → **Add → BEX Wrangle** (it comes with its own script).
**Click the code on the node** to edit it there, with syntax highlighting, undo and the
usual editor keys (Tab / Shift+Tab indent, Cmd/Ctrl+/ comment, Cmd/Ctrl+Z undo,
Ctrl +/− to resize the code, Ctrl 0 to reset). Double-click selects the word, number,
string or bracket pair under the mouse; double-click past the end of a line (or triple-click) selects the line.
While you type, the best completion shows as grey text after the cursor; **Tab** accepts it.
The strip under the code lists the attributes the script reads and writes.
**Esc**, **Ctrl+Enter** or a click outside finishes. The pencil icon opens the script in the Text Editor instead.

The code is evaluated when the editor loses focus. The outline shows the state: blue while
you edit, green once evaluated, red on an error (the last working nodes are kept).
Each `chf("name")` becomes a field above the code (and a socket you can link into).
Remove a channel and bring it back, and it keeps the value you set.

Scripts are Text datablocks. If one is linked to a file on disk (Text → Open, e.g.
`examples/ripple.bex`), saving that file from any editor updates the nodes. Scripts
open in Blender's Text Editor or linked to a file are checked once a second; everything
else updates by event, with no polling.

## Language

| | |
|---|---|
| Syntax | Python: indented blocks after `:`, `#` comments, one statement per line (`;` also separates). No declarations: `x = 1` creates a variable, its type follows the value; `x: int = 2.7` converts |
| Types | `float` `int` `vector` `bool`, `True` `False` |
| Attributes | `@name` (float by default), `f@` `i@` `v@` `b@` to set the type |
| Built-ins | `@P` `@N` `@ptnum` (`@elemnum` `@primnum` `@index`) `@id` `@Time` `@Frame` |
| Node inputs | `chf("name", default)` `chi` `chv` `chb` |
| Vectors | `(x, y, z)` or `[x, y, z]`, `vector(x, y, z)`, `.x .y .z` (`.r .g .b`), `v[0]` … `v[-1]` |
| Operators | `+ - * / // % **` (`/` always divides exactly, `//` floors), comparisons (chains like `0 < x <= 1`), `and or not`, `a if cond else b`, `+= -= *= /= //= %= **=` |
| Control flow | `if / elif / else` (becomes a per-element Switch), `for i in range(n)` and `while` (unrolled; the range / condition must be known at compile time), `pass` |
| Math | `sin cos tan asin acos atan atan2 sinh cosh tanh sqrt pow exp log log10 abs floor ceil round trunc frac sign mod min max clamp snap pingpong radians degrees` |
| Mapping | `lerp/mix fit efit fit01 fit11 smoothstep/smooth smootherstep wrap(x, lo, hi)`. `fit` and `wrap` also take vectors (per component) |
| Bounding box | `getbbox_min() getbbox_max() getbbox_size() getbbox_center() relbbox(p)` (0–1 inside the box). Of the geometry entering the node; `getbbox_min(0)` also works, as in VEX |
| Vector | `length distance dot cross normalize` |
| Random | `noise(p)` `vnoise(p)` `rand(seed)` `vrand(seed)` |
| Constants | `PI` / `pi`, `TAU` / `tau` |

Functions that take several nodes (the bounding box ones) build a node group, such as
**BEX Relative Bounding Box**, instead of loose nodes. These are ordinary node groups:
add them to any tree from **Add → BEX**. Single-operation functions stay plain nodes.

Statements run in order like VEX: reading an attribute after assigning it gives the
new value, and every written attribute is computed from the geometry that entered the node.
Writing `@name = …` creates or overwrites that attribute on the node's domain.
Variables live until the end of the script. One set in only one branch of an `if` that
depends on the geometry has no value on the other branch, so give it one before the `if`.
C/VEX spellings (`&&`, `!`, `? :`, `{ }`, `float x = …`) give an error that names the Python form.

## Tests

```sh
python3 tests/test_lang.py
python3 tests/test_editor.py
"/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python tests/test_blender.py
```

## Layout

- `bex/lang/`: lexer, parser, compiler (pure Python, no `bpy`)
- `bex/builder.py`: compiled program → nodes
- `bex/editor/`: the on-node editor's text model, highlighter and key mapping (pure Python)
- `bex/code_editor.py`: draws the editor on the node and handles typing
- `bex/node.py`: the Wrangle node, operators, update events
