"""Cloth Layers: vertex selections -> collision-distance layers for a GN cloth setup.

Data compatibility: same property names (Object.cloth_layers ...), same point
attribute ("cloth_layer") and lookup table object ("CLOTH_TABLE_<name>" with a
"distance" point attribute). Files made with v0.3 keep working.

Fixed for Blender 5.2+: modifier inputs are read/written through
``modifier.properties.inputs`` (the old ``mod[identifier]`` access silently
failed, so Spacing/Base fell back to defaults and the table never hooked up).

Set Up Cloth Layers builds a small "Shiro Cloth Layers" node group that
resolves each vertex's layer through the table and stores the final distance
(Base + distance * Spacing) as the float point attribute "cloth_distance",
ready for any cloth / collision setup to read.
"""

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .common import TAB, gn_get, gn_input_identifier, gn_set

ATTR_NAME = "cloth_layer"
TABLE_ATTR = "distance"
OUT_ATTR = "cloth_distance"
COLOR_ATTR = "cloth_layer_color"
TABLE_PREFIX = "CLOTH_TABLE_"
GROUP_NAME = "Shiro Cloth Layers"
UNASSIGNED = 0
PALETTE = ((0.95, 0.45, 0.30), (0.30, 0.70, 0.95), (0.55, 0.85, 0.35), (0.95, 0.80, 0.25),
           (0.75, 0.45, 0.95), (0.30, 0.90, 0.80), (0.95, 0.40, 0.65), (0.85, 0.60, 0.40))

_island_cache = {}


# ---------------------------------------------------------------------------
# mode handling
# ---------------------------------------------------------------------------

def leave_edit_mode(context):
    previous = context.object.mode
    if previous != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    return previous


def restore_mode(mode):
    if mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode=mode)
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# attribute helpers
# ---------------------------------------------------------------------------

def get_layer_attribute(mesh, create=True):
    attr = mesh.attributes.get(ATTR_NAME)
    if attr is not None:
        return attr if attr.domain == "POINT" and attr.data_type == "INT" else None
    return mesh.attributes.new(ATTR_NAME, "INT", "POINT") if create else None


def read_layer_values(mesh):
    attr = get_layer_attribute(mesh)
    if attr is None:
        return None
    values = np.zeros(len(mesh.vertices), dtype=np.int32)
    attr.data.foreach_get("value", values)
    return values


def write_layer_values(obj, values):
    attr = get_layer_attribute(obj.data)
    if attr is None:
        return False
    attr.data.foreach_set("value", values.astype(np.int32))
    update_colors(obj, values)
    obj.data.update()
    return True


def read_selection(mesh):
    flags = np.zeros(len(mesh.vertices), dtype=bool)
    mesh.vertices.foreach_get("select", flags)
    return flags


def write_selection(mesh, flags):
    mesh.vertices.foreach_set("select", flags)
    mesh.update()


def update_colors(obj, values=None):
    """Write per-vertex layer colours (only once the colour view was used)."""
    mesh = obj.data
    attr = mesh.color_attributes.get(COLOR_ATTR)
    if attr is None:
        return
    if values is None:
        values = read_layer_values(mesh)
        if values is None:
            return
    top = max([UNASSIGNED] + [l.uid for l in obj.cloth_layers])
    lut = np.full((top + 1, 4), (0.25, 0.25, 0.25, 1.0), dtype=np.float32)
    for layer in obj.cloth_layers:
        lut[layer.uid, :3] = layer.color
    cols = lut[np.clip(values, 0, top)]
    attr.data.foreach_set("color", cols.ravel())


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------

def find_layer(obj, uid):
    return next((l for l in obj.cloth_layers if l.uid == uid), None)


def active_layer(obj):
    i = obj.cloth_layers_index
    return obj.cloth_layers[i] if 0 <= i < len(obj.cloth_layers) else None


