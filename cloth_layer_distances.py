bl_info = {
    "name": "Cloth Layer Distances",
    "author": "Milan",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Cloth Layers",
    "description": "Assign vertex selections to collision-distance layers for the GeoNodes cloth setup",
    "category": "Physics",
}

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


ATTR_NAME = "cloth_layer"       # INT, POINT domain, on the cloth mesh
TABLE_ATTR = "distance"         # FLOAT, POINT domain, on the table mesh
TABLE_PREFIX = "CLOTH_TABLE_"
UNASSIGNED = 0                  # uid 0 is reserved and always resolves to distance 0

_island_cache = {}


# ---------------------------------------------------------------------------
# mode handling
# ---------------------------------------------------------------------------

def leave_edit_mode(context):
    """Selection flags and attribute data only reflect reality in object mode."""
    obj = context.object
    previous = obj.mode
    if previous != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    return previous


def restore_mode(mode):
    if mode == 'OBJECT':
        return
    try:
        bpy.ops.object.mode_set(mode=mode)
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# the layer attribute
# ---------------------------------------------------------------------------

def get_layer_attribute(mesh, create=True):
    attr = mesh.attributes.get(ATTR_NAME)
    if attr is not None:
        if attr.domain == 'POINT' and attr.data_type == 'INT':
            return attr
        return None
    if not create:
        return None
    return mesh.attributes.new(ATTR_NAME, 'INT', 'POINT')


def read_layer_values(mesh):
    attr = get_layer_attribute(mesh)
    if attr is None:
        return None
    values = np.zeros(len(mesh.vertices), dtype=np.int32)
    attr.data.foreach_get("value", values)
    return values


def write_layer_values(mesh, values):
    attr = get_layer_attribute(mesh)
    if attr is None:
        return False
    attr.data.foreach_set("value", values.astype(np.int32))
    mesh.update()
    return True


def read_selection(mesh):
    flags = np.zeros(len(mesh.vertices), dtype=bool)
    mesh.vertices.foreach_get("select", flags)
    return flags


def write_selection(mesh, flags):
    mesh.vertices.foreach_set("select", flags)
    mesh.update()


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------

def find_layer(obj, uid):
    for layer in obj.cloth_layers:
        if layer.uid == uid:
            return layer
    return None


def active_layer(obj):
    index = obj.cloth_layers_index
    if index < 0 or index >= len(obj.cloth_layers):
        return None
    return obj.cloth_layers[index]


def update_counts(obj):
    """Refresh the per-layer vertex counts shown in the list."""
    values = read_layer_values(obj.data)
    if values is None:
        return
    counts = np.bincount(np.clip(values, 0, None))
    for layer in obj.cloth_layers:
        if layer.uid < counts.size:
            layer.vert_count = int(counts[layer.uid])
        else:
            layer.vert_count = 0
    obj.cloth_unassigned = int(counts[UNASSIGNED])


# ---------------------------------------------------------------------------
# modifier interrogation, failure-tolerant so draw() never dies
# ---------------------------------------------------------------------------

def geonodes_modifier(obj):
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        if getattr(mod, "node_group", None) is None:
            continue
        return mod
    return None


def modifier_socket(mod, name):
    if mod is None:
        return None, None
    group = getattr(mod, "node_group", None)
    if group is None:
        return None, None
    try:
        items = group.interface.items_tree
    except Exception:
        return None, None
    for item in items:
        if getattr(item, "item_type", "") != 'SOCKET':
            continue
        if getattr(item, "in_out", "") != 'INPUT':
            continue
        if getattr(item, "name", "") != name:
            continue
        identifier = getattr(item, "identifier", None)
        if identifier is None:
            return None, None
        try:
            return identifier, mod[identifier]
        except (KeyError, TypeError):
            return identifier, None
    return None, None


def scale_settings(obj):
    spacing = 1.0
    base = 0.0
    try:
        mod = geonodes_modifier(obj)
        found_spacing = modifier_socket(mod, "Spacing")[1]
        found_base = modifier_socket(mod, "Base")[1]
        if isinstance(found_spacing, (int, float)):
            spacing = float(found_spacing)
        if isinstance(found_base, (int, float)):
            base = float(found_base)
    except Exception as err:
        print("[cloth layers] could not read modifier inputs:", err)
    return spacing, base


def effective_distance(obj, layer):
    spacing, base = scale_settings(obj)
    return base + layer.distance * spacing


