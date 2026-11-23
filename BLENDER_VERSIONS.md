# Blender version dependencies

Everything in BEX that is hardcoded to how a specific Blender version behaves: engine
limits, measured sizes, and API gaps we work around. When BEX moves to a new Blender
version, go through this list: each entry says what to re-check and what to change.

Checked against **Blender 5.2.0 LTS** and **5.3.0** (2026-11-19): every entry below
behaves the same in both, and the full test suite passes on both.

## Engine limits

### Float Curve: a 256-step table
- **What:** a Float Curve is never evaluated from its points. Blender bakes it into a
  table of 256 steps over its 0–1 range (`CM_TABLE` in Blender's source) and reads that,
  with straight lines between steps. Both the node graph and `CurveMapping.evaluate()` do.
- **Verified (5.2, 5.3):** a spike 0.002 wide falls between table samples and evaluates
  to 0 everywhere; a wider peak at 0.5003 comes out as exactly its value at 128/256 (0.9424),
  and `evaluate(0.5003)` gives 0.8866, the straight line between two table steps.
- **BEX depends on it:** `bex/ramp.py`. Ramps are sampled into straight pieces, and
  `simplify()` drops points within `TOLERANCE` (1e-4, about 40× finer than one table step).
  A Constant step's jump is spread over one table step. Nothing narrower than 1/256 survives.
- **If it changes:** with a finer table, raise `SAMPLES` and lower `TOLERANCE` to match.
  Re-run the spike test (sample a narrow spike on a finely spaced line of points).

### String attributes: not in geometry nodes (5.2, 5.3)
- **What:** meshes can hold string attributes (`mesh.attributes.new(name, "STRING", ...)`
  works from Python), but geometry nodes can't carry text per element:
  - Store Named Attribute and Named Attribute list `STRING` in their `data_type` enum but
    filter it out at runtime: setting it raises `enum "STRING" not found`.
  - Capture Attribute refuses a `STRING` item ("Unable to create item with this socket type").
  - Strings exist only as single values (String, Value to String, Join Strings…).
- **Verified:** identical in 5.2.0 and 5.3.0. (An earlier note here expected 5.3 to add
  it; it does not.)
- **BEX depends on it:** `s@` attributes are not supported (TODO in `bex/lang/lexer.py`).
- **To re-check on a new version:** set `data_type = "STRING"` on both nodes and add a
  `STRING` capture item. When that works, add `s@` gated on `bpy.app.version`.

### Closures (5.0+)
- **BEX depends on it:** a ramp read at several positions puts its one Float Curve in a
  closure zone and reads it with one Evaluate Closure per position (`bex/builder.py`,
  `create()` for `ramp`). Fields pass through closures (verified in 5.2 and 5.3).
- **BEX also depends on it:** an own function called from several places is one closure,
  evaluated per call (`builder.create` for `closure_out` / `closure_call`). Nodes outside
  the zone that its body uses are linked in: closures capture them (verified in 5.2).
- **If it changes:** watch for changes to the Closure / Evaluate Closure items API
  (`input_items`, `output_items`, `pair_with_output`).

## Measured layout sizes

Python cannot read where Blender draws widgets on a node, so the custom-drawn parts (code
box, ramp graphs) are placed by adding up row heights measured from 5.2 screenshots
(re-checked on 5.3: same pitch, everything in place).
Units are node-editor units at zoom 1, before UI scale.

| Constant | Value | Where | What it is |
|---|---|---|---|
| `UNIT_Y` | 20 | `code_editor.py` | Blender's `UI_UNIT_Y`; a label with `scale_y = h / UNIT_Y` is exactly `h` tall |
| `SOCKET_ROW` | 22 | `code_editor.py` | one input socket row under the node body |
| `BOTTOM_PAD` | 7 | `code_editor.py` | the node's padding below its last socket |
| `SIDE` | 10.5 | `code_editor.py` | the node body's side inset |
| `BODY_TOP` | 16.7 Blender px | `ramp_widget.py` | gap between the output socket row and the first field (same at UI scale 1.0 and 1.1, Retina) |
| `TOGGLES_W` | 40 | `ramp_widget.py` | the 2x2 button grid left of a ramp graph (20-unit icon buttons) |
| `LABEL_X` / `LABEL_BASE` | 0.8 / 13.8 Blender px | `ramp_widget.py` | where Blender's layout puts a label's text in its row (x from the body's edge, baseline from the row's bottom); the ramp name is drawn by us at the same place |
| `LABEL_POINTS` | 11 | `ramp_widget.py` | Blender's UI text size |
| `NODE_PAD` | 16 | `node.py` | the node's inner margins, left + right (an estimate; only shifts the label column by a few pixels) |

- **Ramp graphs are counted down from the node's top** in Blender pixels
  (`ramp_widget._row_sizes`), with Blender's own rounding, not fixed units x UI scale:
  a row is `U.widget_unit` = `round(18 x scale) + 2 x pixel size` and the gap between
  rows is `buttonspacey` = `int(5 x scale)`, where scale is `preferences.system.ui_scale`
  (it includes the Retina factor). At Resolution Scale 1.1 on Retina that is 44 + 10,
  not 1.1 x (20 + 5): fixed units drifted a few pixels per row. Aligned columns (the
  ch*() fields) have no gaps. A label scaled by `h / UNIT_Y` is `h x widget_unit / 20` tall.
- **How to re-check:** open a saved file (no splash) in Blender without `-b`, take a
  screenshot (`bpy.ops.screen.screenshot_area`) and compare the top pixel row of a ramp's
  toggle buttons with its graph's, with one ramp's point fields open and a second ramp
  under it, at Resolution Scale 1.0 and 1.1. Compute positions in the same frame as the
  screenshot: `node.view_all` animates the zoom for a moment.

### Repeat Zones: field items, and faster than unrolling (5.2)
- **What:** a Repeat Zone item can carry a field; it is evaluated wherever the zone's
  output is used. A new Repeat Output comes with one Geometry item.
- **Measured (5.2, 2026-11-19, 100k points, `v += sin(v*3 + i*0.1)*0.01`):** evaluation
  in ms, unrolled / zone carrying a field / zone storing an attribute every iteration:
  8 iterations 0.8 / 0.6 / 1.0, 64: 4.3 / 2.2 / 6.4, 512: 22 / 13.6 / 51. Building the
  unrolled tree from Python took 0.2 s at 64 and 26 s at 512.
- **BEX depends on it:** every for loop that makes nodes is one zone, with the values it
  changes carried as field items and attributes written once after it
  (`compiler._repeat_zone`); the builder clears the default Geometry item
  (`builder.create` for `repeat_in`).
- **If it changes:** re-run the measurement (unrolled vs zone at 8, 64, 512 iterations) if
  zone overhead or field evaluation changes; check `repeat_items` / `pair_with_output`.

### Repeat Zones: no stop condition (5.2)
- **What:** the Repeat Input's only control is Iterations (sockets: Iterations, then the
  items; no properties but `paired_output`). A zone always runs the full count; there is
  no break.
