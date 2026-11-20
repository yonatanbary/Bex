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
| `bex/editor/signature.py` | Signature help: `signature_at()`, `Signature` |
| `bex/editor/keys.py` | Key events → buffer actions (`handle_key()`), scroll/hit-test (`View`) |
| `bex/code_editor.py` | On-node editor, Blender side: draw handler, `BEX_OT_code_edit` modal, compile on focus loss |
| `bex/upstream.py` | Static guess of the named attributes reaching a wrangle's input (`incoming_attributes()`) |
| `bex/library.py` | Library node groups (`GROUPS`, `ensure()`), and the **Add → BEX** menu |
| `bex/ramp.py` | chramp() ramp maths in plain Python: evaluation, Bezier editing, presets (see **Ramps**) |
| `bex/ramp_widget.py` | The ramp on the node: data, layout, drawn graph, mouse and key editing (see **Ramps**) |
| `bex/ramp_icons.py` | Icons Blender lacks, drawn in its style as preview icons (`icon(name, dim)`), and the preset thumbnails (`thumbnail()`). Plain Python except `icon()` |
| `bex/builder.py` | IR → nodes (`sync_interface()`, `build()`), plus per-row Group Inputs (`_split_group_inputs()`) and auto-layout (`_layout()`) |
| `bex/node.py` | The node class, channel properties, operators, Add-menu entry, update events |
| `bex/presets.py` | The **Presets** menu: built-in and user `.bex` scripts, Save as Preset |
| `bex/builtin_presets/` | Built-in preset scripts (Displace, Height Gradient, Noise Displace, Ripple) |
| `BLENDER_VERSIONS.md` | Everything tied to a Blender version's behaviour (engine limits, measured sizes, API workarounds): re-check on a Blender upgrade |
| `examples/ripple.bex` | Example script (linked-to-a-file workflow) |
| `tests/test_lang.py` | Compiler tests, plain `python3` |
| `tests/test_editor.py` | Editor core tests (buffer, highlighter, completion, signatures, keys), plain `python3` |
| `tests/test_blender.py` | End-to-end tests, run in background Blender |
| `tests/test_ramp.py` | Ramp maths tests, plain `python3`; compares against `houdini_ramps.json` |
| `tests/houdini_ramps.py` | Samples Houdini's own ramps into `houdini_ramps.json` (run with `hython`) |
| `.vscode/settings.json` | Blender Development extension config (reload on save, `*.bex` as Python) |

## `bex/lang/lexer.py`

- `BexError(msg, line)`: the only user-facing error type. `str()` gives `line N: msg`.
- `Token(kind, val, line)`: kinds `num | ident | attr | str | op | newline | indent | dedent | eof`.
  - `attr` value is `(name, prefix_type)`; prefix `f/i/v/b` → `float/int/vector/bool`, else `None`.
  - `num` is `int` unless the text has `.`, `e` or `E`.
  - `.1` written right after a name (not a keyword), attribute, `)` or `]` is component access: an `op` `.` then an int (`_is_component`), so `@P.1` is `@P[1]` while `x = .5` stays a number.
