"""The BEX Wrangle node, its operators and the live-update timer."""

import hashlib
import json
import os
import time
import traceback

import bpy
from bpy.app.handlers import persistent

from . import builder, code_editor, library
from .lang import BexError, compile_source

# Bump when the generated node layout changes, so existing nodes recompile.
COMPILER_VERSION = 7
WATCH_INTERVAL = 1.0   # Text Editor / file check, only while a script is open or on disk
TYPING_IDLE = 2.0      # Text Editor: a line counts as finished after this pause
SAVED_LIMIT = 64       # remembered values of removed inputs, per node
TREE_PREFIX = ".BEX "

TEMPLATE = """\
# BEX wrangle: runs once per element, like a Houdini wrangle.
#   @P @N @ptnum @Time      built-in attributes
#   f@mask  v@dir  i@count  your own attributes (float / vector / int / b@ bool)
#   chf("name", default)    adds an input socket to the node (chi, chv, chb too)

amp = chf("amplitude", 0.2)
freq = chf("frequency", 4.0)

@P += @N * sin(@P.x * freq + @Time) * amp
"""

DOMAINS = [
    ("POINT", "Points", "Run over points"),
    ("EDGE", "Edges", "Run over edges"),
    ("FACE", "Faces", "Run over faces"),
    ("CORNER", "Face Corners", "Run over face corners"),
    ("CURVE", "Splines", "Run over splines"),
    ("INSTANCE", "Instances", "Run over instances"),
]

_file_mtimes = {}  # Text name -> mtime of its file when last synced
_text_changes = {}  # Text name -> (md5, time it last changed), for the Text Editor watch


def _log(msg):
    print(f"[BEX] {msg}")


def _on_change(node, _context):
    node.sync()


def _socket_value(sock):
    v = sock.default_value
    return list(v) if hasattr(v, "__len__") else v


