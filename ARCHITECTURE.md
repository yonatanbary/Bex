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
| `bex/lang/parser.py` | AST node `N`, recursive-descent `Parser`, `parse()` |
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
| `bex/node.py` | The node class, channel fields, operators, Add-menu entry, update events |
| `examples/ripple.bex` | Example script |
| `tests/test_lang.py` | Compiler tests, plain `python3` |
| `tests/test_editor.py` | Editor core tests (buffer, highlighter, keys), plain `python3` |
| `tests/test_blender.py` | End-to-end tests, run in background Blender |
| `.vscode/settings.json` | Blender Development extension config (reload on save, `*.bex` as Python) |

## `bex/lang/lexer.py`

- `BexError(msg, line)`: the only user-facing error type. `str()` gives `line N: msg`.
- `Token(kind, val, line)`: kinds `num | ident | attr | str | op | newline | indent | dedent | eof`.
  - `attr` value is `(name, prefix_type)`; prefix `f/i/v/b` → `float/int/vector/bool`, else `None`.
  - `num` is `int` unless the text has `.`, `e` or `E`.
- `tokenize(src)`: Python-style. One regex (`_TOKEN_RE`) for tokens; `#` comments; `newline` ends each logical line; `indent` / `dedent` from the leading whitespace (tab = 4); blank and comment-only lines are skipped; line breaks inside `(` `[` (and after a `\`) are ignored; an unclosed bracket is an error at its line. C/VEX tokens (`&& || ! ? { } ++`) raise a `_HINTS` message naming the Python form.

## `bex/lang/parser.py`

- `N(kind, line, **fields)`: generic AST node.
  - Statement kinds: `block`, `if` (`elif` is an `if` inside the `orelse` block), `for` (`var`, `args` of `range()`, `body`), `while`, `assign` (`op`, optional `annot` type from `x: int = …`), `expr`. `pass` is an empty `block`.
  - Expression kinds: `num`, `str`, `bool`, `attr`, `var`, `call`, `member`, `unop` (`-`, `not`), `binop`, `ternary`.
- `Parser`: `statement()` (if / for / while, or `simple()` statements separated by `;` up to the `newline`) with `block()` (`:` then an indented block, or statements on the same line).
  Expressions follow Python precedence: `expr()` (`a if c else b`) → `or_expr` → `and_expr` → `not_expr` → `comparison()` (chains become `and`) → `arith(level)` (`_ARITH`: `+ -`, then `* / // %`) → `unary()` → `power()` (`**`, right-associative, binds tighter than unary minus) → `postfix()` (`.x` and `[0]`/`[-1]` give `member`) → `primary()`.
  - `(a, b, c)` and `[a, b, c]` become `call("set")`.
  - `float x = …` and `true`/`false` raise errors pointing at the Python form; `def`, `return`, `break`… are `UNSUPPORTED`.
  - `_check_lvalue` allows only `var`, `attr`, or a member of one of those.
- `parse(src)` returns a list of statements.

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
- `compile_source(src, domain, pending_line=None, known_params=())`: a ch*() call on `pending_line` (1-based) whose name is not in `known_params` (the node's current inputs) compiles as its default constant instead of adding a param. This stops half-typed names from creating and removing sockets.
- Tables:
  - `BUILTINS` (`@P @N @ptnum @elemnum @primnum @index @id @Time @Frame`)
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
  - Functions: `fn1` / `fn2` (a vector op without a native node falls back to per-component math), `channel` (ch* inputs, honours `pending_line`), `map_range` (fit/smoothstep family, folds constants, vectors per component), `bbox` (`getbbox_*` are outputs 0–3 of one shared `group` node `bbox`; `relbbox` is group `relbbox`), `call` (dispatches every built-in function).
  - State: `attr_type` (an attribute's type is fixed on first use), `read_original`, `read_attr` (records `reads`), `lookup` (Python-style flat `vars`, then `CONSTANTS`; `half_set` names an `if` that set a variable in one branch only), `assign`.
  - `eval(expr)`, `exec(stmt)`, `exec_if`, `exec_for`, `exec_while`.
    - Variables need no declaration: assignment creates them and they stay until the end of the script; the type is the value's (an annotation converts).
    - `exec_if` with a non-constant condition runs **both** branches from the same starting state, then merges every variable and attribute with `select` (a per-element Switch). A variable set in only one branch is dropped and recorded in `half_set`.
    - `exec_for` unrolls `range()` (constant ints) and `exec_while` unrolls a constant condition, each at most `MAX_LOOP_ITERATIONS = 1024` times.
  - `program()` builds the `Program` and drops no-op writes such as `@P = @P`.
- **Semantics:** reads see earlier writes in the same script (sequential, like VEX). Every written value is computed from the geometry that **entered** the node (the builder captures them first).
- **IR node kinds** (must match `builder.create`): `param input attr math imath vmath compare bmath switch combine separate clamp mix maprange noise white_noise group`.
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
  - Undo: `undo()` / `redo()` return True if they changed something. Snapshots of text + cursor + anchor, capped at 200. Consecutive typed word characters coalesce into one step.
- `complete.py` — `suggest(lines, row, col, attributes=())` → the text completing the word before the cursor with the best match, or "". After `@` (optionally `f@` …) it offers attributes: the script's own (most used first), then `attributes` (meant for names read from the incoming geometry), then built-ins and `KNOWN_TYPES`. Otherwise identifiers: the script's own (most used first, comments and strings skipped via the highlighter), then language names (functions, ch*, keywords, types, constants; shortest first). Returns "" mid-word or when the word is already a complete name.
- `keys.py` — `handle_key(buf, key, text, ctrl, shift, alt, cmd, mac, page, clipboard)` applies one key (Blender event type names) and returns `EXIT`, True (handled) or False (unknown; the caller passes modifier shortcuts and F-keys on to Blender). `mac=True`: Cmd = command key, Option = word moves; elsewhere Ctrl does both. Esc and Ctrl/Cmd+Enter return `EXIT`; Ctrl or Cmd with `+` `-` `0` return `ZOOM_IN` / `ZOOM_OUT` / `ZOOM_RESET`. `Clipboard` is a stand-in; `thumb()` / `thumb_first()` are the scrollbar math (thumb offset and size, and back); `View` keeps the scroll (`top`, `left`), `follow()` keeps the cursor visible, `hit()` maps a local point to `(row, col)`.
- `highlight.py`
  - `highlight_line(line)` → spans `(start, end, kind)`, contiguous from 0 to `len(line)`, adjacent same-kind spans merged. Never raises. Lines are independent (`#` comments only).
  - `highlight(lines)`: one span list per line. `KEYWORDS`, `TYPES`, `CONSTANTS` are also used by `complete.py`.
  - `KINDS`: `comment string number attr type keyword function channel constant operator punct ident text`. `function` only for a `FUNCTION_NAMES` entry followed by `(`; `float( int( vector( bool(` stay `type`.

## `bex/builder.py`

- `sync_interface(tree, program, keep_stale=False)` makes the group's sockets match `program.params`. Sockets with an unchanged name and type are kept, so links and values survive a recompile. With `keep_stale` (a line is pending) unused inputs are not removed. Every input gets `hide_value`: the node draws the values above the code instead. Returns the newly created inputs.
- `build(tree, program, domain, keep_stale=False)` clears the tree and rebuilds it:
  - `create(ir)` maps each IR kind to a Blender node (`ShaderNodeMath`, `FunctionNodeCompare`, `GeometryNodeSwitch`, `ShaderNodeTexNoise`, …).
  - `realize()` creates nodes lazily and memoizes them.
  - `connect()` sets a socket's default for a constant, or links it for a node value. `connect_field()` is for sockets that fall back to an implicit field when unlinked, such as Position.
  - `group` adds a Group node for `library.ensure(name)` with its options hidden. Its Geometry input is linked to the Group Input's Geometry, so it measures the geometry that entered the wrangle. Inputs with an implicit field (`default_input`) use `connect_field()`.
  - Geometry chain: with more than one non-constant write, one **Capture Attribute** node captures all of them on the input geometry. Then each write gets a **Set Position** (`@P`) or **Store Named Attribute** node on `domain`.
  - Returns `(node_count, created_inputs)`.
- `_split_group_inputs(...)` runs before layout. Each source line that reads node inputs gets its own **Group Input** node in its frame, wired only to that line's nodes. Unused sockets are hidden on every Group Input, including the main one, which keeps only Geometry.
- `_layout(...)`:
  - Columns are placed by depth from the Group Output, so links flow left to right.
  - Rows are bands: the geometry chain on top, then one `NodeFrame` per source line, labelled with that line's code.
  - `estimate_height()` uses the `BUTTON_ROWS` table and skips hidden sockets, because real node sizes only exist after a redraw.
  - Add new node types to `BUTTON_ROWS`.

## `bex/upstream.py`

Blender's Python API can't read the geometry flowing into a socket, so the attributes on a wrangle's incoming geometry are a **static guess** from the node graph.

- `incoming_attributes(node)` → `[(name, bex_type)]`, sorted. Walks upstream from the Geometry input (`_from_input` / `_from_output`): union over links and all geometry inputs (Join Geometry, any pass-through node, muted nodes, reroutes). Group Input at the top level gives the attributes of the objects whose modifier runs the tree (original data, `_modifier_objects_attributes`); inside a group it continues from the group node's matching input. Group nodes are followed inside to their Group Output. Object Info gives that object's attributes. Store Named Attribute adds its (unlinked) name and type, Remove Named Attribute removes exact or wildcard matches, upstream wrangles add their compiled `writes`. Names starting with `.` and `position` are skipped.
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
- `_draw()`: `SpaceNodeEditor` POST_PIXEL handler. Paints each visible wrangle's code in Blender's monospace font (`FONT_ID = 1`), clipped with a scissor clamped to the region (`_clip()`: Metal drops the whole draw for a scissor outside the region), using the calm fixed `PALETTE` (highlight kind → colour via `_COLOR_OF`: neutral text, dim punctuation, soft colours for keywords, attributes, numbers, strings). The outline (`PALETTE`): `focus` blue while edited, `error` red, `evaluated` green when `node.is_up_to_date()`. The edited one also shows selection (rows whose line break is selected fill to the box's right edge), cursor and the inline completion as ghost text (the rest of the line shifts right, `_split_spans`). Up to `MAX_ROWS` lines; longer scripts scroll. `node.code_scale` scales the font and line height.
- `scrollbars(node, rect, lines, view)`: the vertical (right edge) and horizontal (bottom edge) bars, each only when the code overflows that way (`BAR_W`, `BAR_MARGIN`, `BAR_BOTTOM` under the horizontal bar, `BAR_MIN`; `BAR_HIT` widens the grab area, inside the box). Drawn on every wrangle; while editing, a press on a bar drags its thumb or jumps to the clicked spot (`_bar_press` / `_bar_drag`, `Session.bar`), and Shift+wheel scrolls sideways.
- `_draw_info()` / `error_lines(node)` / `info_lines(node)`: under the code, the node's error in red (wrapped to the box width, at most `ERROR_MAX_LINES`), then three small lines: `input` (named attributes probably on the incoming geometry, `upstream.incoming_attributes`), and from `node.info` `reads` (attributes the script reads from the incoming geometry) and `writes` + node count. Attributes are shown with their type prefix (`f@mask`); built-ins as `@P`.
- `Session`: the one wrangle being edited (tree/node names, `TextBuffer`, `View`, `suggestion`, `clicks` / `last_press`, `bar`). `push()` writes the buffer to the Text, `pull()` takes in outside changes. `is_editing(node)`.
- `BEX_OT_code_edit` (`bex.code_edit`): bound to LEFTMOUSE in the Node Editor keymap. `invoke` passes through unless the click is on a code box (`wrangle_at()`). The modal:
  - swallows keys (so X, Tab… don't hit node shortcuts) and passes through navigation (wheel, MMB, trackpad), unknown Ctrl/Cmd shortcuts and F-keys;
  - click places the cursor, drag selects (sideways the view follows the mouse past the box edge, even on a short row), Shift-click extends. Clicks are counted by the operator itself (`Session.clicks`: within the user's double-click time and `CLICK_SLOP` px; Blender's `DOUBLE_CLICK` is treated as a plain press). Double-click uses `buf.select_at`, or selects the whole line when past the line's end; triple-click selects the line. `DEBUG_CLICKS` (off by default) prints each click to the console and status bar. A click on another wrangle's code switches to it;
  - Ctrl/Cmd `+` `-` `0` change `node.code_scale` by `ZOOM_STEP` (0.5–3) or reset it;
  - a click outside, right-click, Esc, Ctrl+Enter, or a key pressed over another area ends it (`_end`: final sync + undo push).
- Compile: every edit is written to the Text (`push`), but nothing is evaluated until focus is lost (`_end` → `node.sync()`).
- Autocomplete: after a typed word character or Backspace, `suggest()` fills `Session.suggestion` (incoming attribute names from `upstream` are offered after `@`); any other key or a click clears it. Tab with a suggestion (and no selection) inserts it instead of indenting.

## `bex/node.py`

- Constants:
  - `COMPILER_VERSION`: bump it when the generated output changes, so existing nodes recompile.
  - `WATCH_INTERVAL = 1.0`, `TYPING_IDLE = 2.0` (a Text Editor line counts as finished after this pause), `SAVED_LIMIT`.
  - `TREE_PREFIX = ".BEX "`: hidden inner groups. Wrangles inside these groups are skipped.
  - `TEMPLATE`: the default script.
  - `DOMAINS`: Point, Edge, Face, Corner, Curve, Instance.
- `BexWrangleNode(GeometryNodeCustomGroup)`, idname `GeometryNodeBexWrangle`:
  - Properties: `script` (Text), `domain`, `show_code`, `error`, `compiled_key`, `saved_values`, `info` (JSON reads / writes / node count of the last good compile), `code_scale`.
  - `init` creates the node's own group and its own script Text from `TEMPLATE` (`_new_text()`). `copy` copies both the group and the script, so duplicates are independent. To share a script, pick the same Text in the node's script field.
  - `draw_buttons` shows the script picker, New / Edit, domain + show-code toggle + go-inside button, errors (only while the code is hidden; otherwise they're drawn under the code), then `draw_channels()` (each ch* input's value, greyed out with a link icon when the socket is linked), then the reserved code box (`code_editor.reserve`) while `show_code` is on.
  - `channels()`: the enabled non-geometry inputs.
  - `is_up_to_date()`: the compiled key matches the current script and domain (drives the green outline).
  - `sync(force, pending_line=None)`: the key is `version|domain|md5(src)` (+ `|pending` while ch* names are held back). It recompiles on change, with `keep_stale` while pending. On `BexError` it keeps the last good nodes and sets `error`. On any other exception it logs the traceback. Every rebuild calls `_redraw()`.
  - `saved_values`: JSON `{name: [socket type, value]}` written before each build (`_save_values`). A newly created input takes its remembered value when the type matches, else the script default.
- `_redraw()` tags the Node Editor, 3D Viewport, Properties and Spreadsheet areas for redraw, because a recompile from a timer sends no UI notifier.
- Updates:
  - `_watch()`: persistent 1 s timer. For scripts linked to a file it reloads changes (`_reload_from_disk`). For scripts open in a Text Editor (`_texts_in_editors`) it syncs, holding back the cursor line's ch* names while the text changed less than `TYPING_IDLE` ago (`_text_changes`). Skips the wrangle being edited on the node.
  - `_on_load` (`load_post`) force-syncs everything; `_on_undo` (`undo_post` / `redo_post`) syncs.
  - `sync_all(force)` reloads disk-linked Texts, then syncs every wrangle (`force=True` rebuilds even unchanged ones). It also runs once after `register()`.
- Operators:
  - `bex.new_script`: a new Text from `TEMPLATE`.
  - `bex.edit_script`: opens the Text Editor, or a new window if none is open.
  - `bex.update_node`: re-reads the script's file, then forces a rebuild of this node (no button any more; available from F3 search).
  - `bex.edit_nodes`: goes inside the wrangle with `space.path.append(node_tree, node=node)`, because Blender's Tab only enters built-in group nodes. It's bound to **Tab** in the addon keymap (registered in `register()`, removed in `unregister()`); on other nodes its poll fails and Tab falls through to Blender's own group edit. Also a button on the node. Edits made inside are replaced on the next recompile.
  - `bex.recompile`: forces a rebuild of every wrangle.
- `register()` also registers `library` (its operator and menu), `code_editor` (its operator, draw handler and the LEFTMOUSE keymap item) and the app handlers.
- `_add_menu` adds **BEX Wrangle** to the Geometry Nodes Add menu.

## Tests

```sh
python3 tests/test_lang.py
python3 tests/test_editor.py
"/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python tests/test_blender.py
```

- `test_lang.py`: Python syntax (indentation, elif, one-line blocks, chained comparisons, `**` `//`, error hints), Python-style variables, folding, attribute types, sequencing, if→switch, scopes, for-unroll, channels, the pending line, CSE, functions, error messages and lines.
- `test_editor.py`: buffer editing, selection, movement, undo coalescing, VS Code-style `select_at`, `suggest`; highlighter kinds, coverage, block comments, fuzzing; key mapping (platform modifiers, clipboard, exit, zoom) and `View`.
- `test_blender.py`: real evaluated geometry. Covers bounding box / mapping functions, the library groups (shared, nested, found after a rename, rebuilt on a version bump, usable in a user tree), position and channels, attributes and sequencing, swap semantics, the face domain, keeping the last good nodes on error, sync_all and copy, the pending line (inputs held back, not removed, values remembered), incoming attributes (object data, Store/Remove Named Attribute, upstream wrangles, group nodes), typing through the editor's key path (Tab-accepting completions, compiling only on focus loss), and the layout (no overlaps, links flow left to right, per-row Group Inputs).
- The draw placement can only be checked with a UI: run Blender without `-b` and with `--enable-event-simulate`, then screenshot (`bpy.ops.screen.screenshot`).

## Adding a language function (checklist)

1. `compiler.py`: add it to `MATH1` / `MATH2` or a branch in `Compiler.call` (and to `FUNCTION_NAMES` if it's a `call` branch). If it is one Blender node, map it to that node (a new IR kind needs steps 2 and 3). If it needs several nodes, add a `GroupDef` to `library.GROUPS` and emit `self.node("group", type, inputs, out=i, name=key)`.
2. `builder.py`: add the IR kind to `create()`, and the node's idname to `BUTTON_ROWS`.
3. Bump `COMPILER_VERSION` in `node.py` if output for existing scripts changes.
4. Add tests in `tests/test_lang.py` (and `test_blender.py` if node wiring is involved).
5. Update the function table in `README.md` and this file.
