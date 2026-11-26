"""Helpers shared by every ShiroTools module."""

import os

import bpy

TAB = "Shiro"  # default N-panel tab, overridable in the add-on preferences


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def human_bytes(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024.0:
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024.0
    return f"{f:.2f} TB"


def human_count(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def redraw(context=None, types=None):
    """Tag every area (optionally only some editor types) for redraw."""
    wm = bpy.context.window_manager if context is None else context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if types is None or area.type in types:
                area.tag_redraw()


def stat_row(layout, label, value, icon="NONE", alert=False):
    row = layout.row(align=True)
    row.alert = alert
    row.label(text=label, icon=icon)
    sub = row.row()
    sub.alignment = "RIGHT"
    sub.label(text=str(value))
    return row


SEVERITY_ICON = {"ERROR": "CANCEL", "WARNING": "ERROR", "INFO": "INFO", "OK": "CHECKMARK"}


# ---------------------------------------------------------------------------
# images / materials
# ---------------------------------------------------------------------------

def iter_image_nodes(node_tree, _seen=None):
    """Yield (node, image) for every image node in a tree, recursing into groups."""
    if node_tree is None:
        return
    if _seen is None:
        _seen = set()
    key = node_tree.as_pointer()
    if key in _seen:
        return
    _seen.add(key)
    for node in node_tree.nodes:
        if node.type in {"TEX_IMAGE", "TEX_ENVIRONMENT"}:
            if node.image is not None:
                yield node, node.image
        elif node.type == "GROUP" and node.node_tree is not None:
            yield from iter_image_nodes(node.node_tree, _seen)


def material_images(mat):
    if mat is None or mat.node_tree is None:
        return set()
    return {img for _node, img in iter_image_nodes(mat.node_tree)}


def object_materials(obj):
    return [slot.material for slot in obj.material_slots if slot.material is not None]


def object_images(obj):
    imgs = set()
    for mat in object_materials(obj):
        imgs |= material_images(mat)
    # Geometry Nodes modifiers can carry images as plain inputs.
    for mod in obj.modifiers:
        if mod.type == "NODES" and mod.node_group is not None:
            for value in gn_input_values(mod):
                if isinstance(value, bpy.types.Image):
                    imgs.add(value)
    return imgs


def scene_objects_deep(scene):
    """Scene objects plus everything reachable through collection instances."""
    seen = set()
    stack = list(scene.objects)
    while stack:
        obj = stack.pop()
        key = obj.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        yield obj
        if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
            stack.extend(obj.instance_collection.all_objects)


def image_abspath(img):
    if not img.filepath:
        return ""
    return os.path.normpath(bpy.path.abspath(img.filepath, library=img.library))


# ---------------------------------------------------------------------------
# Geometry Nodes modifier inputs (5.2+ RNA API)
# ---------------------------------------------------------------------------

def gn_input_items(node_group):
    for item in node_group.interface.items_tree:
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT":
            yield item


def gn_input_identifier(mod, name):
    if mod is None or getattr(mod, "node_group", None) is None:
        return None
    for item in gn_input_items(mod.node_group):
        if item.name == name:
            return item.identifier
    return None


def gn_input(mod, name):
    """The modifier's RNA input struct for socket `name`, or None."""
    identifier = gn_input_identifier(mod, name)
    if identifier is None:
        return None
    return getattr(mod.properties.inputs, identifier, None)


def gn_get(mod, name, default=None):
    sock = gn_input(mod, name)
    if sock is None or not hasattr(sock, "value"):
        return default
    return sock.value


def gn_set(mod, name, value):
    sock = gn_input(mod, name)
    if sock is None or not hasattr(sock, "value"):
        return False
    sock.value = value
    mod.id_data.update_tag()  # 5.2 does not re-evaluate on Python sets by itself
    return True


def gn_input_values(mod):
    inputs = mod.properties.inputs
    for item in gn_input_items(mod.node_group):
        sock = getattr(inputs, item.identifier, None)
        if sock is not None and hasattr(sock, "value"):
            yield sock.value


# ---------------------------------------------------------------------------
# mesh stats
# ---------------------------------------------------------------------------

def mesh_tri_count(mesh):
    """Triangle count without building loop triangles."""
    import numpy as np
    n = len(mesh.polygons)
    if n == 0:
        return 0
    totals = np.empty(n, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return int(totals.sum() - 2 * n)


# ---------------------------------------------------------------------------
# context helpers
# ---------------------------------------------------------------------------

class object_mode:
    """Context manager: drop to Object Mode for the duration, then go back."""

    def __init__(self, context):
        self.context = context
        self.previous = None

    def __enter__(self):
        obj = self.context.object
        self.previous = obj.mode if obj else "OBJECT"
        if self.previous != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        return self.previous

    def __exit__(self, *exc):
        if self.previous and self.previous != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=self.previous)
            except RuntimeError:
                pass
        return False


def selected_only(context, objects, active=None):
    """Make exactly `objects` selected (and `active` active)."""
    for obj in context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if active is not None:
        context.view_layer.objects.active = active


def addon_prefs(context=None):
    ctx = context or bpy.context
    addon = ctx.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def smart_uv(context, objects):
    """Smart UV Project each mesh (adds a UV map first when there is none)."""
    for obj in objects:
        if obj.type != "MESH":
            continue
        if not obj.data.uv_layers:
            obj.data.uv_layers.new(name="UVMap")
        with context.temp_override(active_object=obj, object=obj,
                                   selected_editable_objects=[obj], selected_objects=[obj]):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(island_margin=0.02)
            bpy.ops.object.mode_set(mode="OBJECT")
