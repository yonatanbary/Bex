"""Script presets: the **Presets** menu on a BEX Wrangle.

Presets are plain ``.bex`` files. Built-in ones ship in ``bex/builtin_presets/``; the
user's own are saved to the extension's user folder, so every project sees them
and add-on updates keep them. Picking one replaces the node's code (undoable).
"""

import os
from pathlib import Path

import bpy

EXT = ".bex"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_presets"


def user_dir(create=False):
    """Folder for the user's presets."""
    try:
        return Path(bpy.utils.extension_path_user(__package__, path="presets", create=create))
    except (ValueError, KeyError):
        # Loaded as a plain module (development / tests), not as an extension.
        return Path(bpy.utils.user_resource("SCRIPTS", path=os.path.join("presets", "bex"), create=create))


def _scan(folder):
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.suffix.lower() == EXT and p.is_file()), key=lambda p: p.stem.lower())


def builtin_presets():
    return _scan(BUILTIN_DIR)


def user_presets():
    return _scan(user_dir())


def clean_name(name):
    """A preset name that is safe as a file name."""
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"/\\|?*').strip(" .")
    return name


def save(name, source):
    """Write a user preset, replacing one with the same name. Returns its path."""
    name = clean_name(name)
    if not name:
        raise ValueError("Preset name is empty")
    path = user_dir(create=True) / (name + EXT)
    path.write_text(source, encoding="utf-8")
    return path


def load_into(node, path):
    """Replace the node's code with a preset and recompile."""
    from . import node as bex_node
    source = Path(path).read_text(encoding="utf-8")
    if node.script is None:
        node.script = bex_node._new_text()
    node.script.from_string(source)
    node.sync()


def _wrangle(context):
    node = getattr(context, "node", None)
    return node if node is not None and node.bl_idname == "GeometryNodeBexWrangle" else None


class BEX_OT_load_preset(bpy.types.Operator):
    bl_idname = "bex.load_preset"
    bl_label = "Load BEX Preset"
    bl_description = "Replace this wrangle's code with the preset"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _wrangle(context) is not None

    @classmethod
    def description(cls, context, props):
        return f"Replace this wrangle's code with {Path(props.filepath).stem}"

    def execute(self, context):
        try:
            load_into(_wrangle(context), self.filepath)
        except OSError as exc:
            self.report({"ERROR"}, f"Can't read preset: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class BEX_OT_save_preset(bpy.types.Operator):
    bl_idname = "bex.save_preset"
    bl_label = "Save BEX Preset"
    bl_description = "Save this wrangle's code as a preset, available in every project"

    name: bpy.props.StringProperty(name="Name", options={"SKIP_SAVE"})
    # The node is remembered when the dialog opens: the dialog's own context
    # no longer carries the node the menu was opened from.
    tree_name: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    node_name: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.type == "NODE_EDITOR"

    def invoke(self, context, event):
        node = _wrangle(context)
        if node is None or node.script is None:
            return {"CANCELLED"}
        self.tree_name, self.node_name = node.id_data.name, node.name
        return context.window_manager.invoke_props_dialog(self, confirm_text="Save")

    def draw(self, context):
        self.layout.prop(self, "name")
        if clean_name(self.name) and (user_dir() / (clean_name(self.name) + EXT)).exists():
            self.layout.label(text="Replaces the preset with this name", icon="INFO")

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else _wrangle(context)
        if node is None or node.script is None:
            self.report({"ERROR"}, "The wrangle to save is gone")
            return {"CANCELLED"}
        try:
            path = save(self.name, node.script.as_string())
        except (ValueError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved preset {path.stem}")
        return {"FINISHED"}


class BEX_OT_open_presets_folder(bpy.types.Operator):
    bl_idname = "bex.open_presets_folder"
    bl_label = "Open Presets Folder"
    bl_description = "Show the folder with your BEX presets (rename or delete them there)"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=str(user_dir(create=True)))
        return {"FINISHED"}


class NODE_MT_bex_presets(bpy.types.Menu):
    bl_idname = "NODE_MT_bex_presets"
    bl_label = "Presets"

    def draw(self, context):
        layout = self.layout
        for path in builtin_presets():
            layout.operator(BEX_OT_load_preset.bl_idname, text=path.stem, icon="FILE_SCRIPT").filepath = str(path)
        mine = user_presets()
        if mine:
            layout.separator()
            for path in mine:
                layout.operator(BEX_OT_load_preset.bl_idname, text=path.stem, icon="USER").filepath = str(path)
        layout.separator()
        layout.operator(BEX_OT_save_preset.bl_idname, text="Save as Preset…", icon="ADD")
        layout.operator(BEX_OT_open_presets_folder.bl_idname, icon="FILE_FOLDER")


classes = (BEX_OT_load_preset, BEX_OT_save_preset, BEX_OT_open_presets_folder, NODE_MT_bex_presets)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