def update_counts(obj):
    values = read_layer_values(obj.data)
    if values is None:
        return
    counts = np.bincount(np.clip(values, 0, None),
                         minlength=max([1] + [l.uid + 1 for l in obj.cloth_layers]))
    for layer in obj.cloth_layers:
        layer.vert_count = int(counts[layer.uid]) if layer.uid < counts.size else 0
    obj.cloth_unassigned = int(counts[UNASSIGNED])


# ---------------------------------------------------------------------------
# modifier + table
# ---------------------------------------------------------------------------

def cloth_modifier(obj):
    """The GN modifier that takes the layer table, else the first GN modifier."""
    first = None
    for mod in obj.modifiers:
        if mod.type != "NODES" or mod.node_group is None:
            continue
        if gn_input_identifier(mod, "Layer Table") is not None:
            return mod
        first = first or mod
    return first


def scale_settings(obj):
    mod = cloth_modifier(obj)
    spacing = gn_get(mod, "Spacing", 1.0) if mod else 1.0
    base = gn_get(mod, "Base", 0.0) if mod else 0.0
    return float(spacing), float(base)


def effective_distance(obj, layer, scale=None):
    spacing, base = scale or scale_settings(obj)
    return base + layer.distance * spacing


def ensure_table(obj):
    table = obj.cloth_layer_table or bpy.data.objects.get(TABLE_PREFIX + obj.name)
    if table is None:
        name = TABLE_PREFIX + obj.name
        table = bpy.data.objects.new(name, bpy.data.meshes.new(name))
        colls = obj.users_collection
        (colls[0] if colls else bpy.context.scene.collection).objects.link(table)
        table.hide_render = True
        table.hide_select = True
        table.display_type = "WIRE"
        try:
            table.hide_set(True)
        except RuntimeError:
            pass
    obj.cloth_layer_table = table
    return table


def sync_table(obj):
    """Rebuild the table mesh so that point index == layer uid."""
    if obj is None or obj.type != "MESH":
        return
    table = ensure_table(obj)
    mesh = table.data
    size = max([UNASSIGNED] + [l.uid for l in obj.cloth_layers]) + 1
    distances = np.zeros(size, dtype=np.float32)
    for layer in obj.cloth_layers:
        distances[layer.uid] = layer.distance
    if len(mesh.vertices) != size:
        mesh.clear_geometry()
        mesh.vertices.add(size)
    coords = np.zeros(size * 3, dtype=np.float32)
    coords[0::3] = np.arange(size, dtype=np.float32)
    coords[2::3] = distances
    mesh.vertices.foreach_set("co", coords)
    attr = mesh.attributes.get(TABLE_ATTR)
    if attr is None:
        attr = mesh.attributes.new(TABLE_ATTR, "FLOAT", "POINT")
    attr.data.foreach_set("value", distances)
    mesh.update()
    mod = cloth_modifier(obj)
    if mod is not None and gn_get(mod, "Layer Table", None) != table:
        gn_set(mod, "Layer Table", table)


def _layer_changed(self, context):
    obj = context.object
    if obj is not None and obj.type == "MESH":
        sync_table(obj)
        update_colors(obj)


# ---------------------------------------------------------------------------
# set-up node group
# ---------------------------------------------------------------------------

def _sock(node, name, output=False):
    coll = node.outputs if output else node.inputs
    for s in coll:
        if s.name == name and s.enabled:
            return s
    return coll[name]