# ---------------------------------------------------------------------------
# lookup table object
# ---------------------------------------------------------------------------

def table_name(obj):
    return TABLE_PREFIX + obj.name


def ensure_table(obj):
    name = table_name(obj)
    table = bpy.data.objects.get(name)
    if table is not None:
        return table

    mesh = bpy.data.meshes.new(name)
    table = bpy.data.objects.new(name, mesh)

    linked = False
    for coll in obj.users_collection:
        coll.objects.link(table)
        linked = True
        break
    if not linked:
        bpy.context.scene.collection.objects.link(table)

    table.hide_render = True
    table.hide_select = True
    table.display_type = 'WIRE'
    try:
        table.hide_set(True)
    except RuntimeError:
        pass
    return table


def sync_table(obj):
    """Rebuild the table mesh so that point index equals layer uid."""
    if obj is None or obj.type != 'MESH':
        return

    table = ensure_table(obj)
    mesh = table.data

    highest = UNASSIGNED
    for layer in obj.cloth_layers:
        if layer.uid > highest:
            highest = layer.uid
    size = highest + 1

    mesh.clear_geometry()
    mesh.vertices.add(size)

    distances = np.zeros(size, dtype=np.float32)
    for layer in obj.cloth_layers:
        distances[layer.uid] = layer.distance

    coords = np.zeros(size * 3, dtype=np.float32)
    coords[0::3] = np.arange(size, dtype=np.float32)
    coords[2::3] = distances
    mesh.vertices.foreach_set("co", coords)

    attr = mesh.attributes.get(TABLE_ATTR)
    if attr is None:
        attr = mesh.attributes.new(TABLE_ATTR, 'FLOAT', 'POINT')
    attr.data.foreach_set("value", distances)
    mesh.update()

    mod = geonodes_modifier(obj)
    identifier = modifier_socket(mod, "Layer Table")[0]
    if identifier is not None:
        try:
            mod[identifier] = table
        except (KeyError, TypeError):
            pass


def distance_changed(self, context):
    obj = context.object
    if obj is not None and obj.type == 'MESH':
        sync_table(obj)


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class ClothLayerItem(PropertyGroup):
    name: StringProperty(name="Name", default="Layer")
    distance: FloatProperty(
        name="Distance",
        description="Collision distance for this layer, before Spacing and Base",
        default=0.0,
        precision=4,
        step=1,
        update=distance_changed,
    )
    uid: IntProperty()
    vert_count: IntProperty()


class ClothIslandItem(PropertyGroup):
    island: IntProperty()
    vert_count: IntProperty()
    layer_uid: IntProperty(default=UNASSIGNED)


def mesh_active(context):
    obj = context.object
    if obj is None:
        return False
    return obj.type == 'MESH'


class ClothLayerPoll:
    @classmethod
    def poll(cls, context):
        return mesh_active(context)


# ---------------------------------------------------------------------------
# layer list operators
# ---------------------------------------------------------------------------

