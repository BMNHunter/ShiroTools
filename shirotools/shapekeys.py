"""Shape Keys: bake shape keys to attributes (and back), GN modifier to shape key,
and bake the evaluated mesh (cloth / GN / any modifier) into per-frame shape keys.

Replaces Bake Shape Key to Attribute and Geometry Nodes Shape Key Tools.
All per-vertex copies use foreach_get / foreach_set (no Python loops).
"""

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .common import TAB, object_mode

RESERVED = {"position", "radius", "id", "material_index", "sharp_face", "sharp_edge",
            "crease_vert", "crease_edge", "uv_seam", "velocity", "normal"}


def attribute_name_problem(name):
    if not name:
        return "Attribute name is empty"
    if name.startswith("."):
        return "Names starting with '.' are internal"
    if name.lower() in RESERVED:
        return f"'{name}' is a built-in attribute, writing it would change the mesh itself"
    return None


def shape_coords(key_block):
    n = len(key_block.data)
    co = np.empty(n * 3, dtype=np.float32)
    key_block.data.foreach_get("co", co)
    return co


def ensure_vector_attribute(mesh, name, overwrite):
    attr = mesh.attributes.get(name)
    if attr is not None:
        if not overwrite:
            return None, f"'{name}' exists (enable Overwrite)"
        if attr.domain != "POINT" or attr.data_type != "FLOAT_VECTOR":
            mesh.attributes.remove(attr)
            attr = None
    if attr is None:
        attr = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    return attr, None


class SHIRO_PG_shapekeys(PropertyGroup):
    attribute_name: StringProperty(name="Attribute", default="fixed_pos")
    mode: EnumProperty(name="Mode", items=[
        ("ABSOLUTE", "Absolute Position", "Shape key coordinates (for GN Set Position)"),
        ("RELATIVE", "Offset", "Offset from the key's Relative To shape"),
    ], default="ABSOLUTE")
    overwrite: BoolProperty(name="Overwrite", default=True)
    all_keys: BoolProperty(name="All Keys", default=False,
                           description="Bake every shape key to '<attribute>_<key name>'")
    frame_start: IntProperty(name="Start", default=1)
    frame_end: IntProperty(name="End", default=250)
    frame_step: IntProperty(name="Step", default=1, min=1)
    key_prefix: StringProperty(name="Prefix", default="frame_")
    mute_existing: BoolProperty(name="Mute Other Keys", default=True,
                                description="Mute pre-existing shape keys so they are not "
                                            "applied twice on top of the baked frames")
    disable_modifiers: BoolProperty(name="Disable Modifiers After", default=True)


def _sk(context):
    return context.scene.shiro_shapekeys


class SHIRO_OT_sk_to_attribute(Operator):
    bl_idname = "shiro.sk_to_attribute"
    bl_label = "Bake Shape Key to Attribute"
    bl_description = "Write shape key positions (or offsets) into a point attribute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        o = context.object
        return o is not None and o.type == "MESH" and o.data.shape_keys is not None

    def execute(self, context):
        props = _sk(context)
        base = props.attribute_name.strip()
        problem = attribute_name_problem(base)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        obj = context.object
        with object_mode(context):
            me = obj.data
            keys = list(me.shape_keys.key_blocks) if props.all_keys else [obj.active_shape_key]
            done = 0
            for kb in keys:
                if kb is None or len(kb.data) != len(me.vertices):
                    continue
                name = f"{base}_{bpy.path.clean_name(kb.name)}" if props.all_keys else base
                attr, err = ensure_vector_attribute(me, name, props.overwrite)
                if err:
                    self.report({"ERROR"}, err)
                    return {"CANCELLED"}
                co = shape_coords(kb)
                if props.mode == "RELATIVE":
                    ref = kb.relative_key or me.shape_keys.reference_key
                    co -= shape_coords(ref)
                attr.data.foreach_set("vector", co)
                done += 1
            me.update()
        self.report({"INFO"}, f"Baked {done} shape key(s)")
        return {"FINISHED"}


class SHIRO_OT_attribute_to_sk(Operator):
    bl_idname = "shiro.attribute_to_sk"
    bl_label = "Attribute to Shape Key"
    bl_description = "Create a shape key from a FLOAT_VECTOR point attribute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        o = context.object
        return o is not None and o.type == "MESH"

    def execute(self, context):
        props = _sk(context)
        obj = context.object
        with object_mode(context):
            me = obj.data
            attr = me.attributes.get(props.attribute_name)
            if attr is None or attr.domain != "POINT" or attr.data_type != "FLOAT_VECTOR":
                self.report({"ERROR"}, f"No point vector attribute '{props.attribute_name}'")
                return {"CANCELLED"}
            co = np.empty(len(me.vertices) * 3, dtype=np.float32)
            attr.data.foreach_get("vector", co)
            if me.shape_keys is None:
                obj.shape_key_add(name="Basis", from_mix=False)
            kb = obj.shape_key_add(name=props.attribute_name, from_mix=False)
            if props.mode == "RELATIVE":
                co += shape_coords(me.shape_keys.reference_key)
            kb.data.foreach_set("co", co)
            me.update()
        return {"FINISHED"}