def build_setup_group():
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    iface = ng.interface
    iface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    iface.new_socket("Layer Table", in_out="INPUT", socket_type="NodeSocketObject")
    sp = iface.new_socket("Spacing", in_out="INPUT", socket_type="NodeSocketFloat")
    sp.default_value, sp.min_value = 1.0, 0.0
    base = iface.new_socket("Base", in_out="INPUT", socket_type="NodeSocketFloat")
    base.default_value = 0.0
    iface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    n, link = ng.nodes, ng.links

    gi = n.new("NodeGroupInput")
    gi.location = (-900, 0)
    go = n.new("NodeGroupOutput")
    go.location = (500, 0)
    info = n.new("GeometryNodeObjectInfo")
    info.transform_space = "ORIGINAL"
    info.location = (-650, -200)
    layer = n.new("GeometryNodeInputNamedAttribute")
    layer.data_type = "INT"
    _sock(layer, "Name").default_value = ATTR_NAME
    layer.location = (-650, -420)
    dist = n.new("GeometryNodeInputNamedAttribute")
    dist.data_type = "FLOAT"
    _sock(dist, "Name").default_value = TABLE_ATTR
    dist.location = (-650, -560)
    sample = n.new("GeometryNodeSampleIndex")
    sample.data_type = "FLOAT"
    sample.domain = "POINT"
    sample.location = (-380, -300)
    mad = n.new("ShaderNodeMath")
    mad.operation = "MULTIPLY_ADD"
    mad.location = (-150, -300)
    store = n.new("GeometryNodeStoreNamedAttribute")
    store.data_type = "FLOAT"
    store.domain = "POINT"
    _sock(store, "Name").default_value = OUT_ATTR
    store.location = (200, 0)

    link.new(_sock(gi, "Layer Table", True), _sock(info, "Object"))
    link.new(_sock(info, "Geometry", True), _sock(sample, "Geometry"))
    link.new(_sock(dist, "Attribute", True), _sock(sample, "Value"))
    link.new(_sock(layer, "Attribute", True), _sock(sample, "Index"))
    link.new(_sock(sample, "Value", True), mad.inputs[0])
    link.new(_sock(gi, "Spacing", True), mad.inputs[1])
    link.new(_sock(gi, "Base", True), mad.inputs[2])
    link.new(_sock(gi, "Geometry", True), _sock(store, "Geometry"))
    link.new(mad.outputs[0], _sock(store, "Value"))
    link.new(_sock(store, "Geometry", True), _sock(go, "Geometry"))
    return ng


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class SHIRO_PG_cloth_layer(PropertyGroup):
    name: StringProperty(name="Name", default="Layer")
    distance: FloatProperty(name="Distance", default=0.0, precision=4, step=1,
                            description="Collision distance for this layer, before Spacing "
                                        "and Base", update=_layer_changed)
    uid: IntProperty()
    vert_count: IntProperty()
    color: FloatVectorProperty(name="Color", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
                               default=PALETTE[0], update=_layer_changed)


class SHIRO_PG_cloth_island(PropertyGroup):
    island: IntProperty()
    vert_count: IntProperty()
    layer_uid: IntProperty(default=UNASSIGNED)


class _Poll:
    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH"


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SHIRO_OT_cloth_setup(_Poll, Operator):
    bl_idname = "shiro.cloth_setup"
    bl_label = "Set Up Cloth Layers"
    bl_description = ("Add the layer attribute, a first layer, the lookup table and a "
                      f"'{GROUP_NAME}' modifier that writes '{OUT_ATTR}' per vertex")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        previous = leave_edit_mode(context)
        get_layer_attribute(obj.data)
        if not obj.cloth_layers:
            bpy.ops.shiro.cloth_layer_add()
        mod = cloth_modifier(obj)
        if mod is None or gn_input_identifier(mod, "Layer Table") is None:
            mod = obj.modifiers.new(GROUP_NAME, "NODES")
            mod.node_group = build_setup_group()
            with context.temp_override(object=obj, active_object=obj):
                bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=0)
        sync_table(obj)
        update_counts(obj)
        restore_mode(previous)
        self.report({"INFO"}, f"Ready: read '{OUT_ATTR}' in your cloth setup")
        return {"FINISHED"}