- **BEX depends on it:** a loop whose length differs per element (`range(@ptnum)`, and
  later `while` / `break` / `continue`) runs the largest count, and each element stops
  changing once it is done (a "still running" flag selects old or new values).
  Finished elements are still computed every iteration.
- **Workaround (measured 5.2, 100k points, ms):** a single-value Switch inside a zone skips
  the branch it doesn't pick, so loops that can end early run in chunks (`compiler._zone`,
  `chunked`; `builder.chunked_loop`): an outer zone of chunks of 32 × 1.5^k iterations,
  each an inner zone of masked field items, storing the items between chunks and skipping
  a chunk once no element runs. Per-point counts 0–20 with cap 1000: masked full cap 59,
  fixed chunks of 64 7.3, growing chunks 4 (the compiled BEX version 6.3). A few points at
  500: masked 57, fixed 64 33, compiled BEX 23 (stores only where still running). A skipped
  chunk costs ~0.15 ms; growing chunks make a 100k cap cost ~6 ms. A known per-point count
  (`range(@ptnum % 64)`): masked zone with Attribute Statistic Max 4.0 vs chunks 5.3, so
  per-element `for` loops without `break` stay one zone.
- **Capture Attribute inside a Repeat Zone** gave zeros / wrong values in our tests (5.2),
  so chunks store named hidden attributes instead.