class BexWrangleNode(bpy.types.GeometryNodeCustomGroup):
    bl_idname = "GeometryNodeBexWrangle"
    bl_label = "BEX Wrangle"
    bl_description = "Write VEX-style code that is compiled into geometry nodes"
    bl_width_default = 260

    script: bpy.props.PointerProperty(type=bpy.types.Text, name="Script", description="BEX code for this node", update=_on_change)
    domain: bpy.props.EnumProperty(items=DOMAINS, name="Run Over", default="POINT", update=_on_change)
    show_code: bpy.props.BoolProperty(name="Show Code", default=True, description="Show and edit the script on the node")
    error: bpy.props.StringProperty()
    compiled_key: bpy.props.StringProperty()
    # JSON {name: [socket type, value]} of input values, so an input that is
    # removed and comes back (undo, rename back) gets its value back.
    saved_values: bpy.props.StringProperty()
    # JSON {"reads": [[name, type]], "writes": [[name, type]], "nodes": n} of the
    # last good compile, shown under the code.
    info: bpy.props.StringProperty()
    code_scale: bpy.props.FloatProperty(
        name="Code Size", default=1.0, min=0.5, max=3.0,
        description="Text size of the code on the node (Ctrl + / Ctrl - while editing, Ctrl 0 resets)",
    )

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == "GeometryNodeTree"

    def init(self, context):
        # A new wrangle comes with its own script, ready to edit.
        self.node_tree = bpy.data.node_groups.new(TREE_PREFIX + "Wrangle", "GeometryNodeTree")
        self.script = _new_text()
        self.sync()

    def copy(self, node):
        # A duplicate owns its node group and a copy of the script, so editing
        # one never silently changes the other. Pick the same Text in the
        # script field to share a script on purpose.
        if node.node_tree:
            self.node_tree = node.node_tree.copy()
        if node.script:
            self.script = node.script.copy()

    def draw_label(self):
        return f"BEX: {self.script.name}" if self.script else "BEX Wrangle"

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "script", text="")
        if self.script:
            row.operator("bex.edit_script", text="", icon="GREASEPENCIL")
        else:
            row.operator("bex.new_script", text="New", icon="ADD")
        row = layout.row(align=True)
        row.prop(self, "domain", text="")
        row.prop(self, "show_code", text="", icon="HIDE_OFF" if self.show_code else "HIDE_ON")
        row.operator("bex.edit_nodes", text="", icon="NODETREE")
        if self.error:
            col = layout.column(align=True)
            col.alert = True
            col.label(text=self.error, icon="ERROR")
        self.draw_channels(layout)
        if self.show_code and self.script:
            code_editor.reserve(layout, self)

    def channels(self):
        return [s for s in self.inputs if s.enabled and s.type != "GEOMETRY"]

    def draw_channels(self, layout):
        """The ch*() inputs as fields above the code, like a wrangle's parameters.
        The sockets stay linkable; a linked one is shown greyed out."""
        chans = self.channels()
        if not chans:
            return
        col = layout.column(align=True)
        for sock in chans:
            row = col.row(align=True)
            if sock.is_linked:
                row.active = False
                row.label(text=sock.name, icon="LINKED")
            else:
                row.prop(sock, "default_value", text=sock.name)

    def is_up_to_date(self):
        """True when the nodes were built from the script as it is now."""
        src = self.script.as_string() if self.script else ""
        key = f"{COMPILER_VERSION}|{self.domain}|{hashlib.md5(src.encode()).hexdigest()}"
        return self.compiled_key == key

    def sync(self, force=False, pending_line=None):
        """Recompile if the script or settings changed since the last build.

        `pending_line` (1-based) is the line being typed: new ch*() names on it
        don't create inputs yet, and no input is removed meanwhile.
        """
        src = self.script.as_string() if self.script else ""
        digest = hashlib.md5(src.encode()).hexdigest()
        key = f"{COMPILER_VERSION}|{self.domain}|{digest}"
        if pending_line is None and key == self.compiled_key and not force and self.node_tree:
            return
        name = self.script.name if self.script else "(no script)"
        try:
            program = compile_source(src, self.domain, pending_line, [s.name for s in self.channels()])
            if program.pending:
                key += "|pending"
            if key == self.compiled_key and not force and self.node_tree:
                return
            self.compiled_key = key
            if not self.node_tree:
                self.node_tree = bpy.data.node_groups.new(TREE_PREFIX + "Wrangle", "GeometryNodeTree")
            saved = self._save_values()
            count, created = builder.build(self.node_tree, program, self.domain, keep_stale=bool(program.pending))
        except BexError as e:
            # Keep the last working nodes so the scene does not break while typing.
            self.compiled_key = key
            self.error = str(e)
            _log(f"{name}: error, {e}")
            _redraw()
            return
        except Exception:
            self.compiled_key = key
            self.error = "internal error, see the console"
            _log(f"{name}: internal error")
            traceback.print_exc()
            _redraw()
            return
        self.error = ""
        for input_name, default in created:
            # A new input starts at its remembered value, else the script's default.
            sock = self.inputs.get(input_name)
            if sock is None:
                continue
            kept = saved.get(input_name)
            try:
                sock.default_value = kept[1] if kept and kept[0] == sock.type else default
            except (TypeError, ValueError):
                sock.default_value = default
        self.node_tree.name = TREE_PREFIX + name
        self.info = json.dumps({
            "reads": [[n, t] for n, t in program.reads],
            "writes": [[n, t] for n, t, _ in program.writes],
            "nodes": count,
        })
        _log(f"{name}: compiled to {count} nodes ({len(program.writes)} attributes written, {len(program.params)} inputs)")
        _redraw()

    def _save_values(self):
        try:
            saved = json.loads(self.saved_values) if self.saved_values else {}
        except ValueError:
            saved = {}
        for sock in self.channels():
            saved.pop(sock.name, None)  # re-insert so recently used names are kept longest
            saved[sock.name] = [sock.type, _socket_value(sock)]
        saved = dict(list(saved.items())[-SAVED_LIMIT:])
        self.saved_values = json.dumps(saved)
        return saved