class SHIRO_OT_cloth_layer_add(_Poll, Operator):
    bl_idname = "shiro.cloth_layer_add"
    bl_label = "Add Layer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        uid = max(obj.cloth_layer_uid_next, UNASSIGNED + 1)
        layer = obj.cloth_layers.add()
        layer.uid = uid
        layer.name = f"Layer {len(obj.cloth_layers)}"
        layer.color = PALETTE[(len(obj.cloth_layers) - 1) % len(PALETTE)]
        obj.cloth_layer_uid_next = uid + 1
        obj.cloth_layers_index = len(obj.cloth_layers) - 1
        sync_table(obj)
        return {"FINISHED"}


class SHIRO_OT_cloth_layer_remove(_Poll, Operator):
    bl_idname = "shiro.cloth_layer_remove"
    bl_label = "Remove Layer"
    bl_description = "Delete the active layer and unassign its vertices"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            return {"CANCELLED"}
        uid = layer.uid
        previous = leave_edit_mode(context)
        values = read_layer_values(obj.data)
        if values is not None:
            values[values == uid] = UNASSIGNED
        obj.cloth_layers.remove(obj.cloth_layers_index)
        obj.cloth_layers_index = max(0, min(obj.cloth_layers_index, len(obj.cloth_layers) - 1))
        if values is not None:
            write_layer_values(obj, values)
        sync_table(obj)
        update_counts(obj)
        restore_mode(previous)
        return {"FINISHED"}


class SHIRO_OT_cloth_layer_move(_Poll, Operator):
    bl_idname = "shiro.cloth_layer_move"
    bl_label = "Move Layer"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")], default="UP")

    def execute(self, context):
        obj = context.object
        i = obj.cloth_layers_index
        j = i - 1 if self.direction == "UP" else i + 1
        if not (0 <= j < len(obj.cloth_layers)):
            return {"CANCELLED"}
        obj.cloth_layers.move(i, j)
        obj.cloth_layers_index = j
        return {"FINISHED"}


class SHIRO_OT_cloth_assign(_Poll, Operator):
    bl_idname = "shiro.cloth_assign"
    bl_label = "Assign"
    bl_description = "Put the selected vertices on the active layer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            self.report({"WARNING"}, "No active layer")
            return {"CANCELLED"}
        previous = leave_edit_mode(context)
        sel = read_selection(obj.data)
        if not sel.any():
            restore_mode(previous)
            self.report({"WARNING"}, "Nothing selected")
            return {"CANCELLED"}
        values = read_layer_values(obj.data)
        values[sel] = layer.uid
        write_layer_values(obj, values)
        update_counts(obj)
        restore_mode(previous)
        self.report({"INFO"}, f"{int(sel.sum())} vertices -> {layer.name}")
        return {"FINISHED"}


class SHIRO_OT_cloth_remove_selection(_Poll, Operator):
    bl_idname = "shiro.cloth_remove_selection"
    bl_label = "Remove"
    bl_description = "Unassign the selected vertices from the active layer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            return {"CANCELLED"}
        previous = leave_edit_mode(context)
        values = read_layer_values(obj.data)
        hit = read_selection(obj.data) & (values == layer.uid)
        values[hit] = UNASSIGNED
        write_layer_values(obj, values)
        update_counts(obj)
        restore_mode(previous)
        self.report({"INFO"}, f"{int(hit.sum())} vertices removed")
        return {"FINISHED"}


def _enter_vertex_edit(previous):
    if previous == "OBJECT":
        bpy.ops.object.mode_set(mode="EDIT")
    else:
        restore_mode(previous)
    bpy.ops.mesh.select_mode(type="VERT")


class SHIRO_OT_cloth_select_layer(_Poll, Operator):
    bl_idname = "shiro.cloth_select_layer"
    bl_label = "Select"
    bl_options = {"REGISTER", "UNDO"}

    deselect: BoolProperty(default=False)

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            return {"CANCELLED"}
        previous = leave_edit_mode(context)
        sel = read_selection(obj.data)
        sel[read_layer_values(obj.data) == layer.uid] = not self.deselect
        write_selection(obj.data, sel)
        _enter_vertex_edit(previous)
        return {"FINISHED"}