class CLOTH_OT_layer_add(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.layer_add"
    bl_label = "Add Layer"
    bl_description = "Create a new collision distance layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        uid = obj.cloth_layer_uid_next
        if uid <= UNASSIGNED:
            uid = UNASSIGNED + 1

        layer = obj.cloth_layers.add()
        layer.uid = uid
        layer.name = "Layer %d" % len(obj.cloth_layers)
        layer.distance = 0.0

        obj.cloth_layer_uid_next = uid + 1
        obj.cloth_layers_index = len(obj.cloth_layers) - 1
        sync_table(obj)
        return {'FINISHED'}


class CLOTH_OT_layer_remove(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.layer_remove"
    bl_label = "Remove Layer"
    bl_description = "Delete the active layer and unassign anything using it"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            self.report({'WARNING'}, "No active layer")
            return {'CANCELLED'}

        uid = layer.uid
        previous = leave_edit_mode(context)

        values = read_layer_values(obj.data)
        if values is not None:
            values[values == uid] = UNASSIGNED
            write_layer_values(obj.data, values)

        obj.cloth_layers.remove(obj.cloth_layers_index)
        obj.cloth_layers_index = max(0, min(obj.cloth_layers_index, len(obj.cloth_layers) - 1))
        sync_table(obj)
        update_counts(obj)

        restore_mode(previous)
        return {'FINISHED'}


class CLOTH_OT_layer_move(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.layer_move"
    bl_label = "Move Layer"
    bl_description = "Reorder the layer list. Assignments are unaffected"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
        default='UP',
    )

    def execute(self, context):
        obj = context.object
        index = obj.cloth_layers_index
        if self.direction == 'UP':
            target = index - 1
        else:
            target = index + 1

        if target < 0 or target >= len(obj.cloth_layers):
            return {'CANCELLED'}

        obj.cloth_layers.move(index, target)
        obj.cloth_layers_index = target
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# selection based assignment, the vertex group workflow
# ---------------------------------------------------------------------------

class CLOTH_OT_assign(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.assign"
    bl_label = "Assign"
    bl_description = "Put the selected vertices on the active layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            self.report({'WARNING'}, "No active layer")
            return {'CANCELLED'}

        previous = leave_edit_mode(context)
        selection = read_selection(obj.data)

        if not selection.any():
            restore_mode(previous)
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}

        values = read_layer_values(obj.data)
        values[selection] = layer.uid
        write_layer_values(obj.data, values)
        update_counts(obj)

        restore_mode(previous)
        self.report({'INFO'}, "%d vertices assigned to %s" % (int(selection.sum()), layer.name))
        return {'FINISHED'}


class CLOTH_OT_remove_selection(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.remove_selection"
    bl_label = "Remove"
    bl_description = "Unassign the selected vertices from the active layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            return {'CANCELLED'}

        previous = leave_edit_mode(context)
        selection = read_selection(obj.data)
        values = read_layer_values(obj.data)

        targets = np.logical_and(selection, values == layer.uid)
        values[targets] = UNASSIGNED
        write_layer_values(obj.data, values)
        update_counts(obj)

        restore_mode(previous)
        self.report({'INFO'}, "%d vertices removed" % int(targets.sum()))
        return {'FINISHED'}


class CLOTH_OT_select_layer(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.select_layer"
    bl_label = "Select"
    bl_description = "Add the active layer's vertices to the selection"
    bl_options = {'REGISTER', 'UNDO'}

    deselect: BoolProperty(default=False)

    def execute(self, context):
        obj = context.object
        layer = active_layer(obj)
        if layer is None:
            return {'CANCELLED'}

        previous = leave_edit_mode(context)
        values = read_layer_values(obj.data)
        selection = read_selection(obj.data)

        members = values == layer.uid
        if self.deselect:
            selection[members] = False
        else:
            selection[members] = True
        write_selection(obj.data, selection)

        if previous == 'OBJECT':
            bpy.ops.object.mode_set(mode='EDIT')
        else:
            restore_mode(previous)
        bpy.ops.mesh.select_mode(type='VERT')
        return {'FINISHED'}


class CLOTH_OT_select_unassigned(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.select_unassigned"
    bl_label = "Select Unassigned"
    bl_description = "Select every vertex that sits on no layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        previous = leave_edit_mode(context)

        values = read_layer_values(obj.data)
        selection = values == UNASSIGNED
        write_selection(obj.data, selection)

        if previous == 'OBJECT':
            bpy.ops.object.mode_set(mode='EDIT')
        else:
            restore_mode(previous)
        bpy.ops.mesh.select_mode(type='VERT')
        return {'FINISHED'}


class CLOTH_OT_refresh_counts(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.refresh_counts"
    bl_label = "Refresh Counts"
    bl_description = "Recount how many vertices sit on each layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        previous = leave_edit_mode(context)
        update_counts(obj)
        restore_mode(previous)
        return {'FINISHED'}


class CLOTH_OT_sync_table(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.sync_table"
    bl_label = "Rebuild Table"
    bl_description = "Rebuild the lookup table object that the node tree samples"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sync_table(context.object)
        self.report({'INFO'}, "Table rebuilt")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# islands, now an optional helper for multi piece garments
# ---------------------------------------------------------------------------

def compute_islands(mesh):
    count = len(mesh.vertices)
    if count == 0:
        return np.zeros(0, dtype=np.int32)

    raw = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", raw)
    edges = raw.reshape(-1, 2)

    parent = list(range(count))

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            nxt = parent[node]
            parent[node] = root
            node = nxt
        return root

    for a, b in edges:
        root_a = find(int(a))
        root_b = find(int(b))
        if root_a != root_b:
            parent[root_b] = root_a

    roots = np.array([find(i) for i in range(count)], dtype=np.int32)
    _, labels = np.unique(roots, return_inverse=True)
    return labels.astype(np.int32)


def get_islands(obj, rebuild=False):
    key = (obj.name, len(obj.data.vertices), len(obj.data.edges))
    cached = _island_cache.get(obj.name)
    if rebuild or cached is None or cached[0] != key:
        labels = compute_islands(obj.data)
        _island_cache[obj.name] = (key, labels)
        return labels
    return cached[1]


def refresh_islands(obj, rebuild=True):
    mesh = obj.data
    labels = get_islands(obj, rebuild=rebuild)
    values = read_layer_values(mesh)
    if values is None:
        values = np.zeros(len(mesh.vertices), dtype=np.int32)

    obj.cloth_islands.clear()
    if labels.size == 0:
        obj.cloth_islands_index = 0
        return 0

    total = int(labels.max()) + 1
    for island_id in range(total):
        members = np.nonzero(labels == island_id)[0]
        if members.size == 0:
            continue
        entry = obj.cloth_islands.add()
        entry.island = island_id
        entry.vert_count = int(members.size)
        assigned = np.clip(values[members], 0, None)
        entry.layer_uid = int(np.argmax(np.bincount(assigned)))

    obj.cloth_islands_index = max(0, min(obj.cloth_islands_index, len(obj.cloth_islands) - 1))
    return len(obj.cloth_islands)


class CLOTH_OT_islands_refresh(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.islands_refresh"
    bl_label = "Scan Islands"
    bl_description = "Find the loose parts of this mesh so whole pieces can be selected at once"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        previous = leave_edit_mode(context)
        total = refresh_islands(obj, rebuild=True)
        restore_mode(previous)
        self.report({'INFO'}, "Found %d islands" % total)
        return {'FINISHED'}


class CLOTH_OT_island_select(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.island_select"
    bl_label = "Select Island"
    bl_description = "Select the active island, ready to assign"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        index = obj.cloth_islands_index
        if index < 0 or index >= len(obj.cloth_islands):
            return {'CANCELLED'}

        entry = obj.cloth_islands[index]
        leave_edit_mode(context)

        labels = get_islands(obj)
        selection = labels == entry.island
        write_selection(obj.data, selection)

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='VERT')
        return {'FINISHED'}


class CLOTH_OT_purge(ClothLayerPoll, Operator):
    bl_idname = "cloth_layers.purge"
    bl_label = "Purge Stored Data"
    bl_description = "Clear the cached island scan stored on this object"
    bl_options = {'REGISTER', 'UNDO'}

    include_layers: BoolProperty(name="Also remove layers", default=False)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.object
        _island_cache.pop(obj.name, None)
        obj.cloth_islands.clear()
        obj.cloth_islands_index = 0

        if self.include_layers:
            obj.cloth_layers.clear()
            obj.cloth_layers_index = 0
            obj.cloth_layer_uid_next = UNASSIGNED + 1
            sync_table(obj)

        self.report({'INFO'}, "Stored data cleared")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI lists
# ---------------------------------------------------------------------------

class CLOTH_UL_layers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = context.object
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='MOD_CLOTH')

        count = row.row(align=True)
        count.scale_x = 0.45
        count.active = False
        count.label(text="%d v" % item.vert_count)

        value = row.row(align=True)
        value.scale_x = 0.6
        value.prop(item, "distance", text="")

        result = row.row(align=True)
        result.scale_x = 0.5
        result.active = False
        result.label(text="%.4f" % effective_distance(obj, item))


class CLOTH_UL_islands(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = context.object
        row = layout.row(align=True)
        row.label(text="Island %d" % item.island, icon='MESH_DATA')

        count = row.row(align=True)
        count.scale_x = 0.5
        count.label(text="%d v" % item.vert_count)

        layer = find_layer(obj, item.layer_uid)
        if layer is None:
            tail = row.row(align=True)
            tail.active = False
            tail.label(text="unassigned")
            return
        row.label(text=layer.name)


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------

class ClothLayerPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Cloth Layers"

    @classmethod
    def poll(cls, context):
        return mesh_active(context)


class CLOTH_PT_main(ClothLayerPanel, Panel):
    bl_idname = "CLOTH_PT_main"
    bl_label = "Cloth Layers"

    def draw(self, context):
        obj = context.object
        layout = self.layout

        mod = geonodes_modifier(obj)
        if mod is None:
            layout.label(text="No GeoNodes modifier found", icon='ERROR')
            return

        spacing, base = scale_settings(obj)
        col = layout.column(align=True)
        col.active = False
        col.label(text="Spacing  %.4f      Base  %.4f" % (spacing, base))
        col.label(text="Unassigned  %d v" % obj.cloth_unassigned)


class CLOTH_PT_layers(ClothLayerPanel, Panel):
    bl_idname = "CLOTH_PT_layers"
    bl_parent_id = "CLOTH_PT_main"
    bl_label = "Layers"

    def draw(self, context):
        obj = context.object
        layout = self.layout

        row = layout.row()
        row.template_list("CLOTH_UL_layers", "", obj, "cloth_layers",
                          obj, "cloth_layers_index", rows=5)

        col = row.column(align=True)
        col.operator("cloth_layers.layer_add", icon='ADD', text="")
        col.operator("cloth_layers.layer_remove", icon='REMOVE', text="")
        col.separator()
        col.operator("cloth_layers.layer_move", icon='TRIA_UP', text="").direction = 'UP'
        col.operator("cloth_layers.layer_move", icon='TRIA_DOWN', text="").direction = 'DOWN'

        edit_mode = obj.mode == 'EDIT'

        block = layout.column(align=True)
        block.enabled = edit_mode
        row = block.row(align=True)
        row.operator("cloth_layers.assign", text="Assign")
        row.operator("cloth_layers.remove_selection", text="Remove")

        row = block.row(align=True)
        row.operator("cloth_layers.select_layer", text="Select").deselect = False
        row.operator("cloth_layers.select_layer", text="Deselect").deselect = True

        if not edit_mode:
            layout.label(text="Enter edit mode to assign", icon='INFO')

        layout.operator("cloth_layers.select_unassigned", icon='RESTRICT_SELECT_OFF')

        row = layout.row(align=True)
        row.operator("cloth_layers.refresh_counts", icon='FILE_REFRESH')
        row.operator("cloth_layers.sync_table", icon='EXPORT')


class CLOTH_PT_islands(ClothLayerPanel, Panel):
    bl_idname = "CLOTH_PT_islands"
    bl_parent_id = "CLOTH_PT_main"
    bl_label = "Islands"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        obj = context.object
        layout = self.layout

        layout.label(text="Shortcut for multi piece garments", icon='INFO')

        row = layout.row(align=True)
        row.operator("cloth_layers.islands_refresh", icon='FILE_REFRESH')
        row.operator("cloth_layers.purge", icon='TRASH', text="")

        layout.template_list("CLOTH_UL_islands", "", obj, "cloth_islands",
                             obj, "cloth_islands_index", rows=4)
        layout.operator("cloth_layers.island_select", icon='RESTRICT_SELECT_OFF')


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

classes = (
    ClothLayerItem,
    ClothIslandItem,
    CLOTH_OT_layer_add,
    CLOTH_OT_layer_remove,
    CLOTH_OT_layer_move,
    CLOTH_OT_assign,
    CLOTH_OT_remove_selection,
    CLOTH_OT_select_layer,
    CLOTH_OT_select_unassigned,
    CLOTH_OT_refresh_counts,
    CLOTH_OT_sync_table,
    CLOTH_OT_islands_refresh,
    CLOTH_OT_island_select,
    CLOTH_OT_purge,
    CLOTH_UL_layers,
    CLOTH_UL_islands,
    CLOTH_PT_main,
    CLOTH_PT_layers,
    CLOTH_PT_islands,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.cloth_layers = CollectionProperty(type=ClothLayerItem)
    bpy.types.Object.cloth_layers_index = IntProperty(default=0)
    bpy.types.Object.cloth_layer_uid_next = IntProperty(default=UNASSIGNED + 1)
    bpy.types.Object.cloth_unassigned = IntProperty(default=0)
    bpy.types.Object.cloth_islands = CollectionProperty(type=ClothIslandItem)
    bpy.types.Object.cloth_islands_index = IntProperty(default=0)


def unregister():
    _island_cache.clear()

    del bpy.types.Object.cloth_islands_index
    del bpy.types.Object.cloth_islands
    del bpy.types.Object.cloth_unassigned
    del bpy.types.Object.cloth_layer_uid_next
    del bpy.types.Object.cloth_layers_index
    del bpy.types.Object.cloth_layers

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()