- `tokenize(src)`: Python-style. One regex (`_TOKEN_RE`) for tokens; `#` comments; `newline` ends each logical line; `indent` / `dedent` from the leading whitespace (tab = 4); blank and comment-only lines are skipped; line breaks inside `(` `[` (and after a `\`) are ignored; an unclosed bracket is an error at its line. C/VEX tokens (`&& || ! ? { } ++`) raise a `_HINTS` message naming the Python form.
- TODO in `lexer.py`: `s@` string attributes. Geometry nodes can't hold text per element in Blender 5.2 or 5.3 (see `BLENDER_VERSIONS.md`).

## `bex/lang/parser.py`

- `N(kind, line, **fields)`: generic AST node.
  - Statement kinds: `block`, `if` (`elif` is an `if` inside the `orelse` block), `for` (`var`, `args` of `range()`, `body`), `while`, `break`, `continue`, `def` (`name`, `params` = `[(name, default or None)]`, `body`), `return` (`value` or None), `assign` (`op`, optional `annot` type from `x: int = …`), `expr`. `pass` is an empty `block`.
  - Expression kinds: `num`, `str`, `bool`, `attr`, `var`, `call`, `member`, `index` (`v[i]` with a non-literal index: `obj`, `index`), `kwarg` (`name=value` in a call's `args`), `unop` (`-`, `not`), `binop`, `ternary`.
- `Parser`: `statement()` (if / for / while, or `simple()` statements separated by `;` up to the `newline`) with `block()` (`:` then an indented block, or statements on the same line).
  Expressions follow Python precedence: `expr()` (`a if c else b`) → `or_expr` → `and_expr` → `not_expr` → `comparison()` (chains become `and`) → `arith(level)` (`_ARITH`: `+ -`, then `* / // %`) → `unary()` → `power()` (`**`, right-associative, binds tighter than unary minus) → `postfix()` (`.x`, `.0`–`.2` and `[0]`/`[-1]` give `member`, any other `[expr]` gives `index`) → `primary()`.
  - `@group_name` (Houdini's spelling, `GROUP_PREFIX`) is parsed as the bool attribute `name`, in scripts and in the Group field.
  - `(a, b, c)` and `[a, b, c]` become `call("set")`.
  - `float x = …` and `true`/`false` raise errors pointing at the Python form; `import`, `class`, `lambda`… are `UNSUPPORTED`. `def_stmt()` reads `def` (defaults only after the positional parameters); `call_args()` reads keyword arguments after the positional ones.
  - `_check_lvalue` allows only `var`, `attr`, or a `member` / `index` of one of those.
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
  - `ramps`: chramp() names in first-use order; `shared_ramps`: those read at more than one position.
  - `stores`: `[Store]`, the write nodes in chain order, and `captures`: `[(label, type, Val)]` to capture on the incoming geometry. Both are filled by `plan_stores()`; the builder follows them.
  - `preludes`: `chunk_end` IR nodes, the chunked loops (see `_zone`), built on the geometry before the captures and stores.
- `Store(name, type, value, selection, conds, offset)`: one write node. `conds` are the bool `Val`s ANDed into `selection` (the Group first). `offset`: Set Position's `value` goes into Offset.
- `compile_source(src, domain, pending_line=None, known_params=(), group="")`: a ch*() call on `pending_line` (1-based) whose name is not in `known_params` (the node's current inputs) compiles as its default constant instead of adding a param. This stops half-typed names from creating and removing sockets.
  - `group` is compiled **first** (so it reads the incoming geometry), with `bare_attributes` on (a bare name is an attribute, e.g. `ptnum < 5`) and no pending line. `_mark_groups()` makes a name used as a whole condition (`top`, `@top`, operands of `and`/`or`/`not`) a bool attribute; inside a comparison it keeps its usual type (`@mask > 0.5` is a float). Attribute types the Group inferred are reset before the script runs. A Group that fails to compile selects nothing (`Val("bool", False)`) and adds a warning; it never stops the build. An always-true group becomes `None`.
- Tables:
  - `BUILTINS` (`@P @N @ptnum @elemnum @primnum @curvenum @vtxnum @edgenum @index @id @Time @Frame @curveu @curvelength @tangent @area @island @edgeangle`; name → type, input kind, output), `COUNT_ATTRS` (`@numpt @numprim @numvtx`: counts of input 0)
  - `DOMAIN_INDEX`: the index built-ins that depend on Run Over, like Formula to Nodes: `@ptnum` is the index on Points/Instances and the corner's vertex on Corners, `@primnum` the index on Faces/Curves and the corner's face on Corners, `@curvenum` the curve index or a point's curve, `@vtxnum` only on Corners, `@edgenum` only on Edges. On any other domain they raise the `INDEX_HINTS` message. `@elemnum` / `@index` are always the element's own index. `@curveu` / `@curvelength` are Spline Parameter's Factor / Length.
  - Other inputs: `MAX_INPUT = 3`, `SAMPLE_FUNCS` (`point prim vertex` → domain), `COUNT_FUNCS` (`npoints nprims nprimitives nvertices nedges ncurves`), `TOPOLOGY` (the two-argument topology functions → node, output, index socket, domain), `NODE_OUTS` (the topology nodes' outputs in order), `INPUT_SOCKETS` ("Input 2"…"Input 4", counted from 1 like a wrangle's inputs, so input 1 is "Input 2": the node's always-present extra geometry inputs, not allowed as ch*() names), `OPINPUT_RE` (`@opinputN_name`)
  - `READ_ONLY`
  - `KNOWN_TYPES` (`v Cd up` are vectors, `radius pscale` are floats; other attributes default to float)
  - `MATH1` / `MATH2` (name → Math op + Python fn)
  - `VMATH1` / `VMATH2` / `IMATH2` (ops the vector or int nodes support natively)
  - `COLOR_MODES` (`hsvtorgb rgbtohsv hsltorgb rgbtohsl` → Combine / Separate Color mode), `LUMINANCE` (Rec. 709)
  - `ARITH` (Python semantics: `/` always float, `//` floor division, `%` floored modulo, `**`), `CONSTANTS` (`PI pi TAU tau M_PI`), `COMPARE`, `CHANNELS` (`chf chi chv chb`), `BBOX` (`getbbox_min/max/size/center`, `relbbox`)
  - `FUNCTION_NAMES`: every name `call` accepts (used by the editor's highlighter). Keep in sync with `call`.
  - `SIGNATURES`: name → (parameters, one-line summary) for the editor's signature help. BEX forms, not VEX: no types or overloads, an optional parameter is `"[name]"`. Summaries are short and plain. Every `FUNCTION_NAMES` entry needs one (`test_editor.py` checks).
- `Compiler` methods:
  - `node()` builds an IR node with **CSE** (dedup key: kind + params + inputs).
  - `conv()` does implicit type conversion. A vector never converts down to a scalar.
  - `fold()` does constant folding.
  - Operators: `arith`, `compare`, `logic` (short-circuits constants), `negate`, `select` (becomes a Switch), `member`, `combine`.
  - Functions: `fn1` / `fn2` (a vector op without a native node falls back to per-component math), `channel` (ch* inputs, honours `pending_line`), `map_range` (fit/smoothstep family, folds constants, vectors per component), `bbox` (`getbbox_*` are outputs 0–3 of one shared `group` node `bbox`; `relbbox` is group `relbbox`; an optional first argument picks the input, param `input`),
  - Other inputs: `input_number()` (a constant 0–3; using 1–3 records it in `inputs`), `read_input()` (a read tagged `src=n`, so it is its own IR node: never shared with, or captured as, the same read on this geometry, and skipped by `_reads`), `sample()` / `sample_call()` (`point(1, "v@vel", i)`, type from `_typed_name`), `count()`, `read_opinput()` (`@opinputN_name` = `sample` at this element's index on the run-over domain), `nearpoint` in `call`. `call` (dispatches every built-in function).
  - `gnode()` builds an IR `node`: any one Blender node described by its params (see the IR kinds). `topology()`: a topology node on input 0 is a field taking the index directly (`field`); on another input it is evaluated there (`src`) and sampled at the index, so its other inputs must be constants. `proximity()` (Geometry Proximity to faces), `query()` (`xyzdist` / `intersect`: the face comes from Face of Corner read through Sample Nearest Surface / Raycast, which look values up on corners; the ray is as long as the direction), `out_arg()` (VEX output arguments: a last argument naming a variable or attribute is assigned), `color()` (folds with `colorsys`; the hue wraps with a Fraction node, since Combine Color clamps it).
  - State: `attr_type` (an attribute's type is fixed on first use), `read_original`, `read_attr` (records `reads`), `lookup` (Python-style flat `vars`, then `CONSTANTS`, then, in the Group field only, an attribute; `half_set` names an `if` that set a variable in one branch only, `loop_only` a Repeat Zone loop that first set it inside), `assign`.
  - `eval(expr)`, `exec(stmt)`, `exec_if`, `exec_for`, `exec_while`.
    - Variables need no declaration: assignment creates them and they stay until the end of the script; the type is the value's (an annotation converts).
    - `exec_if` with a non-constant condition runs **both** branches from the same starting state, then merges every variable and attribute with `select` (a per-element Switch). A variable set in only one branch is dropped and recorded in `half_set`.
    - **Loops** (`exec_for`, `exec_while`): `range()` takes whole numbers, the step must be a constant.
      - `_unroll` runs the loop now and keeps the result when everything the body assigns (`_assigned`) stays constant, or it ran at most `UNROLL_MAX = 8` times (so `v[i]`, folding and Set Position Offset work in short loops); otherwise it rolls back (`_snapshot` / `_restore` copy the `_STATE` attributes). At most `MAX_LOOP_ITERATIONS = 1024` iterations; an all-constant loop past that is an error. A while loop whose condition was constant each time but whose body wasn't returns its exact count.
      - Everything else is `_zone`: one Repeat Zone, never unrolled copies (measured in Blender 5.2: see BLENDER_VERSIONS.md). Its items are the variables (that exist before the loop) and attributes the body assigns, carried as fields, so attributes are written once after the zone. Item types widen and items become fields (`params["fields"]`) as the body needs, by rerunning the body. Counts are clamped to `MAX_ZONE_ITERATIONS = 100_000`.
      - `break` / `continue` set hidden bool variables, one pair per loop (`_new_flags`: `$breakN`, `$continueN`; `loops` is the stack). While one is set, `assign()` keeps old values (`_live`), so `if` merging, unrolling and zone items handle them like any variable. `$continueN` resets each iteration; the loop variable is carried when the loop can stop early, so it keeps its last value.
      - A count that differs per element (`range(@ptnum)`, `_is_field`) runs the longest count (IR `stat`: Attribute Statistic Max over the run-over domain, Selection = the Group, `group_sel`), and an element stops (`$breakN`) once Iteration reaches its own count. It may not read an outer zone's items (`_uses_items`): that would re-measure every outer iteration.
      - A while loop with a per-element condition sets `$breakN` when the condition fails.
      - A loop that can stop early starts stopped (`_stopped_here`) where it doesn't run: outside the `if` branches around it (`path`, kept by `exec_if`) or after a break / continue / return, so its chunks skip those elements. A `return` raises every break flag of its function, so `_assigned` (`outer`) lists them all as items. A `while` inside another zone adds a warning about its cap.
      - **Chunks** (`_zone(chunked=True)`): a loop that can end early (a `while` without a known count, or a body with `break`) that sits in no other zone is an outer zone of growing chunks (IR `chunk`: chunk k runs floor(`CHUNK_FIRST` × `CHUNK_GROWTH`^k) = 32 × 1.5^k times, out 0 = that size, out 1 = the iterations before it), each an inner Repeat Zone. Between chunks the items live on the geometry as hidden attributes (`TEMP_PREFIX` `.bex<loop>_<item>`); after the loop they are read back from there. `chunk_end` (in `preludes`) stores them, and a chunk is skipped once no element in the Group is running. A while loop inside another zone uses the cap `WHILE_CAP = 1000` instead. Blender's Repeat Zone has no stop condition: this is how loops end early.
      - Variables first set inside a zone are dropped after it (`loop_only`); Python keeps the loop variable at its last value.
      - A loop body that calls a function writing an attribute: `_zone` and `_unroll` also notice attributes that changed while running the body, and carry them.
    - **The script's own functions** (`def`, `call_user`): `exec` of a `def` stores it in `funcs` (a built-in name is an error). A call binds the arguments (`_bind_args`: positional, keywords, defaults evaluated at the call in the script's variables) and runs the body inlined (`_run_body`) in its own `vars`, with `frames` holding each running call: reads fall back to the script's variables (`globals`), assignments stay local, attribute writes are allowed (predicated by the caller's `live` too). `return` sets the hidden `$ret` / `$returned` variables like `break` (`_raise_flags`, which also leaves every loop of the function), so an early return under a per-element `if` merges with Switches. A statement after a constant `return` / `break` doesn't run at all (`exec` checks `_live`), which lets recursion end while compiling (`MAX_CALL_DEPTH = 50`, `MAX_CALLS = 10_000`; past them the error suggests a while loop). `_result`: a function used as a value must return one, on every path (`returned` constant true, or `_always_returns` from the code: a trailing return, if/else both returning, a trailing `while True:` without `break` (`_breaks`)). `logic` folds `x or not x` (`_negates`) so the return flags stay constant where they can.
    - **Closures**: `compile_source` compiles twice when a function was called more than once (`call_counts`, keyed by name and argument types, counting calls with a non-constant argument and not recursive). Keys are name, argument types and which arguments differ per element (`closure_in` `params["fields"]`, so a loop count inside sees the truth). The second compile builds those (`closure_keys`) once each (`_closure`: the body with `closure_in` outputs as its arguments, recording in `reading` the script variables and attribute values it read) and every call is a `closure_call` (Evaluate Closure). `_find_closure` reuses a closure only when what it read is unchanged at the call (else builds another). `_STATE` includes `closures`, `call_counts`, `calls`, `loop_count` and `warnings`, so rolled-back runs leave no trace. A function that writes attributes or runs a chunked loop is inlined after all (`closures[key] = None`). Measured (Blender 5.2, 20-node body, 16 calls): inlined 4.1 ms to evaluate and 364 ms to build; a closure 2.9 and 12.
  - `program()` builds the `Program` and drops no-op writes such as `@P = @P`.
  - `plan_stores(prog, group)` (called last by `compile_source`) turns `writes` into `stores` and `captures`:
    - `_peel_selection`: a value `select(c, v, original)` writes `v` with Selection `c` (`NOT c` when the original is the true side; nested ifs AND), so a conditional write needs no Switch and no read of the old value.
    - `_position_offset`: `@P` = position + x (`@P += x`, `@P -= x`, per component like `@P.z += 1`) uses Set Position's Offset input. Not when `position` is also written by name.
    - Set Position goes last. A value or selection is captured only if it reads (`_reads`: named attributes, plus position/normal/id input nodes) something an earlier write node changed (`_changes`), e.g. a swap.
- **Semantics:** reads see earlier writes in the same script (sequential, like VEX). Every written value is computed from the geometry that **entered** the node (the builder captures them first).
- **IR node kinds** (must match `builder.create`): `param input attr math imath vmath compare bmath switch combine separate clamp mix maprange noise white_noise group sample nearest count ramp repeat_in repeat_out stat chunk chunk_end closure_in closure_out closure_call node`. `node`: one Blender node of any type, `params`: `idname`, `outs` / `ins` (output and input socket names; `out` indexes `outs`, `inputs` go to `ins`), optional `props`, `menus` (menu sockets), `geo` (socket, input number), `implicit` (sockets that fall back to a field), `field` (a context field even with constant inputs: `_is_field`), `src` (evaluated on that input inside a node reading it: `_is_field` and `_reads` skip it). `repeat_in` (not deduplicated): inputs = the count then the items' starting values, `params["items"]` = their types, `params["fields"]` = which items can differ per element, out 0 = Iteration, out k + 1 = item k. `repeat_out`: inputs = the items' final values, `params["zone"]` = its `repeat_in` (`_reads` and `_is_field` follow it, since zero iterations pass the starting values through). `stat`: the Max of a field over the incoming geometry (inputs: value, selection; `params["domain"]`), one value. `chunk` / `chunk_end`: a chunked loop (`params["names"]` the hidden attributes, `items`, `count` chunks; `chunk_end` inputs: the inner zone's final values, then the Group; `params["flag"]` = the break flag item). `closure_in` (outputs: the arguments) / `closure_out` (input: the result; `params`: `input`, argument `items`, `ret`, `name`): an own function's body, once; `closure_call`: inputs = the arguments, `params["closure"]` = its `closure_out` (`_reads`, `_is_field` and `_walk_outputs` follow it into the body). `ramp` (from `chramp(name, pos)`, `RAMPS`) is the Float Curve evaluating that node-side ramp, `params["name"]`. `sample`, `nearest`, `count` and `group` carry `params["input"]`, the geometry input they read.
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
- `signature.py` — `signature_at(lines, row, col)` → a `Signature` for the innermost call around the cursor that has a `SIGNATURES` entry, or None. A forward scan from the top keeps a stack of open brackets with their comma counts: strings and `#` comments are skipped, calls may span lines, plain `(`/`[` are looked through (`fit((a|` is still fit), and an unknown function hides the help. `Signature`: `name`, `params`, `summary`, `active` (argument index), `row`/`col` of the name; `parts()` → `(text, role)` pieces, role `name | punct | param | active`. A skipped leading optional argument (`relbbox(@P)`, `range(stop)`) puts `active` one too early.
- `keys.py` — `handle_key(buf, key, text, ctrl, shift, alt, cmd, mac, page, clipboard)` applies one key (Blender event type names) and returns `EXIT`, True (handled) or False (unknown; the caller passes modifier shortcuts and F-keys on to Blender). `mac=True`: Cmd = command key, Option = word moves; elsewhere Ctrl does both. Esc and Ctrl/Cmd+Enter return `EXIT`; Ctrl or Cmd with `+` `-` `0` return `ZOOM_IN` / `ZOOM_OUT` / `ZOOM_RESET`. `Clipboard` is a stand-in; `thumb()` / `thumb_first()` are the scrollbar math (thumb offset and size, and back); `View` keeps the scroll (`top`, `left`), `follow()` keeps the cursor visible, `hit()` maps a local point to `(row, col)`.
- `highlight.py`
  - `highlight_line(line)` → spans `(start, end, kind)`, contiguous from 0 to `len(line)`, adjacent same-kind spans merged. Never raises. Lines are independent (`#` comments only).
  - `highlight(lines)`: one span list per line. `KEYWORDS`, `TYPES`, `CONSTANTS` are also used by `complete.py`.
  - `KINDS`: `comment string number attr type keyword function channel constant operator punct ident text`. `function` only for a `FUNCTION_NAMES` entry followed by `(`; `float( int( vector( bool(` stay `type`.

## `bex/builder.py`

- `sync_interface(tree, program, keep_stale=False)` makes the group's sockets match the program: Geometry in/out, the three `INPUT_SOCKETS` geometry inputs (always, like a wrangle's four inputs), then `program.params`. Sockets are found by name. Sockets with an unchanged name and type are kept, so links and values survive a recompile. With `keep_stale` (a line is pending) unused inputs are not removed. Every input gets `hide_value`: the node draws the values above the code instead. Returns the newly created inputs.
- `build(tree, program, domain, keep_stale=False)` clears the tree and rebuilds it:
  - `create(ir)` maps each IR kind to a Blender node (`node` generically from its params) (`ShaderNodeMath`, `FunctionNodeCompare`, `GeometryNodeSwitch`, `ShaderNodeTexNoise`, …). `INPUT_NODES` maps an `input` node's `name` to its node: `index`, `id`, `time`, `spline_parameter`, `tangent`, `face_area`, `mesh_island`, `edge_angle`, and the topology nodes `curve_of_point`, `face_of_corner`, `vertex_of_corner`.
  - `realize()` creates nodes lazily and memoizes them.
  - `connect()` sets a socket's default for a constant, or links it for a node value. `connect_field()` is for sockets that fall back to an implicit field when unlinked, such as Position or Index: a constant goes through a Vector / Integer / Boolean / Value node.
  - `geometry(n)`: the Group Input socket of input n ("Input N"); input 0 is the Geometry after the chunked loops built so far (`base`), so measurements (`stat`, library groups, samples) see their hidden attributes.
  - `repeat_in` creates both Repeat Zone nodes (paired, the default Geometry item removed, one item per `params["items"]`); `repeat_out` fills the output's inputs (`zones` maps one to the other). `_layout` treats a zone's input as feeding its output, so the input lands to its left.
  - `stat` → **Attribute Statistic** (Max) on the incoming geometry.
  - `closure_out` → a Closure zone labelled `name()` (an input item per argument, one Result); `closure_call` → **Evaluate Closure**. Nodes outside the zone that the body uses are linked in (closures capture them). `_layout` treats a closure's input as feeding its output, like a Repeat Zone's.
  - `chunked_loop(end, geo)` builds a `chunk_end` from `program.preludes`: stores the starting values, then an outer Repeat Zone (items Geometry, Offset) whose Iteration gives the chunk size; inside, the inner zone, then every final value is stored under a shadow name (`name~`) and copied back, both only where the element is still running (the flag copied last), and a Switch on "Attribute Statistic Max of running > 0" skips the chunk. `TEMP_PREFIX` attributes are removed (Remove Named Attribute, Wildcard) before the Group Output.
  - `sample` → **Sample Index**, `nearest` → **Sample Nearest**, `count` → **Attribute Statistic** (sum of 1, so it counts mesh, point cloud and curve points together), each on `geometry(input)`.
  - `group` adds a Group node for `library.ensure(name)` with its options hidden. Its Geometry input is linked to `geometry(input)`, so it measures the geometry that entered the wrangle (or another input). Inputs with an implicit field (`default_input`) use `connect_field()`.
  - Geometry chain: first the chunked loops (`program.preludes`), then, if `program.captures` is not empty, one **Capture Attribute** node captures those values on the input geometry; from then on `connect()` links any captured value (keyed by IR node and output) to the Capture output. Then each `program.stores` entry gets a **Set Position** (`@P`; Position, or Offset when `offset`) or **Store Named Attribute** node on `domain`, with `selection` in its **Selection** input (the Group ANDed with any `if` conditions).
  - Ramps: each `chramp()` name is one Float Curve named `RAMP_PREFIX + name` (`ramp_node()`), filled by `set_curve()` from `ramp.polyline()` (straight pieces, vector handles). A ramp read at several positions puts its curve in a Closure zone, evaluated once per position.
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

## Ramps (`chramp()`)

`chramp(name, pos)` reads a Houdini-style ramp whose keys live on the wrangle node, edited
on a graph drawn on the node. The node graph can't run Houdini's interpolations, so the
ramp is sampled into straight pieces for a Float Curve (`builder.set_curve`).

### `bex/ramp.py` (no bpy)

- A ramp is a list of keys `(position, value, interpolation)`; a key's interpolation shapes
  the stretch to the next key. `INTERPOLATIONS`: `CONSTANT LINEAR CATMULL_ROM MONOTONE_CUBIC BSPLINE BEZIER`.
  Positions are 0–1; values may go past 0–1.
- `polyline(keys)` → `[(x, y)]` from x = 0 to 1 (held past the end keys, x rising, `simplify()`d);
  `evaluate(keys, x)` reads it. Matched to Houdini's own lookup (`test_matches_houdini`):
  Catmull-Rom (`_catmull_rom`), Monotone Cubic (`_tangents`, flat ends) and B-spline
  (`_bspline_curve`, ends tripled) per Houdini's rules; every basis but Bezier reads keys in position order.
- **Bezier** is read in **point order**, like Houdini: from the first key, each key and the next
  three are one cubic (anchor, handle, handle, anchor); missing control points repeat the last key.
  Keys 0, 3, 6… and the last are anchors (`is_anchor`, `anchor_of`, `handles_of`, `bezier_segments`).
  BEX keeps Bezier a whole-ramp mode with 3k + 1 keys: `to_bezier()` adds two handles per stretch
  along the curve's slope (shape kept; Constant and B-spline only approximated), `from_bezier()`
  keeps the anchors, `bezier_insert()` splits a cubic with de Casteljau (shape kept; past the ends
  it adds a straight stretch), `bezier_delete()` removes anchors with their handles (handles alone stay).
- `reverse()` / `invert()` (Bezier keeps its point order), `PRESETS` (Houdini's `float.ramp`
  presets, except Round and Sharp: quarter-circle Beziers, handles at `KAPPA` = 4(√2 − 1)/3,
  each the other turned half around), `DEFAULT_KEYS` (Linear).

### `bex/ramp_widget.py`

- Data: `BexRampKey` (`position`, `value`, `interpolation`, `select`) and `BexRamp` (`keys`,
  `selected` = the active point, `expanded`, `show_point`, `point` (Point No.), `preset`,
  `interpolation` (header menu, every point: `set_mode()`), and the point-field proxies
  `point_position` / `point_value` / `point_interpolation`). On the node as `bex_ramps`;
  `ensure_ramps()` after each compile keeps up to `RAMP_LIMIT` unused ones. `_owner()` finds a
  key's node by pointer (RNA has no paths into custom node properties). `write_curve()` refills
  the Float Curve on every change (`_quiet` batches many changes into one write).
- Selection: `key.select` (highlighted; Delete removes them) plus the active point.
  `select_only`, `select_range` (Shift-click: position order; Bezier: anchors only),
  `delete_selected` (Bezier: anchors with their handles), `deselect_tree`. A click outside the
  node clears the selection; a click on the node keeps it, so its buttons act on it.
- Bezier editing: an anchor carries its handles (`carried()`, `move_point()`); handles are
  aligned (`align_sibling()`: the other handle swings into line, keeping its length) and stay on
  their side of the anchor (`keep_side()`: half a turn, never crossing).
- Layout, `draw_layout()`: header row (the name as a plain label; interpolation menu
  `NODE_MT_bex_ramp_interpolation` → `BEX_OT_ramp_interpolation`, flips, preset thumbnails), the
  2x2 grid left of the graph (add / delete `BEX_OT_ramp_action`, point-fields and fold toggles),
  an empty label the graph is drawn into, and the point fields indented by `POINT_INDENT`
  (read-only with several points selected; Interpolation read-only in a Bezier ramp).
  On a muted node our preview icons use dimmed copies (Blender doesn't dim those).
- Drawing, `draw_node()` (from `code_editor._draw`, faded with the node): back, `GRID` × `GRID`
  lines when expanded, the area and the curve (`CURVE_W`), dots (Bezier handles hollow, with a
  purple line to their anchor), a triangle on the edge for a value past 0–1 (`_arrow`), and
  position markers (anchors only). `ramp_rects()` places each graph counted down from the node's
  top in Blender pixels (`_row_sizes`, `_separator_height` for the line under Run Over), following
  `node.draw_buttons`; keep the two in step.
- Editing: `BEX_OT_ramp_edit` (LEFTMOUSE, also in the node tool keymaps): drag points (the selection
  moves as one, stopping at 0–1 unless a value already sits past it), click empty space to add a
  point (Bezier: split the curve), Shift-click for a range, Ctrl+click deletes. `ramp_at()` hit-tests.
  `BEX_OT_ramp_delete` (Delete, Backspace, X) deletes the selected points and passes through when
  none are selected.

## `bex/code_editor.py`

The on-node editor, Blender side. Built on `bex/editor/` (buffer, highlighter, keys).

- Placement: `reserve(layout, node)` (called last in `draw_buttons`) adds an empty label scaled to `box_height()`. `box_rect(node, region)` finds it from the node's **bottom** edge: below it come only the input socket rows (`SOCKET_ROW`) and `BOTTOM_PAD`; `SIDE` is the horizontal inset. These constants were measured from Blender 5.2 screenshots (UI scale 0.6 to 2). Python can't read widget positions, so re-measure if Blender changes node drawing. Sizes are node-editor units × `preferences.system.ui_scale` × zoom. `node_top(node, region)` gives the node's top edge and the zoom, `node_rect()` the whole node; the ramp graphs (`ramp_widget.ramp_rects`) are counted down from it in Blender pixels with Blender's rounding (`_row_sizes`), so nothing below a graph can move it.
- The reserved block is the code box (`box_height(lines, node.code_scale)`) plus the info strip (`info_height(node)`) under it, so the strip needs no layout measuring of its own.
- `_draw()`: `SpaceNodeEditor` POST_PIXEL handler. Paints each visible wrangle's code in Blender's monospace font (`FONT_ID = 1`), clipped with a scissor clamped to the region (`_clip()`: Metal drops the whole draw for a scissor outside the region), using the calm fixed `PALETTE` (highlight kind → colour via `_COLOR_OF`: neutral text, dim punctuation, soft colours for keywords, attributes, numbers, strings). The outline (`PALETTE`): `focus` blue while edited, `error` red, `evaluated` green when `node.is_up_to_date()`. The edited one also shows selection (rows whose line break is selected fill to the box's right edge), cursor and the inline completion as ghost text (the rest of the line shifts right, `_split_spans`). The box shows at least `MIN_ROWS = 10` and up to `MAX_ROWS = 16` lines; longer scripts scroll. `box_height()` includes the horizontal scrollbar row, so the bar never covers the last line. The outline is drawn last, over the line numbers and scrollbars. `node.code_scale` scales the font and line height.
- Line numbers: a gutter on the left (`gutter_width()`: the last line number's digits, at least `MIN_DIGITS`, plus `GUTTER_PAD` characters each side), slightly lighter than the code (`PALETTE` `gutter`), numbers right-aligned and dim. While editing, the cursor line's gutter row and number are highlighted, and `Line N, Col M` is drawn right-aligned on the info strip's first line (that line is shortened with `_fit`). The gutter is painted after the code, so text scrolled sideways slides under it. `text_left()` is where column 0 starts; drawing, `scrollbars()` and mouse hits all use it.
- Scrollbars sit outside the code box, like Houdini's: `box_rect()` is the whole editor, `code_rect()` the box inside it (a `BAR_SIZE` column on the right and row at the bottom, `BAR_GAP` away). `scrollbars(node, rect, lines, view)` → `{"v"/"h": Bar(area, track, thumb, minus, plus, total, visible)}`; both are always drawn (`_draw_bars`: a thin grey track, a grey thumb, `_chevron` arrow buttons at both ends). With nothing to scroll that way `thumb` is None, the arrows are dim and presses do nothing. While editing, an arrow scrolls one line / one character (`_scroll_bar`), the thumb drags and the track jumps (`_bar_press` / `_bar_drag`, `Session.bar`); Shift+wheel scrolls sideways. Held down, an arrow repeats (`Session.repeat`, `_repeat`): after `REPEAT_DELAY` at `REPEAT_RATE` lines per second, speeding up to `REPEAT_MAX` over `REPEAT_RAMP`. A window timer (`REPEAT_TICK`) runs only while an arrow is held (`_sync_timer`).
- Scrolling: each box's `View` is kept per node in `_views` (`view_for(node)`, keyed by tree and node name; cleared by `forget_buffers()`), so the scroll survives sessions. `scroll_code(node, region, event)` handles the wheel (3 rows; Shift sideways while editing) and `TRACKPADPAN` (fractional rows in `View.pan_y` / `pan_x`) and returns False when there is nothing to scroll, so the event reaches the Node Editor. `BEX_OT_code_scroll` (`bex.code_scroll`, keymap on wheel / trackpad) scrolls a box under the mouse when nobody is editing; the edit modal calls `scroll_code` itself. Ending a session resets the sideways scroll.
- `_draw_info()` / `note_lines(node)` / `info_lines(node)`: under the code, the node's error in red, then its warning in orange (each wrapped to the box width by `_wrap`, at most `ERROR_MAX_LINES`), then three small lines: `input` (named attributes probably on the incoming geometry, `upstream.incoming_attributes`), and from `node.info` `reads` (attributes the script reads from the incoming geometry) and `writes` + node count. Attributes are shown with their type prefix (`f@mask`); built-ins as `@P`.
- Undo history is per script and lives outside the session, like Houdini: `buffer_for(text)` keeps one `TextBuffer` per Text (`_buffers`, keyed by `session_uid`), so refocusing a wrangle still undoes earlier typing. Outside changes (Text Editor, Blender undo, disk) enter it via `set_text` as their own step. `forget_buffers()` runs on `load_post` (it also clears `_views`). In Blender's own undo a whole session is one step ("Edit BEX Code", pushed by `_end` only if the text changed).
- `Session`: the one wrangle being edited (tree/node names, its script's `TextBuffer` from `buffer_for`, `start_text`, `View`, `suggestion`, `clicks` / `last_press`, `bar`). `push()` writes the buffer to the Text, `pull()` takes in outside changes. `is_editing(node)`.
- `BEX_OT_code_edit` (`bex.code_edit`): bound to LEFTMOUSE in the Node Editor keymap. `invoke` passes through unless the click is on a code box (`wrangle_at()`). The modal:
  - swallows keys (so X, Tab… don't hit node shortcuts) and passes through navigation (wheel and trackpad when not over a scrollable box, MMB), unknown Ctrl/Cmd shortcuts and F-keys;
  - click places the cursor, drag selects (sideways the view follows the mouse past the box edge, even on a short row), Shift-click extends. Clicks are counted by the operator itself (`Session.clicks`: within the user's double-click time and `CLICK_SLOP` px; Blender's `DOUBLE_CLICK` is treated as a plain press). Double-click uses `buf.select_at`, or selects the whole line when past the line's end; triple-click selects the line. `DEBUG_CLICKS` (off by default) prints each click to the console and status bar. A click on another wrangle's code switches to it;
  - Ctrl/Cmd `+` `-` `0` change `node.code_scale` by `ZOOM_STEP` (0.5–3) or reset it;
  - a click outside, right-click, Esc (a second Esc while the signature help is up), Ctrl+Enter, or a key pressed over another area ends it (`_end`: final sync, plus a Blender undo push when the text changed).
- Compile: every edit is written to the Text (`push`), but nothing is evaluated until focus is lost (`_end` → `node.sync()`).
- Signature help: while the cursor is inside a call, `Session.signature()` (`signature_at`) gives the popup, and `_draw_node` stores it in `_popup`; `_draw()` paints it after every node so no code box covers it (`_draw_signature`). It shows `name(params)` with the argument being typed bright and underlined (`PALETTE` `sig_*`), and the summary under it in a smaller font (`SIG_SUMMARY`), wrapping after `SIG_WRAP` characters. It opens `SIG_GAP` below the cursor line with its text under the function name, slides left to stay `SIG_MARGIN` inside the free part of the region (`_free_x()`: not under the sidebar or toolbar, which Blender draws over the node editor), and opens above the line when there's no room below. The first Esc hides it for that call (`Session.sig_hidden`, the name's row/col) without ending the session; it comes back once the cursor leaves that call.
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
  - Properties: `bex_ramps` (the chramp() keys, `ramp_widget.BexRamp` by name; `ensure_ramps()` after each compile, `ramps()` = the last good compile's names, drawn under the ch* fields), `script` (Text, not drawn), `domain`, `group` (the Group field), `error`, `warning`, `compiled_key`, `built_print`, `saved_values`, `info` (JSON reads / writes / node count of the last good compile), `code_scale`.
  - `init` creates the node's own group and its own script Text from `TEMPLATE` (`_new_text()`). `copy` copies both the group and the script, so duplicates are independent. `draw_label` is "BEX Wrangle" for an own script, else "BEX: name".
  - `draw_buttons`: Group, Run Over, a separator line when there are ch* fields or ramps, the error (only when there's no script; otherwise it's drawn under the code), `draw_channels()` (each ch* value as a property, greyed out with a link icon when its socket is linked) then each ramp (`ramp_widget.draw_layout`) and a separator line, then a right-aligned row with the **Presets** menu and Edit (pencil), then the reserved code box (`code_editor.reserve`). The code is always shown.
  - `channels()`: the enabled non-geometry inputs. ch* values are **properties, not inputs**: `hide_channel_sockets()` (after each build, and from `update()` when links change) hides every unlinked channel socket. The sockets stay in the group interface, which carries the values and keyframes; a linked one (Ctrl+H) stays visible.
  - `is_up_to_date()`: the compiled key matches the current script, Group and domain (drives the green outline).
  - `sync(force, pending_line=None)`: the key is `version|domain|md5(group + src)` (`_key()`, + `|pending` while ch* names are held back). `warning` is set from `program.warnings`. It recompiles on change, with `keep_stale` while pending. It also rebuilds when the inner nodes were edited by hand (`tree_edited()`: `tree_print(tree)`, a digest of node names, types, mute states and links, differs from `built_print`, stored after each build), so a node muted inside comes back on the next edit even if the script didn't change; `restore=False` (the `_watch` timer and `_on_undo`) skips this check, so they never undo what someone is doing inside. On `BexError` it keeps the last good nodes and sets `error`. On any other exception it logs the traceback. Every rebuild calls `_redraw()`.
  - `saved_values`: JSON `{name: [socket type, value]}` written before each build (`_save_values`). A newly created input takes its remembered value when the type matches, else the script default.
- `_redraw()` tags the Node Editor, 3D Viewport, Properties and Spreadsheet areas for redraw, because a recompile from a timer sends no UI notifier.
- Updates:
  - `_watch()`: persistent 1 s timer. For scripts linked to a file it reloads changes (`_reload_from_disk`). For scripts open in a Text Editor (`_texts_in_editors`) it syncs, holding back the cursor line's ch* names while the text changed less than `TYPING_IDLE` ago (`_text_changes`). Skips the wrangle being edited on the node.
  - `_on_load` (`load_post`) forgets the editor's undo histories and scroll positions and force-syncs everything; `_on_undo` (`undo_post` / `redo_post`) syncs.
  - `sync_all(force)` reloads disk-linked Texts, then syncs every wrangle (`force=True` rebuilds even unchanged ones). It also runs once after `register()`.
- Operators:
  - `bex.edit_script`: opens the Text Editor (or a new window if none is open); creates the script if the node has none.
  - `bex.update_node`: re-reads the script's file, then forces a rebuild of this node (no button any more; available from F3 search).
  - `bex.edit_nodes`: goes inside the wrangle with `space.path.append(node_tree, node=node)`, because Blender's Tab only enters built-in group nodes. It's bound to **Tab** in the addon keymap (registered in `register()`, removed in `unregister()`); on other nodes its poll fails and Tab falls through to Blender's own group edit. Edits made inside (muting, deleting, relinking nodes) are replaced on the next sync, even one with an unchanged script (`tree_edited()`).
  - `bex.recompile`: forces a rebuild of every wrangle.
- `_field(layout, node, label, width=None, indent=0)`: a right-aligned label in a fixed `LABEL_WIDTH` column, then the field; `indent` moves both right.
- `register()` also registers `ramp_widget` (first: `bex_ramps` uses its types), `library` (its operator and menu), `presets`, `code_editor` (its operators, draw handler and the LEFTMOUSE / wheel / trackpad keymap items), the ramp keymap items (`bex.ramp_edit` on LEFTMOUSE in "Node Editor" and in the "Node Tool: …" keymaps, which Blender checks first; `bex.ramp_delete` on DEL, BACK_SPACE and X) and the app handlers.

## `bex/presets.py`

- Presets are plain `.bex` files: built-in ones in `BUILTIN_DIR` (`bex/builtin_presets/`), the user's in `user_dir()` (the extension's user folder via `bpy.utils.extension_path_user`, or `scripts/presets/bex` when loaded as a plain module). `builtin_presets()` / `user_presets()` list them (`_scan`), `save(name, source)` writes one (`clean_name` makes the name file-safe; same name replaces), `load_into(node, path)` replaces the node's code and syncs.
- `NODE_MT_bex_presets`: the menu (built-ins, then the user's, then Save as Preset… and Open Presets Folder). It is opened from the node's layout, which sets `context.node` (`context_pointer_set`).
- Operators: `bex.load_preset` (`filepath`, undoable), `bex.save_preset` (name dialog; remembers the node's tree and name in `invoke`, because the dialog's context has no `node`), `bex.open_presets_folder`.
- `_add_menu` adds **BEX Wrangle** to the Geometry Nodes Add menu.

## Tests

```sh
python3 tests/test_lang.py
python3 tests/test_editor.py
python3 tests/test_ramp.py
"/Applications/Blender 5.2.app/Contents/MacOS/Blender" -b --factory-startup --python tests/test_blender.py
```

- `test_ramp.py`: ramp maths (`bex/ramp.py`: presets, the quarter-circle Round / Sharp, Bezier roles, to/from Bezier, insert, delete, reverse / invert), and `test_matches_houdini`: every interpolation against Houdini's own ramps, sampled into `houdini_ramps.json` by `hython tests/houdini_ramps.py` (re-run it after changing its cases; Bezier cases go through a real ramp parameter, because a bare `hou.Ramp` sorts its keys).
- `test_lang.py`: Python syntax (indentation, elif, one-line blocks, chained comparisons, `**` `//`, error hints), Python-style variables, folding, attribute types, sequencing, if→switch, scopes, the write plan (Selection instead of Switch, Set Position Offset, captures only when needed), loops (short ones unrolled, constant ones folded, else one Repeat Zone: items, counts, widening types, `v[i]`, nested counts, per-element counts with the Group, break / continue, while, chunks, errors), functions (inlining, defaults and keywords, globals, early returns, return in loops, recursion, attribute writes in loops, closures for shared calls, errors), channels, the pending line, CSE, functions, error messages and lines, `.0`–`.2` components, the Group (expressions, Houdini patterns, `@group_`, types, warnings), other inputs (`point`/`prim`, `@opinputN_`, counts, `nearpoint`, no captures for other-input reads, errors), the curve built-ins and the domain-aware index built-ins (with their hints), the data functions (colour / maths folding, out arguments, topology as a field or a sample, per-element topology loop counts, errors).
- `test_editor.py`: buffer editing, selection, movement, undo grouping (word steps, Backspace runs), VS Code-style `select_at`, `suggest`, `signature_at` (nesting, strings, comments, lines) and every function having a signature; highlighter kinds (including `@P.1`), coverage, block comments, fuzzing; key mapping (platform modifiers, clipboard, exit, zoom) and `View`.
- `test_blender.py`: real evaluated geometry. Covers bounding box / mapping functions, `@curveu` / `@curvelength` on curves, the index built-ins per domain, `@tangent`, the data functions (topology on every domain on input 0 and Input 2, `@area` `@island` `@edgeangle`, `xyzdist` / `minpos` / `intersect` with out arguments, colour and vector maths per element), the library groups (shared, nested, found after a rename, rebuilt on a version bump, usable in a user tree), position and channels, attributes and sequencing, swap semantics, the write plan (conditional writes keep old values, also from another domain; Offset with Selection; captures only when needed), the face domain, keeping the last good nodes on error, sync_all and copy, hidden own scripts, presets, channels as properties, the Group (several writes, constant writes, float attributes, patterns, bad groups), other inputs (an Object Info into "Input 2": point(), @opinput1_, npoints, nearpoint, getbbox_max(1), the four inputs always present and keeping their links), scrolling, the pending line (inputs held back, not removed, values remembered), incoming attributes (object data, Store/Remove Named Attribute, upstream wrangles, group nodes), typing through the editor's key path (Tab-accepting completions, compiling only on focus loss, Esc hiding the signature help before leaving, undo history kept across refocus and per script), the layout (no overlaps, links flow left to right, per-row Group Inputs), loops as Repeat Zones (a channel count including 0, attribute writes and an `if` inside, nested, `range(@ptnum)` with and without a Group, break / continue, a per-point while, chunked Fibonacci across chunk boundaries with no hidden attributes left, `v[i]`), functions (early return, `while True` with return, recursion, attribute writes from a loop) and shared functions as one Closure, and ramps (chramp() nodes, header actions and the interpolation menu, mouse editing with selection and delete, values past 0–1, Bezier: switching, splitting, anchors carrying handles, aligned handles that don't cross).
- The draw placement can only be checked with a UI: run Blender without `-b` and with `--enable-event-simulate`, then screenshot (`bpy.ops.screen.screenshot`).

## Adding a language function (checklist)

1. `compiler.py`: add it to `MATH1` / `MATH2` or a branch in `Compiler.call` (and to `FUNCTION_NAMES` if it's a `call` branch), and give it a `SIGNATURES` entry. If it is one Blender node, map it to that node (a new IR kind needs steps 2 and 3). If it needs several nodes, add a `GroupDef` to `library.GROUPS` and emit `self.node("group", type, inputs, out=i, name=key)`.
2. `builder.py`: add the IR kind to `create()`, and the node's idname to `BUTTON_ROWS`.
3. Bump `COMPILER_VERSION` in `node.py` if output for existing scripts changes.
4. Add tests in `tests/test_lang.py` (and `test_blender.py` if node wiring is involved).
5. Update the function table in `README.md` and this file.