class SHIRO_OT_cloth_select_unassigned(_Poll, Operator):
    bl_idname = "shiro.cloth_select_unassigned"
    bl_label = "Select Unassigned"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        previous = leave_edit_mode(context)
        write_selection(obj.data, read_layer_values(obj.data) == UNASSIGNED)
        _enter_vertex_edit(previous)
        return {"FINISHED"}


class SHIRO_OT_cloth_refresh(_Poll, Operator):
    bl_idname = "shiro.cloth_refresh_counts"
    bl_label = "Refresh Counts"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        previous = leave_edit_mode(context)
        update_counts(context.object)
        restore_mode(previous)
        return {"FINISHED"}


class SHIRO_OT_cloth_sync(_Poll, Operator):
    bl_idname = "shiro.cloth_sync_table"
    bl_label = "Rebuild Table"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sync_table(context.object)
        return {"FINISHED"}


class SHIRO_OT_cloth_colors(_Poll, Operator):
    bl_idname = "shiro.cloth_show_colors"
    bl_label = "Layer Colours"
    bl_description = "Toggle showing each layer's colour on the mesh (Solid shading)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        mesh = obj.data
        previous = leave_edit_mode(context)
        attr = mesh.color_attributes.get(COLOR_ATTR)
        if attr is None:
            attr = mesh.color_attributes.new(COLOR_ATTR, "FLOAT_COLOR", "POINT")
        update_colors(obj)
        mesh.color_attributes.active_color = attr
        mesh.update()
        restore_mode(previous)
        turn_on = None
        for area in context.screen.areas if context.screen else ():
            if area.type != "VIEW_3D":
                continue
            shading = area.spaces.active.shading
            if turn_on is None:
                turn_on = shading.color_type != "VERTEX"
            shading.color_type = "VERTEX" if turn_on else "MATERIAL"
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# islands (vectorised)
# ---------------------------------------------------------------------------

def compute_islands(mesh):
    """Connected-component label per vertex: min-label propagation + pointer jumping."""
    n = len(mesh.vertices)
    if n == 0:
        return np.zeros(0, dtype=np.int32)
    raw = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", raw)
    a, b = raw[0::2], raw[1::2]
    lab = np.arange(n, dtype=np.int64)
    while True:
        prev = lab.copy()
        m = np.minimum(lab[a], lab[b])
        np.minimum.at(lab, a, m)
        np.minimum.at(lab, b, m)
        while True:  # pointer jumping collapses long chains in log steps
            nxt = lab[lab]
            if np.array_equal(nxt, lab):
                break
            lab = nxt
        if np.array_equal(lab, prev):
            break
    _, labels = np.unique(lab, return_inverse=True)
    return labels.astype(np.int32)


def get_islands(obj, rebuild=False):
    key = (len(obj.data.vertices), len(obj.data.edges))
    hit = _island_cache.get(obj.session_uid)
    if rebuild or hit is None or hit[0] != key:
        hit = (key, compute_islands(obj.data))
        _island_cache[obj.session_uid] = hit
    return hit[1]


def refresh_islands(obj, rebuild=True):
    labels = get_islands(obj, rebuild)
    values = read_layer_values(obj.data)
    if values is None:
        values = np.zeros(len(obj.data.vertices), dtype=np.int32)
    obj.cloth_islands.clear()
    if labels.size == 0:
        return 0
    count = int(labels.max()) + 1
    sizes = np.bincount(labels, minlength=count)
    vals = np.clip(values, 0, None)
    width = int(vals.max()) + 1
    votes = np.bincount(labels * width + vals, minlength=count * width).reshape(count, width)
    majority = votes.argmax(axis=1)
    for i in range(count):
        e = obj.cloth_islands.add()
        e.island, e.vert_count, e.layer_uid = i, int(sizes[i]), int(majority[i])
    obj.cloth_islands_index = max(0, min(obj.cloth_islands_index, count - 1))
    return count


