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
| `ROW_H` | 25 | `ramp_widget.py` | pitch of one row of buttons / fields in the node body |
| `SEPARATOR_H` | 7 | `ramp_widget.py` | `separator(factor=0.5, type="LINE")` |
| `NODE_PAD` | 16 | `node.py` | the node's inner margins, left + right (an estimate; only shifts the label column by a few pixels) |

- **How to re-check:** run Blender without `-b`, take a screenshot
  (`bpy.ops.screen.screenshot_area`) and measure the row pitch in pixels. Divide by
  `box_rect(...)[4]` (pixels per unit) to get units. Check one ramp with its point fields
  open and a second ramp above it: an error in `ROW_H` builds up and shows as the graph
  drawn over the "Point No." row.

## API gaps and workarounds

| Gap (5.2) | Workaround | Where |
|---|---|---|
| No way to read widget positions | measured sizes (above) | `code_editor.box_rect`, `ramp_widget.ramp_rects` |
| `layout.split` only splits by fraction | the fraction is computed from `node.width` for a fixed label column | `node._field` |
| `path_from_id()` can't make paths into a custom node's properties | the owner of a ramp key is found by pointer | `ramp_widget._owner` |
| Tab only enters built-in group nodes | `bex.edit_nodes` pushes the path itself, bound to Tab | `node.py` |
| No event for typing in the Text Editor or files changing on disk | a 1 s timer, only for texts open in an editor or linked to a file | `node._watch` |
| The geometry reaching a socket can't be read from Python | a static guess from the node graph | `upstream.py` |
| `view_to_region` returns whole pixels (the box jumps while zooming) | our own float view mapping | `code_editor._view_map` |
| `DOUBLE_CLICK` events arrive unreliably | clicks are counted by the operator | `code_editor` (`Session.clicks`) |
| The Float Curve widget updates only on mouse release | moot: our ramp widget writes the curve on every move | `ramp_widget.write_curve` |

## Drawing quirks

- **Metal (macOS):** a scissor reaching outside the region drops the whole draw call.
  Scissors are clamped to the region (`code_editor._clip`).
- **blf** text drawing turns blending off. Blending is set again after text
  (`gpu.state.blend_set("ALPHA")`).
- **The sidebar and toolbar** are drawn over the node editor region, so popups keep clear of
  them (`code_editor._free_x`).