def _redraw():
    """A recompile from the timer sends no UI notifier, so Blender would keep showing
    the old nodes and viewport until something else (e.g. the mouse leaving the Text
    Editor) triggered a redraw. Ask for one explicitly."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in ("NODE_EDITOR", "VIEW_3D", "PROPERTIES", "SPREADSHEET"):
                area.tag_redraw()


def _new_text():
    text = bpy.data.texts.new("wrangle.bex")
    text.from_string(TEMPLATE)
    return text


def _reload_from_disk(text):
    """Pull in changes when a script's file was saved by an external editor."""
    if not text.filepath:
        return
    path = bpy.path.abspath(text.filepath)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    last = _file_mtimes.get(text.name)
    _file_mtimes[text.name] = mtime
    if last is None or mtime == last:
        return
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return
    if content != text.as_string():
        text.from_string(content)
        _log(f"{text.name}: reloaded from {path}")


def wrangle_nodes():
    for tree in bpy.data.node_groups:
        if tree.bl_idname != "GeometryNodeTree" or tree.name.startswith(TREE_PREFIX):
            continue
        for node in tree.nodes:
            if node.bl_idname == BexWrangleNode.bl_idname:
                yield node


def sync_all(force=False):
    """force=True rebuilds every wrangle, including ones with Auto Update off."""
    nodes = list(wrangle_nodes())
    for text in {n.script for n in nodes if n.script}:
        _reload_from_disk(text)
    for node in nodes:
        node.sync(force)


def _texts_in_editors():
    wm = getattr(bpy.context, "window_manager", None)
    shown = set()
    for window in wm.windows if wm else ():
        for area in window.screen.areas:
            if area.type == "TEXT_EDITOR" and area.spaces.active.text:
                shown.add(area.spaces.active.text.name)
    return shown


def _watch():
    """Blender sends no event when a Text is typed in or its file changes on disk,
    so scripts open in a Text Editor or linked to a file are checked once a second.
    Everything else (the on-node editor, buttons, undo, loading) updates by event."""
    try:
        shown = _texts_in_editors()
        now = time.monotonic()
        for node in wrangle_nodes():
            text = node.script
            if text is None or code_editor.is_editing(node):
                continue
            if text.filepath:
                _reload_from_disk(text)
            elif text.name not in shown:
                continue
            digest = hashlib.md5(text.as_string().encode()).hexdigest()
            last = _text_changes.get(text.name)
            if last is None or last[0] != digest:
                _text_changes[text.name] = last = (digest, now if last else 0.0)
            typing = text.name in shown and now - last[1] < TYPING_IDLE
            node.sync(pending_line=text.current_line_index + 1 if typing else None)
    except Exception:
        traceback.print_exc()
    return WATCH_INTERVAL


@persistent
def _on_load(*_args):
    _file_mtimes.clear()
    _text_changes.clear()
    sync_all(force=True)


@persistent
def _on_undo(*_args):
    sync_all()


def _sync_after_register():
    try:
        sync_all(force=True)
    except Exception:
        traceback.print_exc()
    return None


class BEX_OT_new_script(bpy.types.Operator):
    bl_idname = "bex.new_script"
    bl_label = "New BEX Script"
    bl_description = "Create a script for this wrangle"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return getattr(context, "node", None) is not None

    def execute(self, context):
        context.node.script = _new_text()
        return {"FINISHED"}


class BEX_OT_edit_script(bpy.types.Operator):
    bl_idname = "bex.edit_script"
    bl_label = "Edit BEX Script"
    bl_description = "Show this wrangle's script in a Text Editor"

    @classmethod
    def poll(cls, context):
        node = getattr(context, "node", None)
        return node is not None and getattr(node, "script", None) is not None

    def execute(self, context):
        text = context.node.script
        area = next((a for a in context.screen.areas if a.type == "TEXT_EDITOR"), None)
        if area is None:
            # No text editor on screen: open one in its own window.
            before = set(context.window_manager.windows)
            bpy.ops.wm.window_new()
            window = next(w for w in context.window_manager.windows if w not in before)
            area = window.screen.areas[0]
            area.type = "TEXT_EDITOR"
        area.spaces.active.text = text
        return {"FINISHED"}


