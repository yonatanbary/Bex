# BEX Architecture

BEX is a Blender 5.2+ add-on that adds a **BEX Wrangle** geometry node. The node holds
a Python-style script with VEX-style `@attributes` (a Blender Text datablock). BEX compiles the script into ordinary
geometry nodes inside the wrangle's own node group, and recompiles whenever the script
changes. The script is edited in a code editor drawn on the node. User-facing language docs live in `README.md`.

> Keep this file current: when you add, rename or remove a module, function, IR node kind,
> built-in, or language feature, update the matching section here.

## Pipeline

```
script text ──lexer──▶ tokens ──parser──▶ AST ──compiler──▶ Program (IR graph) ──builder──▶ Blender nodes
  (Text)     lexer.py           parser.py       compiler.py                     builder.py
                                                                                     ▲
                                                   node.py: BexWrangleNode.sync() ──┘
```

`sync()` is driven by events, not a poll:

- the on-node editor (`code_editor.py`): when it loses focus (click outside, Esc, Ctrl+Enter, typing over another area);
- property updates (script, domain), undo/redo (`undo_post` / `redo_post`) and file load (`load_post`);
- one 1 s timer (`_watch`) for the only sources Blender sends no event for: Blender's Text Editor and files on disk. It does nothing for scripts that are neither open in a Text Editor nor linked to a file.

- `bex/lang/` and `bex/editor/` (its pure-Python core) have no `bpy` import, so they can be tested outside Blender.
- `bex/builder.py`, `bex/library.py`, `bex/upstream.py`, `bex/code_editor.py` and `bex/node.py` need `bpy`.

## Files

| Path | Role |
|---|---|
| `bex/__init__.py` | Add-on entry: `register()` / `unregister()` delegate to `node.py` |
| `bex/blender_manifest.toml` | Extension manifest (id `bex`, v0.1.0, Blender ≥ 5.2, GPL-3, `files` permission for disk reload) |
| `bex/lang/__init__.py` | Public API: `BexError`, `compile_source`, `Program`, `Val`, `IRNode` |
| `bex/lang/lexer.py` | `BexError`, `Token`, `tokenize()` |
| `bex/lang/parser.py` | AST node `N`, recursive-descent `Parser`, `parse()`, `parse_group()`, `parse_expression()` |
| `bex/lang/compiler.py` | Symbolic executor: AST → IR (`Compiler`, `compile_source()`) |
| `bex/editor/__init__.py` | Package for the on-node code editor (no imports) |
| `bex/editor/buffer.py` | `TextBuffer`: multi-line editing model (cursor, selection, undo) |
| `bex/editor/highlight.py` | Syntax highlighter: `highlight_line()`, `highlight()`, `KINDS` |
| `bex/editor/complete.py` | Inline autocomplete: `suggest()` |
| `bex/editor/keys.py` | Key events → buffer actions (`handle_key()`), scroll/hit-test (`View`) |
| `bex/code_editor.py` | On-node editor, Blender side: draw handler, `BEX_OT_code_edit` modal, compile on focus loss |
| `bex/upstream.py` | Static guess of the named attributes reaching a wrangle's input (`incoming_attributes()`) |
| `bex/library.py` | Library node groups (`GROUPS`, `ensure()`), and the **Add → BEX** menu |
| `bex/builder.py` | IR → nodes (`sync_interface()`, `build()`), plus per-row Group Inputs (`_split_group_inputs()`) and auto-layout (`_layout()`) |
| `bex/node.py` | The node class, channel properties, operators, Add-menu entry, update events |
| `bex/presets.py` | The **Presets** menu: built-in and user `.bex` scripts, Save as Preset |
| `bex/builtin_presets/` | Built-in preset scripts (Displace, Height Gradient, Noise Displace, Ripple) |
| `examples/ripple.bex` | Example script (linked-to-a-file workflow) |
| `tests/test_lang.py` | Compiler tests, plain `python3` |
| `tests/test_editor.py` | Editor core tests (buffer, highlighter, keys), plain `python3` |
| `tests/test_blender.py` | End-to-end tests, run in background Blender |
| `.vscode/settings.json` | Blender Development extension config (reload on save, `*.bex` as Python) |

## `bex/lang/lexer.py`

- `BexError(msg, line)`: the only user-facing error type. `str()` gives `line N: msg`.
- `Token(kind, val, line)`: kinds `num | ident | attr | str | op | newline | indent | dedent | eof`.
  - `attr` value is `(name, prefix_type)`; prefix `f/i/v/b` → `float/int/vector/bool`, else `None`.
  - `num` is `int` unless the text has `.`, `e` or `E`.
  - `.1` written right after a name (not a keyword), attribute, `)` or `]` is component access: an `op` `.` then an int (`_is_component`), so `@P.1` is `@P[1]` while `x = .5` stays a number.