class SHIRO_OT_cloth_islands(_Poll, Operator):
    bl_idname = "shiro.cloth_islands_refresh"
    bl_label = "Scan Islands"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        previous = leave_edit_mode(context)
        n = refresh_islands(context.object, True)
        restore_mode(previous)
        self.report({"INFO"}, f"{n} islands")
        return {"FINISHED"}


class SHIRO_OT_cloth_island_select(_Poll, Operator):
    bl_idname = "shiro.cloth_island_select"
    bl_label = "Select Island"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        i = obj.cloth_islands_index
        if not (0 <= i < len(obj.cloth_islands)):
            return {"CANCELLED"}
        leave_edit_mode(context)
        write_selection(obj.data, get_islands(obj) == obj.cloth_islands[i].island)
        _enter_vertex_edit("OBJECT")
        return {"FINISHED"}


class SHIRO_OT_cloth_purge(_Poll, Operator):
    bl_idname = "shiro.cloth_purge"
    bl_label = "Purge Stored Data"
    bl_options = {"REGISTER", "UNDO"}

    include_layers: BoolProperty(name="Also remove layers", default=False)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.object
        _island_cache.pop(obj.session_uid, None)
        obj.cloth_islands.clear()
        obj.cloth_islands_index = 0
        if self.include_layers:
            obj.cloth_layers.clear()
            obj.cloth_layers_index = 0
            obj.cloth_layer_uid_next = UNASSIGNED + 1
            sync_table(obj)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class SHIRO_UL_cloth_layers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        sw = row.row(align=True)
        sw.ui_units_x = 0.8
        sw.prop(item, "color", text="")
        row.prop(item, "name", text="", emboss=False)
        c = row.row(align=True)
        c.scale_x = 0.45
        c.active = False
        c.label(text=f"{item.vert_count} v")
        v = row.row(align=True)
        v.scale_x = 0.6
        v.prop(item, "distance", text="")
        r = row.row(align=True)
        r.scale_x = 0.5
        r.active = False
        r.label(text=f"{effective_distance(data, item):.4f}")