class BEX_OT_update_node(bpy.types.Operator):
    bl_idname = "bex.update_node"
    bl_label = "Update BEX Wrangle"
    bl_description = "Recompile this wrangle from its script now"

    @classmethod
    def poll(cls, context):
        return getattr(context, "node", None) is not None and hasattr(context.node, "sync")

    def execute(self, context):
        node = context.node
        if node.script:
            _reload_from_disk(node.script)
        node.sync(force=True)
        return {"FINISHED"}


def _wrangle_in_context(context):
    node = getattr(context, "node", None) or getattr(context, "active_node", None)
    if node is not None and node.bl_idname == BexWrangleNode.bl_idname and node.node_tree:
        return node
    return None


class BEX_OT_edit_nodes(bpy.types.Operator):
    """Blender's Tab only enters built-in group nodes, so this pushes the
    wrangle's node group onto the editor path itself (Tab / Ctrl+Tab leave it)."""

    bl_idname = "bex.edit_nodes"
    bl_label = "Go Inside BEX Wrangle"
    bl_description = "Show the nodes this wrangle compiled to. Edits made inside are replaced on the next recompile"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR" and _wrangle_in_context(context) is not None

    def execute(self, context):
        node = _wrangle_in_context(context)
        context.space_data.path.append(node.node_tree, node=node)
        try:
            bpy.ops.node.view_all()
        except RuntimeError:
            pass
        return {"FINISHED"}


class BEX_OT_recompile(bpy.types.Operator):
    bl_idname = "bex.recompile"
    bl_label = "Recompile All BEX Wrangles"
    bl_description = "Rebuild the nodes of every BEX Wrangle from its script"

    def execute(self, context):
        sync_all(force=True)
        return {"FINISHED"}


def _add_menu(self, context):
    if context.space_data.tree_type == "GeometryNodeTree":
        self.layout.separator()
        op = self.layout.operator("node.add_node", text="BEX Wrangle", icon="TEXT")
        op.type = BexWrangleNode.bl_idname
        op.use_transform = True
        self.layout.menu(library.NODE_MT_bex_library.bl_idname, icon="NODETREE")


classes = (BexWrangleNode, BEX_OT_new_script, BEX_OT_edit_script, BEX_OT_update_node, BEX_OT_edit_nodes, BEX_OT_recompile)
_keymaps = []


_HANDLERS = (
    (bpy.app.handlers.load_post, _on_load),
    (bpy.app.handlers.undo_post, _on_undo),
    (bpy.app.handlers.redo_post, _on_undo),
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    code_editor.register()
    library.register()
    bpy.types.NODE_MT_add.append(_add_menu)
    # Tab on a BEX Wrangle goes inside. On any other node our poll fails and
    # Tab falls through to Blender's own group edit.
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
        _keymaps.append((km, km.keymap_items.new(BEX_OT_edit_nodes.bl_idname, "TAB", "PRESS")))
        # A click on a wrangle's code starts the editor; elsewhere it passes through.
        _keymaps.append((km, km.keymap_items.new(code_editor.BEX_OT_code_edit.bl_idname, "LEFTMOUSE", "PRESS")))
    for handlers, fn in _HANDLERS:
        if fn not in handlers:
            handlers.append(fn)
    bpy.app.timers.register(_sync_after_register, first_interval=0.0)
    bpy.app.timers.register(_watch, first_interval=WATCH_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    for handlers, fn in _HANDLERS:
        if fn in handlers:
            handlers.remove(fn)
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    bpy.types.NODE_MT_add.remove(_add_menu)
    library.unregister()
    code_editor.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