def evaluated_coords(obj, depsgraph):
    ev = obj.evaluated_get(depsgraph)
    mesh = ev.to_mesh()
    try:
        n = len(mesh.vertices)
        co = np.empty(n * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
    finally:
        ev.to_mesh_clear()
    return co


class SHIRO_OT_sk_bake_frames(Operator):
    bl_idname = "shiro.sk_bake_frames"
    bl_label = "Bake Frames to Shape Keys"
    bl_description = ("Store the evaluated mesh (cloth, Geometry Nodes, any modifiers) of "
                      "every frame as a shape key keyed on for exactly that frame")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        o = context.object
        return o is not None and o.type == "MESH"

    def execute(self, context):
        props = _sk(context)
        scene = context.scene
        obj = context.object
        if props.frame_end < props.frame_start:
            self.report({"ERROR"}, "End is before Start")
            return {"CANCELLED"}
        frames = list(range(props.frame_start, props.frame_end + 1, props.frame_step))
        original_frame = scene.frame_current
        with object_mode(context):
            me = obj.data
            vcount = len(me.vertices)
            snapshots = []
            wm = context.window_manager
            wm.progress_begin(0, len(frames))
            try:
                for i, f in enumerate(frames):
                    scene.frame_set(f)
                    co = evaluated_coords(obj, context.evaluated_depsgraph_get())
                    if co.size != vcount * 3:
                        self.report({"ERROR"}, f"Frame {f}: vertex count changes "
                                               f"({co.size // 3} vs {vcount}), can't use "
                                               "shape keys")
                        return {"CANCELLED"}
                    snapshots.append((f, co))
                    wm.progress_update(i + 1)
            finally:
                wm.progress_end()
                scene.frame_set(original_frame)

            if me.shape_keys is None:
                obj.shape_key_add(name="Basis", from_mix=False)
            existing = [kb for kb in me.shape_keys.key_blocks[1:]]
            if props.mute_existing:
                for kb in existing:
                    kb.mute = True
            step = props.frame_step
            for f, co in snapshots:
                kb = obj.shape_key_add(name=f"{props.key_prefix}{f:04d}", from_mix=False)
                kb.data.foreach_set("co", co)
                kb.value = 0.0
                for frame, value in ((f - step, 0.0), (f, 1.0), (f + step, 0.0)):
                    kb.value = value
                    kb.keyframe_insert("value", frame=frame)
            if props.disable_modifiers:
                for mod in obj.modifiers:
                    if mod.type not in {"ARMATURE"}:
                        mod.show_viewport = mod.show_render = False
            me.update()
        self.report({"INFO"}, f"Baked {len(snapshots)} frame(s)")
        return {"FINISHED"}


def gn_topology_ok(context, obj):
    """True when the evaluated mesh keeps the vertex count (shape keys possible)."""
    try:
        dg = context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(dg)
        return len(ev.data.vertices) == len(obj.data.vertices)
    except (AttributeError, RuntimeError):
        return True


def draw_shapekey_panel(self, context):
    obj = context.object
    if obj is None or obj.type != "MESH" or obj.data.shape_keys is None:
        return
    props = _sk(context)
    layout = self.layout
    box = layout.box()
    box.label(text="Shiro: Bake to Attribute", icon="RNA")
    col = box.column(align=True)
    col.prop(props, "attribute_name", text="")
    problem = attribute_name_problem(props.attribute_name.strip())
    if problem:
        col.label(text=problem, icon="ERROR")
    row = col.row(align=True)
    row.prop(props, "mode", text="")
    row.prop(props, "all_keys", toggle=True)
    row.prop(props, "overwrite", toggle=True)
    row = col.row(align=True)
    label = "Bake All Keys" if props.all_keys else f'Bake "{obj.active_shape_key.name}"' \
        if obj.active_shape_key else "Bake"
    row.operator("shiro.sk_to_attribute", text=label, icon="EXPORT")
    row.operator("shiro.attribute_to_sk", text="", icon="IMPORT")


class SHIRO_PT_gn_shapekey(Panel):
    bl_idname = "SHIRO_PT_gn_shapekey"
    bl_label = "Shape Key Tools (Shiro)"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "modifier"

    @classmethod
    def poll(cls, context):
        obj = context.object
        md = obj.modifiers.active if obj is not None else None
        return md is not None and obj.type == "MESH"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        md = obj.modifiers.active
        if not gn_topology_ok(context, obj):
            layout.label(text="Modifiers change the vertex count, shape keys impossible",
                         icon="ERROR")
        row = layout.row(align=True)
        op = row.operator("object.modifier_apply_as_shapekey", text="Apply as Shape Key",
                          icon="SHAPEKEY_DATA")
        op.modifier, op.keep_modifier = md.name, False
        op = row.operator("object.modifier_apply_as_shapekey", text="Save as Shape Key",
                          icon="SHAPEKEY_DATA")
        op.modifier, op.keep_modifier = md.name, True


class SHIRO_PT_sk_frames(Panel):
    bl_idname = "SHIRO_PT_sk_frames"
    bl_label = "Bake Frames to Shape Keys"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH"

    def draw(self, context):
        props = _sk(context)
        layout = self.layout
        row = layout.row(align=True)
        row.prop(props, "frame_start")
        row.prop(props, "frame_end")
        row.prop(props, "frame_step")
        layout.prop(props, "key_prefix")
        layout.prop(props, "mute_existing")
        layout.prop(props, "disable_modifiers")
        n = len(range(props.frame_start, props.frame_end + 1, max(props.frame_step, 1)))
        big = layout.row()
        big.scale_y = 1.4
        big.operator("shiro.sk_bake_frames", text=f"Bake {n} Frames", icon="REC")


classes = (SHIRO_PG_shapekeys, SHIRO_OT_sk_to_attribute, SHIRO_OT_attribute_to_sk,
           SHIRO_OT_sk_bake_frames, SHIRO_PT_gn_shapekey, SHIRO_PT_sk_frames)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_shapekeys = PointerProperty(type=SHIRO_PG_shapekeys)
    bpy.types.DATA_PT_shape_keys.append(draw_shapekey_panel)


def unregister():
    bpy.types.DATA_PT_shape_keys.remove(draw_shapekey_panel)
    del bpy.types.Scene.shiro_shapekeys
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
