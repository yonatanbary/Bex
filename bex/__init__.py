"""BEX: VEX-style wrangle scripting for Blender Geometry Nodes."""

from . import node


def register():
    node.register()


def unregister():
    node.unregister()