- **If it changes:** check every new Blender for a stop / condition input on the Repeat
  Zone (or a new loop zone). If one appears, feed it "any element still running" (an
  Attribute Statistic Max of the flag) for a real early exit, and drop the chunks.

## API gaps and workarounds

| Gap (5.2) | Workaround | Where |
|---|---|---|
| No way to read widget positions | measured sizes (above) | `code_editor.box_rect`, `ramp_widget.ramp_rects` |
| `layout.split` only splits by fraction | the fraction is computed from `node.width` for a fixed label column | `node._field` |
| Instance on Points copies point attributes to the instances, but not `radius` | Copy to Points captures `radius` (and whether it exists) on the points first | `library._build_copy_to_points` |
| Every point Blender makes has a `radius` (0.05 from Distribute Points / Mesh to Points, 0.1 from Points), so Named Attribute's Exists can't tell a set radius from the default | Copy to Points reads a real `pscale`; `radius` only behind its Scale by Radius toggle; `@pscale` is no longer an alias of `radius` | `library._build_copy_to_points`, `lexer.ALIASES` |
| Realize Instances with Realize All off: Depth 0 realizes nothing; Depth 1 realizes one level | Copy to Points uses Depth 1, so instances inside the copied geometry stay instances | `library._build_copy_to_points` |
| No vertical double-arrow / small fold icons built in | our own icons as previews (`icon_value`); their pixels must be **premultiplied** alpha, or transparent parts draw white | `ramp_icons.raster` |
| `path_from_id()` can't make paths into a custom node's properties | the owner of a ramp key is found by pointer | `ramp_widget._owner` |
| Tab only enters built-in group nodes | `bex.edit_nodes` pushes the path itself, bound to Tab | `node.py` |
| No event for typing in the Text Editor or files changing on disk | a 1 s timer, only for texts open in an editor or linked to a file | `node._watch` |
| The geometry reaching a socket can't be read from Python | a static guess from the node graph | `upstream.py` |
| `view_to_region` returns whole pixels (the box jumps while zooming) | our own float view mapping | `code_editor._view_map` |
| `DOUBLE_CLICK` events arrive unreliably | clicks are counted by the operator | `code_editor` (`Session.clicks`) |
| The active tool's keymap ("Node Tool: Tweak / Select Box / Select Lasso / Select Circle", each also "(fallback)") is checked before "Node Editor", and its Shift-click `node.select` (toggle) doesn't pass through | the ramp's click item is added to those tool keymaps too (add-on items run before Blender's in the same keymap) | `node.register` |
| A layout separator's height can't be read | taken from Blender's source: `int(int(0.3 * widget unit) * factor)`, plus the row gap (checked on screen, 5.2) | `ramp_widget._separator_height` |
| Remove Named Attribute's pattern mode is a menu socket ("Pattern Mode": "Exact" / "Wildcard"), not a node property | set through `inputs["Pattern Mode"].default_value` | `builder.build` (chunked loops' hidden attributes) |
| Raycast's Interpolation is a menu socket ("Interpolated" / "Nearest"), not a property | set through `menus` on the IR `node` | `compiler.query` |
| Raycast and Sample Nearest Surface can't output the face they found | their Attribute / Value reads Face of Corner, which they evaluate on the target's corners (checked in 5.2 by `test_data_functions`) | `compiler.query` |
| Combine Color (HSV / HSL) clamps the hue to 0–1 | a Fraction node wraps it first, like `colorsys` (which constant folding uses) | `compiler.color` |
| A Rotation socket's own value is Euler angles, not a quaternion | constant quaternions are turned into Euler angles with `mathutils` | `builder._set_default` |
| A Matrix socket has no value of its own | a constant matrix is a Combine Matrix node | `builder.build` (`connect`) |
| Rotations are always unit quaternions (Quaternion to Rotation normalizes; Rotation to Quaternion may give -q); attributes store W, X, Y, Z | BEX folds constants to unit length too, and maps Houdini's x, y, z, w onto W, X, Y, Z at the node | `compiler.combine`, `compiler.member` |
| Rotate Rotation (Global) applies Rotation, then Rotate By; Multiply Matrices is A @ B (B applies first); Axes to Rotation and Transform Point use column vectors (checked in 5.2 by `test_rotations_and_matrices`) | VEX's order (`q1 * q2` = qmultiply, `v * m1 * m2`) is mapped onto them | `compiler.qmultiply`, `compiler.matmul` |
| Multiply Matrices' two inputs are both named "Matrix" | IR `node` inputs can be given by socket number | `builder.build` (`node`) |
| No node makes a face from a list of point numbers; Fill Curve flattens to XY and drops attributes | a "poly" is a closed curve of copies of its points, Curve to Mesh with Fill Caps on a one-point path (each profile becomes one cap), then Merge Points by point number joins the corners to the points they copy (checked in 5.2 by `test_geometry_functions`) | `builder.geometry_ops` |
| Curve to Mesh's cap runs the profile backwards | that is Houdini's clockwise winding in Blender terms, so no Flip Faces | `builder.geometry_ops` |
| Merge Points averages the attributes of the points it merges, and collapses faces that become duplicates (a new face identical to an old one disappears) | the corners are full copies of their points (identical values average to themselves) | `builder.geometry_ops` (`pick`) |
| No "scatter" node: an element can't gather the values of other elements that name it | records and one marker per element are joined and sorted by Sort Elements, whose Group ID orders the groups and which is stable, so a marker comes first with its records after it; Accumulate Field / Field at Index / Field Min & Max then combine them (5.2) | `builder.gather` |
| Duplicate Elements (Point) on a mesh gives loose vertices, ordered by point and then copy, with every attribute | `pick` copies points with it, then sorts them into record order | `builder.pick` |
| A Join Geometry input lists its newest link first | links are made in reverse order | `builder.join` |
| Each component numbers its points on its own (a mesh vertex and a curve point can both be point 3) | Houdini point numbers mean the mesh and point cloud points; curve points only when there are none | `builder.split_points` |
| Set Mesh Normal (Free) has no Selection input | outside the Group, the old normal is written back | `compiler.plan_stores` |
| An instance's transform is the `instance_transform` attribute (Float 4x4, Instance domain), and Store Named Attribute can write it | @P / @orient / @scale / @transform on instances are written as one instance_transform | `compiler._instance_transform` |
| Gabor Texture has two inputs named "Orientation" (2D float, 3D vector) | given by socket number | `compiler.gabor` |
| A Voronoi Texture's unused outputs (W, Radius…) are disabled, and some can't be found by name | the IR `node` lists them anyway, falling back to any socket with that name | `builder.build` (`node`) |
| The Mac key labelled "delete" sends `BACK_SPACE`, not `DEL` | ramp point delete is bound to both | `node.register` |
| The Float Curve widget updates only on mouse release | moot: our ramp widget writes the curve on every move | `ramp_widget.write_curve` |

## Drawing quirks

- **Metal (macOS):** a scissor reaching outside the region drops the whole draw call.
  Scissors are clamped to the region (`code_editor._clip`).
- **blf** text drawing turns blending off. Blending is set again after text
  (`gpu.state.blend_set("ALPHA")`).
- **The sidebar and toolbar** are drawn over the node editor region, so popups keep clear of
  them (`code_editor._free_x`).
