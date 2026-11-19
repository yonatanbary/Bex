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

## API gaps and workarounds

| Gap (5.2) | Workaround | Where |
|---|---|---|
| No way to read widget positions | measured sizes (above) | `code_editor.box_rect`, `ramp_widget.ramp_rects` |
| `layout.split` only splits by fraction | the fraction is computed from `node.width` for a fixed label column | `node._field` |
| No vertical double-arrow / small fold icons built in | our own icons as previews (`icon_value`); their pixels must be **premultiplied** alpha, or transparent parts draw white | `ramp_icons.raster` |
| `path_from_id()` can't make paths into a custom node's properties | the owner of a ramp key is found by pointer | `ramp_widget._owner` |
| Tab only enters built-in group nodes | `bex.edit_nodes` pushes the path itself, bound to Tab | `node.py` |
| No event for typing in the Text Editor or files changing on disk | a 1 s timer, only for texts open in an editor or linked to a file | `node._watch` |
| The geometry reaching a socket can't be read from Python | a static guess from the node graph | `upstream.py` |
| `view_to_region` returns whole pixels (the box jumps while zooming) | our own float view mapping | `code_editor._view_map` |
| `DOUBLE_CLICK` events arrive unreliably | clicks are counted by the operator | `code_editor` (`Session.clicks`) |
| The active tool's keymap ("Node Tool: Tweak / Select Box / Select Lasso / Select Circle", each also "(fallback)") is checked before "Node Editor", and its Shift-click `node.select` (toggle) doesn't pass through | the ramp's click item is added to those tool keymaps too (add-on items run before Blender's in the same keymap) | `node.register` |
| A layout separator's height can't be read | taken from Blender's source: `int(int(0.3 * widget unit) * factor)`, plus the row gap; not yet confirmed on screen | `ramp_widget._separator_height` |
| The Mac key labelled "delete" sends `BACK_SPACE`, not `DEL` | ramp point delete is bound to both | `node.register` |
| The Float Curve widget updates only on mouse release | moot: our ramp widget writes the curve on every move | `ramp_widget.write_curve` |

## Drawing quirks

- **Metal (macOS):** a scissor reaching outside the region drops the whole draw call.
  Scissors are clamped to the region (`code_editor._clip`).
- **blf** text drawing turns blending off. Blending is set again after text
  (`gpu.state.blend_set("ALPHA")`).
- **The sidebar and toolbar** are drawn over the node editor region, so popups keep clear of
  them (`code_editor._free_x`).