- `tokenize(src)`: Python-style. One regex (`_TOKEN_RE`) for tokens; `#` comments; `newline` ends each logical line; `indent` / `dedent` from the leading whitespace (tab = 4); blank and comment-only lines are skipped; line breaks inside `(` `[` (and after a `\`) are ignored; an unclosed bracket is an error at its line. C/VEX tokens (`&& || ! ? { } ++`) raise a `_HINTS` message naming the Python form.

## `bex/lang/parser.py`

- `N(kind, line, **fields)`: generic AST node.
  - Statement kinds: `block`, `if` (`elif` is an `if` inside the `orelse` block), `for` (`var`, `args` of `range()`, `body`), `while`, `assign` (`op`, optional `annot` type from `x: int = …`), `expr`. `pass` is an empty `block`.
  - Expression kinds: `num`, `str`, `bool`, `attr`, `var`, `call`, `member`, `unop` (`-`, `not`), `binop`, `ternary`.
- `Parser`: `statement()` (if / for / while, or `simple()` statements separated by `;` up to the `newline`) with `block()` (`:` then an indented block, or statements on the same line).
  Expressions follow Python precedence: `expr()` (`a if c else b`) → `or_expr` → `and_expr` → `not_expr` → `comparison()` (chains become `and`) → `arith(level)` (`_ARITH`: `+ -`, then `* / // %`) → `unary()` → `power()` (`**`, right-associative, binds tighter than unary minus) → `postfix()` (`.x`, `.0`–`.2` and `[0]`/`[-1]` give `member`) → `primary()`.
  - `@group_name` (Houdini's spelling, `GROUP_PREFIX`) is parsed as the bool attribute `name`, in scripts and in the Group field.
  - `(a, b, c)` and `[a, b, c]` become `call("set")`.
  - `float x = …` and `true`/`false` raise errors pointing at the Python form; `def`, `return`, `break`… are `UNSUPPORTED`.
  - `_check_lvalue` allows only `var`, `attr`, or a member of one of those.
- `parse(src)` returns a list of statements. `parse_expression(src)`: one expression on its own.
- `parse_group(src)`: the wrangle's Group field. If every space-separated word matches `_PATTERN_WORD` (and none is a keyword) it is a **Houdini pattern**, read left to right into an ordinary expression AST: words are OR-ed; `!x` adds `not x`; `^x` ANDs `not x` onto what came before; `*` is `True`; `5`, `2-5`, `0-10:3` compare `@elemnum` (`_pattern_term`); a bare name is a `var`; a word containing `@` is parsed as an expression (`@P.y>0`). Anything else is parsed as a BEX expression.

## `bex/lang/compiler.py`

The script runs **symbolically**. Every value is a `Val`, which is either a compile-time constant or an output of an `IRNode`.

- `IRNode(kind, params, inputs, line)`: one future Blender node, compared by identity. `line` labels the frame it is placed in.
- `Val(type, const, node, out)`: frozen. `is_const` is true when `node is None`.
- `Program(params, writes, source, pending)`:
  - `params`: `[(name, type, default)]`, which become node input sockets.
  - `writes`: `[(attr, type, Val)]` in first-write order.
  - `source`: the script lines.
  - `pending`: ch*() names held back because they are on the line being typed (see below). The on-node editor compiles only on focus loss, so today this is used by the Text Editor watch.
  - `reads`: `[(attr, type)]` read from the incoming geometry (a read after the script's own write is not listed). Shown in the node's info strip.
  - `group`: a bool `Val` choosing the elements to write, or `None` (all). `group_source`: the Group text (labels its frame). `warnings`: e.g. a Group that failed to compile.
  - `inputs`: the extra geometry inputs (1–3) the script reads, sorted (the sockets themselves always exist).
  - `stores`: `[Store]`, the write nodes in chain order, and `captures`: `[(label, type, Val)]` to capture on the incoming geometry. Both are filled by `plan_stores()`; the builder follows them.
- `Store(name, type, value, selection, conds, offset)`: one write node. `conds` are the bool `Val`s ANDed into `selection` (the Group first). `offset`: Set Position's `value` goes into Offset.
- `compile_source(src, domain, pending_line=None, known_params=(), group="")`: a ch*() call on `pending_line` (1-based) whose name is not in `known_params` (the node's current inputs) compiles as its default constant instead of adding a param. This stops half-typed names from creating and removing sockets.
  - `group` is compiled **first** (so it reads the incoming geometry), with `bare_attributes` on (a bare name is an attribute, e.g. `ptnum < 5`) and no pending line. `_mark_groups()` makes a name used as a whole condition (`top`, `@top`, operands of `and`/`or`/`not`) a bool attribute; inside a comparison it keeps its usual type (`@mask > 0.5` is a float). Attribute types the Group inferred are reset before the script runs. A Group that fails to compile selects nothing (`Val("bool", False)`) and adds a warning; it never stops the build. An always-true group becomes `None`.
- Tables:
  - `BUILTINS` (`@P @N @ptnum @elemnum @primnum @index @id @Time @Frame`), `COUNT_ATTRS` (`@numpt @numprim @numvtx`: counts of input 0)
  - Other inputs: `MAX_INPUT = 3`, `SAMPLE_FUNCS` (`point prim vertex` → domain), `COUNT_FUNCS` (`npoints nprims nvertices`), `INPUT_SOCKETS` ("Input 2"…"Input 4", counted from 1 like a wrangle's inputs, so input 1 is "Input 2": the node's always-present extra geometry inputs, not allowed as ch*() names), `OPINPUT_RE` (`@opinputN_name`)
  - `READ_ONLY`
  - `KNOWN_TYPES` (`v Cd up` are vectors, `radius pscale` are floats; other attributes default to float)
  - `MATH1` / `MATH2` (name → Math op + Python fn)
  - `VMATH1` / `VMATH2` / `IMATH2` (ops the vector or int nodes support natively)
  - `ARITH` (Python semantics: `/` always float, `//` floor division, `%` floored modulo, `**`), `CONSTANTS` (`PI pi TAU tau M_PI`), `COMPARE`, `CHANNELS` (`chf chi chv chb`), `BBOX` (`getbbox_min/max/size/center`, `relbbox`)
  - `FUNCTION_NAMES`: every name `call` accepts (used by the editor's highlighter). Keep in sync with `call`.
- `Compiler` methods:
  - `node()` builds an IR node with **CSE** (dedup key: kind + params + inputs).
  - `conv()` does implicit type conversion. A vector never converts down to a scalar.
  - `fold()` does constant folding.
  - Operators: `arith`, `compare`, `logic` (short-circuits constants), `negate`, `select` (becomes a Switch), `member`, `combine`.
  - Functions: `fn1` / `fn2` (a vector op without a native node falls back to per-component math), `channel` (ch* inputs, honours `pending_line`), `map_range` (fit/smoothstep family, folds constants, vectors per component), `bbox` (`getbbox_*` are outputs 0–3 of one shared `group` node `bbox`; `relbbox` is group `relbbox`; an optional first argument picks the input, param `input`),
  - Other inputs: `input_number()` (a constant 0–3; using 1–3 records it in `inputs`), `read_input()` (a read tagged `src=n`, so it is its own IR node: never shared with, or captured as, the same read on this geometry, and skipped by `_reads`), `sample()` / `sample_call()` (`point(1, "v@vel", i)`, type from `_typed_name`), `count()`, `read_opinput()` (`@opinputN_name` = `sample` at this element's index on the run-over domain), `nearpoint` in `call`. `call` (dispatches every built-in function).
  - State: `attr_type` (an attribute's type is fixed on first use), `read_original`, `read_attr` (records `reads`), `lookup` (Python-style flat `vars`, then `CONSTANTS`, then, in the Group field only, an attribute; `half_set` names an `if` that set a variable in one branch only), `assign`.
  - `eval(expr)`, `exec(stmt)`, `exec_if`, `exec_for`, `exec_while`.
    - Variables need no declaration: assignment creates them and they stay until the end of the script; the type is the value's (an annotation converts).
    - `exec_if` with a non-constant condition runs **both** branches from the same starting state, then merges every variable and attribute with `select` (a per-element Switch). A variable set in only one branch is dropped and recorded in `half_set`.
    - `exec_for` unrolls `range()` (constant ints) and `exec_while` unrolls a constant condition, each at most `MAX_LOOP_ITERATIONS = 1024` times.
  - `program()` builds the `Program` and drops no-op writes such as `@P = @P`.
  - `plan_stores(prog, group)` (called last by `compile_source`) turns `writes` into `stores` and `captures`:
    - `_peel_selection`: a value `select(c, v, original)` writes `v` with Selection `c` (`NOT c` when the original is the true side; nested ifs AND), so a conditional write needs no Switch and no read of the old value.
    - `_position_offset`: `@P` = position + x (`@P += x`, `@P -= x`, per component like `@P.z += 1`) uses Set Position's Offset input. Not when `position` is also written by name.
    - Set Position goes last. A value or selection is captured only if it reads (`_reads`: named attributes, plus position/normal/id input nodes) something an earlier write node changed (`_changes`), e.g. a swap.
- **Semantics:** reads see earlier writes in the same script (sequential, like VEX). Every written value is computed from the geometry that **entered** the node (the builder captures them first).
- **IR node kinds** (must match `builder.create`): `param input attr math imath vmath compare bmath switch combine separate clamp mix maprange noise white_noise group sample nearest count`. `sample`, `nearest`, `count` and `group` carry `params["input"]`, the geometry input they read.
- **Plain nodes vs groups:** a function that is one Blender node (math, `fit`, `wrap`, `snap`…) compiles to that node. A function that needs several nodes compiles to an IR `group` node (`params["name"]` = a `library.GROUPS` key, inputs = its non-geometry inputs in order, `out` = output index), which becomes a library node group.

## `bex/editor/` (pure Python core)

Model and highlighter for the code editor drawn on the node. The Blender drawing/event side codes against this API.

- `buffer.py` — `TextBuffer(text="")`. Positions are `(row, col)`, 0-based. Indent unit is 4 spaces.
  - State: `lines` (never empty), `text` (property), `cursor`, `anchor` (`None` = no selection), `version` (bumps on every content change; the drawing side re-highlights / writes back when it changes).
  - Selection: `has_selection`, `selection_range()` (ordered or `None`), `selected_text()`, `select_all()`, `clear_selection()`.
  - Editing (replaces the selection): `insert(s)` (tabs → spaces; typing the `:` of `else:` / `elif …:` lines it up with its `if`, `_dedent_else`), `newline()` (copies indent, +1 level after a trailing `:` `(` `[`, -1 after `pass`, splits `(|)` onto three lines), `backspace(word=False)` (in leading whitespace it goes back to the previous indent stop), `delete(word=False)`, `indent()` / `dedent()` (all touched lines when there is a selection), `toggle_comment()` (`# `), `set_text(text)` (external change; undoable, no-op if equal).
  - Movement, all with `select=False` (shift-extends from `anchor`): `move_left/right(word=False)` (collapse a selection without `select`), `move_up/down` (remembered preferred column), `page_up/down(n)`, `move_home` (smart home), `move_end`, `move_doc_start/end`.
  - Mouse: `click(row, col, select=False)` (clamps), `select_word_at(row, col)` (word = `[A-Za-z0-9_@]`), `select_line(row)` (the line's text, cursor at its end, not on the next line).
  - `select_at(row, col)`: double-click selection like VS Code. It selects the word (just after a word counts as on it; `f@mask` is one word), the whole number (`0.25`), a whole string when on a quote, up to the matching bracket when on a bracket (across lines, ignoring brackets in strings; `_match_bracket`), an operator run (`+=`), or a run of spaces.
  - Undo: `undo()` / `redo()` return True if they changed something. Snapshots of text + cursor + anchor, capped at 200. Edits are grouped (`_begin_edit(kind)`): characters typed in a row are one step, and a space typed after a word, `)` or closing quote starts the next one (`@P = 1` undoes as ` = 1`, then `@P`); Backspace / Delete pressed in a row on one line are one step; a line join, newline, paste or cursor move stands alone.
- `complete.py` — `suggest(lines, row, col, attributes=())` → the text completing the word before the cursor with the best match, or "". After `@` (optionally `f@` …) it offers attributes: the script's own (most used first), then `attributes` (meant for names read from the incoming geometry), then built-ins and `KNOWN_TYPES`. Otherwise identifiers: the script's own (most used first, comments and strings skipped via the highlighter), then language names (functions, ch*, keywords, types, constants; shortest first). Returns "" mid-word or when the word is already a complete name.
- `keys.py` — `handle_key(buf, key, text, ctrl, shift, alt, cmd, mac, page, clipboard)` applies one key (Blender event type names) and returns `EXIT`, True (handled) or False (unknown; the caller passes modifier shortcuts and F-keys on to Blender). `mac=True`: Cmd = command key, Option = word moves; elsewhere Ctrl does both. Esc and Ctrl/Cmd+Enter return `EXIT`; Ctrl or Cmd with `+` `-` `0` return `ZOOM_IN` / `ZOOM_OUT` / `ZOOM_RESET`. `Clipboard` is a stand-in; `thumb()` / `thumb_first()` are the scrollbar math (thumb offset and size, and back); `View` keeps the scroll (`top`, `left`), `follow()` keeps the cursor visible, `hit()` maps a local point to `(row, col)`.
- `highlight.py`
  - `highlight_line(line)` → spans `(start, end, kind)`, contiguous from 0 to `len(line)`, adjacent same-kind spans merged. Never raises. Lines are independent (`#` comments only).
  - `highlight(lines)`: one span list per line. `KEYWORDS`, `TYPES`, `CONSTANTS` are also used by `complete.py`.
  - `KINDS`: `comment string number attr type keyword function channel constant operator punct ident text`. `function` only for a `FUNCTION_NAMES` entry followed by `(`; `float( int( vector( bool(` stay `type`.

## `bex/builder.py`

- `sync_interface(tree, program, keep_stale=False)` makes the group's sockets match the program: Geometry in/out, the three `INPUT_SOCKETS` geometry inputs (always, like a wrangle's four inputs), then `program.params`. Sockets are found by name. Sockets with an unchanged name and type are kept, so links and values survive a recompile. With `keep_stale` (a line is pending) unused inputs are not removed. Every input gets `hide_value`: the node draws the values above the code instead. Returns the newly created inputs.
- `build(tree, program, domain, keep_stale=False)` clears the tree and rebuilds it:
  - `create(ir)` maps each IR kind to a Blender node (`ShaderNodeMath`, `FunctionNodeCompare`, `GeometryNodeSwitch`, `ShaderNodeTexNoise`, …).
  - `realize()` creates nodes lazily and memoizes them.
  - `connect()` sets a socket's default for a constant, or links it for a node value. `connect_field()` is for sockets that fall back to an implicit field when unlinked, such as Position.
  - `geometry(n)`: the Group Input socket of input n (0 = Geometry, else "Input N").
  - `sample` → **Sample Index**, `nearest` → **Sample Nearest**, `count` → **Attribute Statistic** (sum of 1, so it counts mesh, point cloud and curve points together), each on `geometry(input)`.
  - `group` adds a Group node for `library.ensure(name)` with its options hidden. Its Geometry input is linked to `geometry(input)`, so it measures the geometry that entered the wrangle (or another input). Inputs with an implicit field (`default_input`) use `connect_field()`.
  - Geometry chain: if `program.captures` is not empty, one **Capture Attribute** node captures those values on the input geometry; from then on `connect()` links any captured value (keyed by IR node and output) to the Capture output. Then each `program.stores` entry gets a **Set Position** (`@P`; Position, or Offset when `offset`) or **Store Named Attribute** node on `domain`, with `selection` in its **Selection** input (the Group ANDed with any `if` conditions).
  - Returns `(node_count, created_inputs)`.
- `_split_group_inputs(...)` runs before layout. Each source line that reads node inputs gets its own **Group Input** node in its frame, wired only to that line's nodes. Unused sockets are hidden on every Group Input, including the main one, which keeps only Geometry.
- `_layout(...)`:
  - Columns are placed by depth from the Group Output, so links flow left to right.
  - Rows are bands: the geometry chain on top, then one `NodeFrame` per source line, labelled with that line's code. IR nodes from the Group expression have line 0 and get a frame labelled "Group: …" (`group_source`).
  - `estimate_height()` uses the `BUTTON_ROWS` table and skips hidden sockets, because real node sizes only exist after a redraw.
  - Add new node types to `BUTTON_ROWS`.

## `bex/upstream.py`

Blender's Python API can't read the geometry flowing into a socket, so the attributes on a wrangle's incoming geometry are a **static guess** from the node graph.

- `incoming_attributes(node)` → `[(name, bex_type)]`, sorted. Walks upstream from the Geometry input (`_from_input` / `_from_output`): union over links and all geometry inputs (Join Geometry, any pass-through node, muted nodes, reroutes). Group Input at the top level gives the attributes of the objects whose modifier runs the tree (original data, `_modifier_objects_attributes`); inside a group it continues from the group node's matching input. Group nodes are followed inside to their Group Output. Object Info gives that object's attributes. Store Named Attribute adds its (unlinked) name and type, Remove Named Attribute removes exact or wildcard matches, upstream wrangles add their compiled `writes` (only their Geometry input is followed, not "Input N"). Names starting with `.` and `position` are skipped.
- Called while drawing, so results are cached per node for `CACHE_SECONDS`; the Node Editor redraws on graph edits, which makes it live.
- Can be wrong after nodes that drop or rename attributes in other ways (Realize, Separate, Capture…), and modifiers before the BEX one are ignored.

## `bex/library.py`

Visible, reusable node groups for the built-in functions that take several nodes. They act as real nodes: users can add them from **Add → BEX** and use them in any tree.

- `Socket(name, type, default_input)`, `GroupDef(name, description, inputs, outputs, build, version, icon)`.
- `GROUPS`: `bbox` (**BEX Bounding Box**: Geometry → Min, Max, Size, Center) and `relbbox` (**BEX Relative Bounding Box**: Geometry, Position (implicit position) → Relative; nests `bbox`).
- `find(key)` looks a group up by its `bex_library` custom property, so renaming it is fine. `ensure(key)` creates it, or rebuilds its nodes in place when `bex_library_version` differs from `GroupDef.version` (the interface is only recreated when the signature changed, so links on its users survive). Groups get a fake user and colour tag `VECTOR`. Linked (library) groups are never rebuilt.
- **Bump a group's `version` when you change its `build`.**
- `BEX_OT_add_library_node` (`bex.add_library_node`, `key`): ensures the group, then calls `node.add_node` with `node_tree` set. `NODE_MT_bex_library` is the **BEX** submenu, appended under **BEX Wrangle** in the Add menu by `node._add_menu`.

## `bex/code_editor.py`

The on-node editor, Blender side. Built on `bex/editor/` (buffer, highlighter, keys).

- Placement: `reserve(layout, node)` (called last in `draw_buttons`) adds an empty label scaled to `box_height()`. `box_rect(node, region)` finds it from the node's **bottom** edge: below it come only the input socket rows (`SOCKET_ROW`) and `BOTTOM_PAD`; `SIDE` is the horizontal inset. These constants were measured from Blender 5.2 screenshots (UI scale 0.6 to 2). Python can't read widget positions, so re-measure if Blender changes node drawing. Sizes are node-editor units × `preferences.system.ui_scale` × zoom.
- The reserved block is the code box (`box_height(lines, node.code_scale)`) plus the info strip (`info_height(node)`) under it, so the strip needs no layout measuring of its own.
- `_draw()`: `SpaceNodeEditor` POST_PIXEL handler. Paints each visible wrangle's code in Blender's monospace font (`FONT_ID = 1`), clipped with a scissor clamped to the region (`_clip()`: Metal drops the whole draw for a scissor outside the region), using the calm fixed `PALETTE` (highlight kind → colour via `_COLOR_OF`: neutral text, dim punctuation, soft colours for keywords, attributes, numbers, strings). The outline (`PALETTE`): `focus` blue while edited, `error` red, `evaluated` green when `node.is_up_to_date()`. The edited one also shows selection (rows whose line break is selected fill to the box's right edge), cursor and the inline completion as ghost text (the rest of the line shifts right, `_split_spans`). Up to `MAX_ROWS = 16` lines; longer scripts scroll. `node.code_scale` scales the font and line height.
- `scrollbars(node, rect, lines, view)`: the vertical (right edge) and horizontal (bottom edge) bars, each only when the code overflows that way (`BAR_W`, `BAR_MARGIN`, `BAR_BOTTOM` under the horizontal bar, `BAR_MIN`; `BAR_HIT` widens the grab area, inside the box). Drawn on every wrangle; while editing, a press on a bar drags its thumb or jumps to the clicked spot (`_bar_press` / `_bar_drag`, `Session.bar`), and Shift+wheel scrolls sideways.
- Scrolling: each box's `View` is kept per node in `_views` (`view_for(node)`, keyed by tree and node name; cleared by `forget_buffers()`), so the scroll survives sessions. `scroll_code(node, region, event)` handles the wheel (3 rows; Shift sideways while editing) and `TRACKPADPAN` (fractional rows in `View.pan_y` / `pan_x`) and returns False when there is nothing to scroll, so the event reaches the Node Editor. `BEX_OT_code_scroll` (`bex.code_scroll`, keymap on wheel / trackpad) scrolls a box under the mouse when nobody is editing; the edit modal calls `scroll_code` itself. Ending a session resets the sideways scroll.
- `_draw_info()` / `note_lines(node)` / `info_lines(node)`: under the code, the node's error in red, then its warning in orange (each wrapped to the box width by `_wrap`, at most `ERROR_MAX_LINES`), then three small lines: `input` (named attributes probably on the incoming geometry, `upstream.incoming_attributes`), and from `node.info` `reads` (attributes the script reads from the incoming geometry) and `writes` + node count. Attributes are shown with their type prefix (`f@mask`); built-ins as `@P`.
- Undo history is per script and lives outside the session, like Houdini: `buffer_for(text)` keeps one `TextBuffer` per Text (`_buffers`, keyed by `session_uid`), so refocusing a wrangle still undoes earlier typing. Outside changes (Text Editor, Blender undo, disk) enter it via `set_text` as their own step. `forget_buffers()` runs on `load_post` (it also clears `_views`). In Blender's own undo a whole session is one step ("Edit BEX Code", pushed by `_end` only if the text changed).
- `Session`: the one wrangle being edited (tree/node names, its script's `TextBuffer` from `buffer_for`, `start_text`, `View`, `suggestion`, `clicks` / `last_press`, `bar`). `push()` writes the buffer to the Text, `pull()` takes in outside changes. `is_editing(node)`.
- `BEX_OT_code_edit` (`bex.code_edit`): bound to LEFTMOUSE in the Node Editor keymap. `invoke` passes through unless the click is on a code box (`wrangle_at()`). The modal:
  - swallows keys (so X, Tab… don't hit node shortcuts) and passes through navigation (wheel and trackpad when not over a scrollable box, MMB), unknown Ctrl/Cmd shortcuts and F-keys;
  - click places the cursor, drag selects (sideways the view follows the mouse past the box edge, even on a short row), Shift-click extends. Clicks are counted by the operator itself (`Session.clicks`: within the user's double-click time and `CLICK_SLOP` px; Blender's `DOUBLE_CLICK` is treated as a plain press). Double-click uses `buf.select_at`, or selects the whole line when past the line's end; triple-click selects the line. `DEBUG_CLICKS` (off by default) prints each click to the console and status bar. A click on another wrangle's code switches to it;
  - Ctrl/Cmd `+` `-` `0` change `node.code_scale` by `ZOOM_STEP` (0.5–3) or reset it;
  - a click outside, right-click, Esc, Ctrl+Enter, or a key pressed over another area ends it (`_end`: final sync, plus a Blender undo push when the text changed).
- Compile: every edit is written to the Text (`push`), but nothing is evaluated until focus is lost (`_end` → `node.sync()`).
- Autocomplete: after a typed word character or Backspace, `suggest()` fills `Session.suggestion` (incoming attribute names from `upstream` are offered after `@`); any other key or a click clears it. Tab with a suggestion (and no selection) inserts it instead of indenting.

## `bex/node.py`

- Constants:
  - `COMPILER_VERSION`: bump it when the generated output changes, so existing nodes recompile.
  - `WATCH_INTERVAL = 1.0`, `TYPING_IDLE = 2.0` (a Text Editor line counts as finished after this pause), `SAVED_LIMIT`.
  - `TREE_PREFIX = ".BEX "`: hidden inner groups. Wrangles inside these groups are skipped.
  - `SCRIPT_NAME = ".BEX Script"`: a wrangle's own script (`_new_text()`): hidden from Text pickers by the dot, and without a fake user, so Blender drops it on save once no node uses it (undo can still bring it back). `_is_own_script(text)`.
  - `TEMPLATE`: the default script.
  - `DOMAINS`: Point, Edge, Face, Corner, Curve, Instance.
- `BexWrangleNode(GeometryNodeCustomGroup)`, idname `GeometryNodeBexWrangle`:
  - Properties: `script` (Text, not drawn), `domain`, `group` (the Group field), `error`, `warning`, `compiled_key`, `saved_values`, `info` (JSON reads / writes / node count of the last good compile), `code_scale`.
  - `init` creates the node's own group and its own script Text from `TEMPLATE` (`_new_text()`). `copy` copies both the group and the script, so duplicates are independent. `draw_label` is "BEX Wrangle" for an own script, else "BEX: name".
  - `draw_buttons`: Group, Run Over, the error (only when there's no script; otherwise it's drawn under the code), `draw_channels()` (each ch* value as a property, greyed out with a link icon when its socket is linked) and a separator line, then a right-aligned row with the **Presets** menu and Edit (pencil), then the reserved code box (`code_editor.reserve`). The code is always shown.
  - `channels()`: the enabled non-geometry inputs. ch* values are **properties, not inputs**: `hide_channel_sockets()` (after each build, and from `update()` when links change) hides every unlinked channel socket. The sockets stay in the group interface, which carries the values and keyframes; a linked one (Ctrl+H) stays visible.
  - `is_up_to_date()`: the compiled key matches the current script, Group and domain (drives the green outline).
  - `sync(force, pending_line=None)`: the key is `version|domain|md5(group + src)` (`_key()`, + `|pending` while ch* names are held back). `warning` is set from `program.warnings`. It recompiles on change, with `keep_stale` while pending. On `BexError` it keeps the last good nodes and sets `error`. On any other exception it logs the traceback. Every rebuild calls `_redraw()`.
  - `saved_values`: JSON `{name: [socket type, value]}` written before each build (`_save_values`). A newly created input takes its remembered value when the type matches, else the script default.
- `_redraw()` tags the Node Editor, 3D Viewport, Properties and Spreadsheet areas for redraw, because a recompile from a timer sends no UI notifier.
- Updates:
  - `_watch()`: persistent 1 s timer. For scripts linked to a file it reloads changes (`_reload_from_disk`). For scripts open in a Text Editor (`_texts_in_editors`) it syncs, holding back the cursor line's ch* names while the text changed less than `TYPING_IDLE` ago (`_text_changes`). Skips the wrangle being edited on the node.
  - `_on_load` (`load_post`) forgets the editor's undo histories and scroll positions and force-syncs everything; `_on_undo` (`undo_post` / `redo_post`) syncs.
  - `sync_all(force)` reloads disk-linked Texts, then syncs every wrangle (`force=True` rebuilds even unchanged ones). It also runs once after `register()`.
- Operators:
  - `bex.edit_script`: opens the Text Editor (or a new window if none is open); creates the script if the node has none.
  - `bex.update_node`: re-reads the script's file, then forces a rebuild of this node (no button any more; available from F3 search).
  - `bex.edit_nodes`: goes inside the wrangle with `space.path.append(node_tree, node=node)`, because Blender's Tab only enters built-in group nodes. It's bound to **Tab** in the addon keymap (registered in `register()`, removed in `unregister()`); on other nodes its poll fails and Tab falls through to Blender's own group edit. Edits made inside are replaced on the next recompile.
  - `bex.recompile`: forces a rebuild of every wrangle.
- `register()` also registers `library` (its operator and menu), `presets`, `code_editor` (its operators, draw handler and the LEFTMOUSE / wheel / trackpad keymap items) and the app handlers.

## `bex/presets.py`

- Presets are plain `.bex` files: built-in ones in `BUILTIN_DIR` (`bex/builtin_presets/`), the user's in `user_dir()` (the extension's user folder via `bpy.utils.extension_path_user`, or `scripts/presets/bex` when loaded as a plain module). `builtin_presets()` / `user_presets()` list them (`_scan`), `save(name, source)` writes one (`clean_name` makes the name file-safe; same name replaces), `load_into(node, path)` replaces the node's code and syncs.
- `NODE_MT_bex_presets`: the menu (built-ins, then the user's, then Save as Preset… and Open Presets Folder). It is opened from the node's layout, which sets `context.node` (`context_pointer_set`).
- Operators: `bex.load_preset` (`filepath`, undoable), `bex.save_preset` (name dialog; remembers the node's tree and name in `invoke`, because the dialog's context has no `node`), `bex.open_presets_folder`.
- `_add_menu` adds **BEX Wrangle** to the Geometry Nodes Add menu.

## Tests

```sh
python3 tests/test_lang.py
python3 tests/test_editor.py
"/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python tests/test_blender.py
```

- `test_lang.py`: Python syntax (indentation, elif, one-line blocks, chained comparisons, `**` `//`, error hints), Python-style variables, folding, attribute types, sequencing, if→switch, scopes, the write plan (Selection instead of Switch, Set Position Offset, captures only when needed), for-unroll, channels, the pending line, CSE, functions, error messages and lines, `.0`–`.2` components, the Group (expressions, Houdini patterns, `@group_`, types, warnings), other inputs (`point`/`prim`, `@opinputN_`, counts, `nearpoint`, no captures for other-input reads, errors).
- `test_editor.py`: buffer editing, selection, movement, undo grouping (word steps, Backspace runs), VS Code-style `select_at`, `suggest`; highlighter kinds (including `@P.1`), coverage, block comments, fuzzing; key mapping (platform modifiers, clipboard, exit, zoom) and `View`.
- `test_blender.py`: real evaluated geometry. Covers bounding box / mapping functions, the library groups (shared, nested, found after a rename, rebuilt on a version bump, usable in a user tree), position and channels, attributes and sequencing, swap semantics, the write plan (conditional writes keep old values, also from another domain; Offset with Selection; captures only when needed), the face domain, keeping the last good nodes on error, sync_all and copy, hidden own scripts, presets, channels as properties, the Group (several writes, constant writes, float attributes, patterns, bad groups), other inputs (an Object Info into "Input 2": point(), @opinput1_, npoints, nearpoint, getbbox_max(1), the four inputs always present and keeping their links), scrolling, the pending line (inputs held back, not removed, values remembered), incoming attributes (object data, Store/Remove Named Attribute, upstream wrangles, group nodes), typing through the editor's key path (Tab-accepting completions, compiling only on focus loss, undo history kept across refocus and per script), and the layout (no overlaps, links flow left to right, per-row Group Inputs).
- The draw placement can only be checked with a UI: run Blender without `-b` and with `--enable-event-simulate`, then screenshot (`bpy.ops.screen.screenshot`).

## Adding a language function (checklist)

1. `compiler.py`: add it to `MATH1` / `MATH2` or a branch in `Compiler.call` (and to `FUNCTION_NAMES` if it's a `call` branch). If it is one Blender node, map it to that node (a new IR kind needs steps 2 and 3). If it needs several nodes, add a `GroupDef` to `library.GROUPS` and emit `self.node("group", type, inputs, out=i, name=key)`.
2. `builder.py`: add the IR kind to `create()`, and the node's idname to `BUTTON_ROWS`.
3. Bump `COMPILER_VERSION` in `node.py` if output for existing scripts changes.
4. Add tests in `tests/test_lang.py` (and `test_blender.py` if node wiring is involved).
5. Update the function table in `README.md` and this file.
