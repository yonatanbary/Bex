"""Which named attributes reach a wrangle's Geometry input (a static guess).

Blender's Python API can't read the geometry flowing into a socket, so this walks
the node graph upstream instead: the modifier object's own attributes at the
tree's Group Input, Object Info objects, Store Named Attribute nodes, other
wrangles' writes, minus Remove Named Attribute. Group nodes are followed inside.
It can be wrong after nodes that drop or rename attributes in other ways.
"""

import fnmatch
import json
import time

import bpy

# Blender attribute data types -> BEX types (None: not usable as a BEX type).
_TYPES = {
    "FLOAT": "float", "INT": "int", "INT8": "int", "BOOLEAN": "bool",
    "FLOAT_VECTOR": "vector", "FLOAT_COLOR": "vector", "BYTE_COLOR": "vector", "FLOAT2": "vector",
    "QUATERNION": "quaternion", "FLOAT4X4": "matrix",
}
_HIDDEN = {"position"}       # shown as the built-in @P
CACHE_SECONDS = 0.3          # drawing asks every frame; the walk reruns at most this often
_MAX_DEPTH = 64              # guards against cycles through nested groups
_cache = {}                  # node pointer -> (time, result)


def incoming_attributes(node, input=0, fresh=False):
    """[(name, bex_type)] of named attributes probably on the geometry entering
    `node` (a BEX Wrangle) at geometry input `input` (0: Geometry, 1-3: Input 2-4),
    sorted by name. Cached for `CACHE_SECONDS` (drawing), unless `fresh` (compiling)."""
    key = (node.as_pointer(), input)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS and not fresh:
        return hit[1]
    found = {}
    geos = [s for s in node.inputs if s.type == "GEOMETRY"]
    geo = geos[input] if input < len(geos) else None
    if geo is not None:
        try:
            found = _from_input(geo, node.id_data, (), set(), 0)
        except (AttributeError, ReferenceError, RecursionError):
            found = {}
    result = sorted(found.items())
    _cache[key] = (now, result)
    return result


def _from_input(socket, tree, stack, seen, depth):
    """Attributes arriving at an input socket: the union over its links."""
    found = {}
    for link in socket.links:
        if link.is_muted or not link.is_valid:
            continue
        found.update(_from_output(link.from_node, link.from_socket, tree, stack, seen, depth + 1))
    return found


def _from_output(node, socket, tree, stack, seen, depth):
    if depth > _MAX_DEPTH:
        return {}
    key = (stack, tree.name, node.name, socket.identifier)
    if key in seen:
        return {}
    seen.add(key)
    idname = node.bl_idname

    def upstream():
        # Every linked geometry input (Join Geometry has several links on one).
        found = {}
        for sock in node.inputs:
            if sock.type == "GEOMETRY" and sock.enabled:
                found.update(_from_input(sock, tree, stack, seen, depth))
        return found

    if node.mute or idname == "NodeReroute":
        return upstream()
    if idname == "NodeGroupInput":
        if stack:  # inside a group: continue from the group node's matching input
            group_node, outer_tree = stack[-1]
            sock = next((s for s in group_node.inputs if s.identifier == socket.identifier), None)
            return _from_input(sock, outer_tree, stack[:-1], seen, depth) if sock else {}
        return _modifier_objects_attributes(tree)
    if idname == "GeometryNodeGroup" and node.node_tree is not None:
        inner = node.node_tree
        out = next((n for n in inner.nodes if n.bl_idname == "NodeGroupOutput" and n.is_active_output), None)
        out = out or next((n for n in inner.nodes if n.bl_idname == "NodeGroupOutput"), None)
        sock = out and next((s for s in out.inputs if s.identifier == socket.identifier), None)
        return _from_input(sock, inner, stack + ((node, tree),), seen, depth) if sock else {}
    if idname == "GeometryNodeObjectInfo":
        obj = node.inputs["Object"].default_value if not node.inputs["Object"].is_linked else None
        return _object_attributes(obj) if obj else {}
    if idname == "GeometryNodeBexWrangle":  # passes on its Geometry, not "Input 2"...
        found = _from_input(node.inputs[0], tree, stack, seen, depth)
    else:
        found = upstream()
    if idname == "GeometryNodeStoreNamedAttribute":
        name = _const(node.inputs.get("Name"))
        if name:
            found[name] = _TYPES.get(node.data_type)
    elif idname == "GeometryNodeRemoveAttribute":
        pattern = _const(node.inputs.get("Name"))
        if pattern:
            wildcard = getattr(node, "pattern_mode", "EXACT") == "WILDCARD"
            for name in list(found):
                if fnmatch.fnmatchcase(name, pattern) if wildcard else name == pattern:
                    del found[name]
    elif idname == "GeometryNodeBexWrangle":
        try:
            writes = json.loads(node.info).get("writes", []) if node.info else []
        except ValueError:
            writes = []
        for name, type in writes:
            if name not in _HIDDEN and name != "P":
                found[name] = type
    return found


def _const(socket):
    """A string socket's value when it is not linked, else None."""
    if socket is None or socket.is_linked:
        return None
    return getattr(socket, "default_value", None) or None


def _modifier_objects_attributes(tree):
    """Attributes of the objects whose Geometry Nodes modifier runs `tree`
    (the original data: modifiers before it are not taken into account)."""
    found = {}
    for obj in bpy.data.objects:
        if any(m.type == "NODES" and m.node_group == tree for m in obj.modifiers):
            found.update(_object_attributes(obj))
    return found


def _object_attributes(obj):
    data = getattr(obj, "data", None)
    attributes = getattr(data, "attributes", None)
    if attributes is None:
        return {}
    return {a.name: _TYPES.get(a.data_type) for a in attributes
            if not a.name.startswith(".") and a.name not in _HIDDEN}
