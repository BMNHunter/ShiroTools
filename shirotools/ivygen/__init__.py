"""IvyGen 2.1.0 — procedural ivy that grows along edges and crawls over any surface.

Sidebar panel that adds the IvyGen geometry nodes modifier to objects, builds
leaf and bark materials from picked images, and edits every node group setting
per object. Operators and classes carry the shiro prefix so a standalone IvyGen
install can sit next to this one; the Object.ivygen property keeps the
standalone name so its .blend files load here.
"""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, PointerProperty,
                       StringProperty)
from bpy_extras.io_utils import ImportHelper

from ..common import TAB
from . import materials, nodes

EDGE_ATTR = "ivy_edges"

IMAGE_SLOTS = (
    ("leaf_color", "Leaf Color", "Leaf albedo; its alpha channel cuts the leaf out"),
    ("leaf_opacity", "Leaf Opacity", "Optional black/white mask, overrides the color's alpha"),
    ("leaf_normal", "Leaf Normal", "Optional tangent-space normal map"),
    ("bark_color", "Bark Color", "Bark albedo, tiled along the vine"),
    ("bark_normal", "Bark Normal", "Optional tangent-space normal map"),
)

# which edge source each Edges-panel setting belongs to (greyed out otherwise)
EDGE_SOCKET_MODE = {
    "Up Threshold": "Upward Rim",
    "Crease Angle": "Sharp Edges",
    "Convex Only": "Sharp Edges",
    "Edge Selection": "Selection",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def get_group():
    """The IvyGen node group, built or upgraded on demand."""
    ng = bpy.data.node_groups.get(nodes.GROUP_NAME)
    # groups from the old ShiroTools IvyGen only carry "shiro_ivy_version", so
    # they read as v1 here and get rebuilt
    if ng is None or ng.get("ivygen_version", 1) < nodes.GROUP_VERSION:
        ng = nodes.build_group()
    return ng


def find_modifier(obj):
    if obj is None or obj.type != "MESH":
        return None
    ng = bpy.data.node_groups.get(nodes.GROUP_NAME)
    if ng is None:
        return None
    for mod in obj.modifiers:
        if mod.type == "NODES" and mod.node_group == ng:
            return mod
    return None


def input_sockets(ng):
    for item in ng.interface.items_tree:
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT":
            yield item


def input_struct(mod, name):
    for item in input_sockets(mod.node_group):
        if item.name == name:
            return getattr(mod.properties.inputs, item.identifier, None)
    return None


def set_input(mod, name, value):
    struct = input_struct(mod, name)
    if struct is not None and hasattr(struct, "value"):
        struct.value = value


def get_input(mod, name):
    struct = input_struct(mod, name)
    if struct is not None and hasattr(struct, "value"):
        return struct.value
    return None


def refresh_materials(obj):
    """Rebuild this object's leaf and bark materials and plug them in."""
    mod = find_modifier(obj)
    if mod is None:
        return
    leaf = materials.build_leaf(obj)
    bark = materials.build_bark(obj, nodes.UV_NAME)
    set_input(mod, "Leaf Material", leaf)
    set_input(mod, "Bark Material", bark)
    obj.update_tag()


def copy_inputs(src_mod, dst_mod):
    for item in input_sockets(src_mod.node_group):
        if item.name in ("Leaf Material", "Bark Material"):
            continue   # every object keeps its own generated materials
        src = getattr(src_mod.properties.inputs, item.identifier, None)
        dst = getattr(dst_mod.properties.inputs, item.identifier, None)
        if src is None or dst is None:
            continue
        for field in ("type", "value", "attribute_name"):
            if hasattr(src, field) and hasattr(dst, field):
                try:
                    setattr(dst, field, getattr(src, field))
                except (AttributeError, TypeError, ValueError):
                    pass


def _on_texture_change(self, context):
    refresh_materials(self.id_data)


# ---------------------------------------------------------------------------
# per-object settings
# ---------------------------------------------------------------------------

def _image_prop(name, desc):
    return PointerProperty(type=bpy.types.Image, name=name, description=desc,
                           update=_on_texture_change)


class SHIRO_PG_ivy_settings(bpy.types.PropertyGroup):
    leaf_color: _image_prop(*IMAGE_SLOTS[0][1:])
    leaf_opacity: _image_prop(*IMAGE_SLOTS[1][1:])
    leaf_normal: _image_prop(*IMAGE_SLOTS[2][1:])
    bark_color: _image_prop(*IMAGE_SLOTS[3][1:])
    bark_normal: _image_prop(*IMAGE_SLOTS[4][1:])
    leaf_translucency: FloatProperty(
        name="Translucency", default=0.3, min=0.0, max=1.0, subtype="FACTOR",
        description="How much light passes through the leaves",
        update=_on_texture_change)
    leaf_material: PointerProperty(type=bpy.types.Material, name="Leaf Material")
    bark_material: PointerProperty(type=bpy.types.Material, name="Bark Material")


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SHIRO_OT_ivy_add(bpy.types.Operator):
    bl_idname = "shiro.ivy_add"
    bl_label = "Add IvyGen"
    bl_description = "Add the IvyGen modifier to the selected mesh objects"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects) or (
            context.object is not None and context.object.type == "MESH")

    def execute(self, context):
        ng = get_group()
        targets = {o for o in context.selected_objects if o.type == "MESH"}
        if context.object is not None and context.object.type == "MESH":
            targets.add(context.object)
        for obj in targets:
            nodes.attach_to(obj, ng)
            refresh_materials(obj)
        self.report({"INFO"}, "IvyGen added to %d object(s)" % len(targets))
        return {"FINISHED"}