class SHIRO_UL_cloth_islands(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        row.label(text=f"Island {item.island}", icon="MESH_DATA")
        c = row.row(align=True)
        c.scale_x = 0.5
        c.label(text=f"{item.vert_count} v")
        layer = find_layer(data, item.layer_uid)
        if layer is None:
            t = row.row(align=True)
            t.active = False
            t.label(text="unassigned")
        else:
            sw = row.row(align=True)
            sw.ui_units_x = 0.8
            sw.prop(layer, "color", text="")
            row.label(text=layer.name)


class _ClothPanel(_Poll):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB


class SHIRO_PT_cloth(_ClothPanel, Panel):
    bl_idname = "SHIRO_PT_cloth"
    bl_label = "Cloth Layers"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        obj = context.object
        layout = self.layout
        mod = cloth_modifier(obj)
        if mod is None or gn_input_identifier(mod, "Layer Table") is None:
            box = layout.box()
            box.label(text="No cloth layer modifier", icon="INFO")
            big = box.row()
            big.scale_y = 1.4
            big.operator("shiro.cloth_setup", icon="MOD_CLOTH")
            return
        spacing, base = scale_settings(obj)
        col = layout.column(align=True)
        col.label(text=f"{mod.name}  ·  {mod.node_group.name}", icon="GEOMETRY_NODES")
        sub = col.row()
        sub.active = False
        sub.label(text=f"Spacing {spacing:.4f}   Base {base:.4f}   Unassigned "
                       f"{obj.cloth_unassigned} v")
        row = layout.row(align=True)
        row.operator("shiro.cloth_show_colors", icon="COLOR")
        row.operator("shiro.cloth_setup", text="", icon="FILE_REFRESH")


class SHIRO_PT_cloth_layers(_ClothPanel, Panel):
    bl_idname = "SHIRO_PT_cloth_layers"
    bl_parent_id = "SHIRO_PT_cloth"
    bl_label = "Layers"

    def draw(self, context):
        obj = context.object
        layout = self.layout
        row = layout.row()
        row.template_list("SHIRO_UL_cloth_layers", "", obj, "cloth_layers",
                          obj, "cloth_layers_index", rows=5)
        col = row.column(align=True)
        col.operator("shiro.cloth_layer_add", icon="ADD", text="")
        col.operator("shiro.cloth_layer_remove", icon="REMOVE", text="")
        col.separator()
        col.operator("shiro.cloth_layer_move", icon="TRIA_UP", text="").direction = "UP"
        col.operator("shiro.cloth_layer_move", icon="TRIA_DOWN", text="").direction = "DOWN"
        edit = obj.mode == "EDIT"
        block = layout.column(align=True)
        block.enabled = edit
        row = block.row(align=True)
        row.operator("shiro.cloth_assign")
        row.operator("shiro.cloth_remove_selection")
        row = block.row(align=True)
        row.operator("shiro.cloth_select_layer", text="Select").deselect = False
        row.operator("shiro.cloth_select_layer", text="Deselect").deselect = True
        if not edit:
            layout.label(text="Enter Edit Mode to assign", icon="INFO")
        layout.operator("shiro.cloth_select_unassigned", icon="RESTRICT_SELECT_OFF")
        row = layout.row(align=True)
        row.operator("shiro.cloth_refresh_counts", icon="FILE_REFRESH")
        row.operator("shiro.cloth_sync_table", icon="EXPORT")


class SHIRO_PT_cloth_islands(_ClothPanel, Panel):
    bl_idname = "SHIRO_PT_cloth_islands"
    bl_parent_id = "SHIRO_PT_cloth"
    bl_label = "Islands"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        obj = context.object
        layout = self.layout
        row = layout.row(align=True)
        row.operator("shiro.cloth_islands_refresh", icon="FILE_REFRESH")
        row.operator("shiro.cloth_purge", icon="TRASH", text="")
        layout.template_list("SHIRO_UL_cloth_islands", "", obj, "cloth_islands",
                             obj, "cloth_islands_index", rows=4)
        layout.operator("shiro.cloth_island_select", icon="RESTRICT_SELECT_OFF")


classes = (
    SHIRO_PG_cloth_layer, SHIRO_PG_cloth_island, SHIRO_OT_cloth_setup, SHIRO_OT_cloth_layer_add,
    SHIRO_OT_cloth_layer_remove, SHIRO_OT_cloth_layer_move, SHIRO_OT_cloth_assign,
    SHIRO_OT_cloth_remove_selection, SHIRO_OT_cloth_select_layer,
    SHIRO_OT_cloth_select_unassigned, SHIRO_OT_cloth_refresh, SHIRO_OT_cloth_sync,
    SHIRO_OT_cloth_colors, SHIRO_OT_cloth_islands, SHIRO_OT_cloth_island_select,
    SHIRO_OT_cloth_purge, SHIRO_UL_cloth_layers, SHIRO_UL_cloth_islands,
    SHIRO_PT_cloth, SHIRO_PT_cloth_layers, SHIRO_PT_cloth_islands,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    T = bpy.types.Object
    T.cloth_layers = CollectionProperty(type=SHIRO_PG_cloth_layer)
    T.cloth_layers_index = IntProperty(default=0)
    T.cloth_layer_uid_next = IntProperty(default=UNASSIGNED + 1)
    T.cloth_unassigned = IntProperty(default=0)
    T.cloth_islands = CollectionProperty(type=SHIRO_PG_cloth_island)
    T.cloth_islands_index = IntProperty(default=0)
    T.cloth_layer_table = PointerProperty(type=bpy.types.Object)


def unregister():
    _island_cache.clear()
    T = bpy.types.Object
    for name in ("cloth_layer_table", "cloth_islands_index", "cloth_islands", "cloth_unassigned",
                 "cloth_layer_uid_next", "cloth_layers_index", "cloth_layers"):
        delattr(T, name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