class SHIRO_OT_ivy_load_image(bpy.types.Operator, ImportHelper):
    bl_idname = "shiro.ivy_load_image"
    bl_label = "Load Image"
    bl_description = "Load an image from disk into this texture slot"
    bl_options = {"REGISTER", "UNDO"}

    filter_image: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.tga;*.exr;*.webp;*.bmp",
        options={"HIDDEN"})
    slot: EnumProperty(items=[(s[0], s[1], s[2]) for s in IMAGE_SLOTS],
                       options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return find_modifier(context.object) is not None

    def execute(self, context):
        try:
            img = bpy.data.images.load(self.filepath, check_existing=True)
        except RuntimeError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        setattr(context.object.ivygen, self.slot, img)
        return {"FINISHED"}


class SHIRO_OT_ivy_clear_image(bpy.types.Operator):
    bl_idname = "shiro.ivy_clear_image"
    bl_label = "Clear Image"
    bl_description = "Empty this texture slot"
    bl_options = {"REGISTER", "UNDO"}

    slot: EnumProperty(items=[(s[0], s[1], s[2]) for s in IMAGE_SLOTS],
                       options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        setattr(context.object.ivygen, self.slot, None)
        return {"FINISHED"}


class SHIRO_OT_ivy_edges_from_selection(bpy.types.Operator):
    bl_idname = "shiro.ivy_edges_from_selection"
    bl_label = "Grow From Selected Edges"
    bl_description = ("Store the edges selected in Edit Mode and switch Edge Source "
                      "to Selection")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (find_modifier(obj) is not None and obj.mode == "EDIT")

    def execute(self, context):
        obj = context.object
        bpy.ops.object.mode_set(mode="OBJECT")
        me = obj.data
        attr = me.attributes.get(EDGE_ATTR)
        if attr is not None and (attr.domain != "EDGE" or attr.data_type != "BOOLEAN"):
            me.attributes.remove(attr)
            attr = None
        if attr is None:
            attr = me.attributes.new(EDGE_ATTR, "BOOLEAN", "EDGE")
        picked = [e.select for e in me.edges]
        attr.data.foreach_set("value", picked)
        bpy.ops.object.mode_set(mode="EDIT")

        mod = find_modifier(obj)
        set_input(mod, "Edge Source", "Selection")
        struct = input_struct(mod, "Edge Selection")
        struct.type = "ATTRIBUTE"
        struct.attribute_name = EDGE_ATTR
        obj.update_tag()
        self.report({"INFO"}, "%d edges marked for ivy" % sum(picked))
        return {"FINISHED"}


class SHIRO_OT_ivy_copy_to_selected(bpy.types.Operator):
    bl_idname = "shiro.ivy_copy_to_selected"
    bl_label = "Copy to Selected"
    bl_description = ("Copy the active object's IvyGen settings and textures to the "
                      "other selected meshes, adding the modifier where missing")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (find_modifier(context.object) is not None
                and len(context.selected_objects) > 1)

    def execute(self, context):
        src = context.object
        src_mod = find_modifier(src)
        count = 0
        for obj in context.selected_objects:
            if obj is src or obj.type != "MESH":
                continue
            dst_mod = nodes.attach_to(obj, src_mod.node_group)
            copy_inputs(src_mod, dst_mod)
            # attribute access: since 5.0 obj.ivygen[slot] makes a stray ID prop
            # instead of setting the slot. Each set rebuilds the (tiny) materials.
            for slot, _label, _desc in IMAGE_SLOTS:
                setattr(obj.ivygen, slot, getattr(src.ivygen, slot))
            obj.ivygen.leaf_translucency = src.ivygen.leaf_translucency
            refresh_materials(obj)   # a fresh modifier needs its materials plugged in
            count += 1
        self.report({"INFO"}, "Copied to %d object(s)" % count)
        return {"FINISHED"}


class SHIRO_OT_ivy_reset(bpy.types.Operator):
    bl_idname = "shiro.ivy_reset"
    bl_label = "Reset Settings"
    bl_description = "Put this object's IvyGen settings back to the defaults"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return find_modifier(context.object) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = context.object
        mod = find_modifier(obj)
        for item in input_sockets(mod.node_group):
            struct = getattr(mod.properties.inputs, item.identifier, None)
            if struct is None or not hasattr(struct, "value"):
                continue
            if hasattr(struct, "type"):
                struct.type = "VALUE"
            default = getattr(item, "default_value", None)
            if default is not None:
                try:
                    struct.value = default
                except (TypeError, ValueError):
                    pass
        refresh_materials(obj)
        return {"FINISHED"}


class SHIRO_OT_ivy_rebuild(bpy.types.Operator):
    bl_idname = "shiro.ivy_rebuild"
    bl_label = "Rebuild Node Group"
    bl_description = ("Regenerate the IvyGen node group from the add-on. Every "
                      "object keeps its settings")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        nodes.build_group()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def draw_image_row(layout, settings, slot, label):
    row = layout.row(align=True)
    row.prop(settings, slot, text=label)
    op = row.operator(SHIRO_OT_ivy_load_image.bl_idname, text="", icon="FILEBROWSER")
    op.slot = slot
    if getattr(settings, slot) is not None:
        op = row.operator(SHIRO_OT_ivy_clear_image.bl_idname, text="", icon="X")
        op.slot = slot


def draw_socket(layout, mod, item, edge_source):
    struct = getattr(mod.properties.inputs, item.identifier, None)
    if struct is None or not hasattr(struct, "value"):
        return   # geometry sockets have nothing to set
    row = layout.row()
    wanted = EDGE_SOCKET_MODE.get(item.name)
    if wanted is not None:
        row.active = edge_source == wanted
    if item.name == "Edge Selection":
        col = row.column()
        if struct.type == "ATTRIBUTE":
            col.prop(struct, "attribute_name", text="Edge Attribute", icon="GROUP_VERTEX")
        else:
            col.prop(struct, "value", text=item.name)
        col.prop(struct, "type", text="Input")
        col.operator(SHIRO_OT_ivy_edges_from_selection.bl_idname, icon="EDGESEL")
        return
    row.prop(struct, "value", text=item.name)


def draw_inputs(layout, mod):
    ng = mod.node_group
    edge_source = get_input(mod, "Edge Source")
    for item in ng.interface.items_tree:
        if getattr(item, "item_type", "") != "PANEL":
            continue
        header, body = layout.panel("ivygen_" + item.name.lower().replace(" ", "_"),
                                    default_closed=item.name != "Growth")
        header.label(text=item.name)
        if body is None:
            continue
        body.use_property_split = True
        body.use_property_decorate = False
        for child in item.interface_items:
            if getattr(child, "item_type", "") == "SOCKET":
                draw_socket(body, mod, child, edge_source)


class SHIRO_PT_ivy(bpy.types.Panel):
    bl_idname = "SHIRO_PT_ivy"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_label = "IvyGen"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh object", icon="INFO")
            return
        mod = find_modifier(obj)
        if mod is None:
            row = layout.row()
            row.scale_y = 1.3
            row.operator(SHIRO_OT_ivy_add.bl_idname, icon="OUTLINER_OB_CURVES")
            return

        row = layout.row(align=True)
        row.label(text=obj.name, icon="OBJECT_DATA")
        row.prop(mod, "show_viewport", text="")
        row.prop(mod, "show_render", text="")

        s = obj.ivygen
        header, body = layout.panel("ivygen_textures", default_closed=False)
        header.label(text="Textures", icon="TEXTURE")
        if body is not None:
            col = body.column(align=True)
            for slot, label, _desc in IMAGE_SLOTS[:3]:
                draw_image_row(col, s, slot, label)
            body.prop(s, "leaf_translucency")
            col = body.column(align=True)
            for slot, label, _desc in IMAGE_SLOTS[3:]:
                draw_image_row(col, s, slot, label)

        draw_inputs(layout, mod)

        col = layout.column(align=True)
        col.operator(SHIRO_OT_ivy_copy_to_selected.bl_idname, icon="PASTEDOWN")
        row = col.row(align=True)
        row.operator(SHIRO_OT_ivy_reset.bl_idname, icon="LOOP_BACK")
        row.operator(SHIRO_OT_ivy_rebuild.bl_idname, text="Rebuild", icon="FILE_REFRESH")


# ---------------------------------------------------------------------------

classes = (
    SHIRO_PG_ivy_settings,
    SHIRO_OT_ivy_add,
    SHIRO_OT_ivy_load_image,
    SHIRO_OT_ivy_clear_image,
    SHIRO_OT_ivy_edges_from_selection,
    SHIRO_OT_ivy_copy_to_selected,
    SHIRO_OT_ivy_reset,
    SHIRO_OT_ivy_rebuild,
    SHIRO_PT_ivy,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.ivygen = PointerProperty(type=SHIRO_PG_ivy_settings)


def unregister():
    del bpy.types.Object.ivygen
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
