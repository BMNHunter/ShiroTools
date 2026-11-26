"""Cost Inspector: what makes this scene heavy?

* Objects ranked by evaluated triangles (instances are counted and credited to
  the object that instances them), modifier evaluation time and texture VRAM.
* Modifiers ranked by their last evaluation time (Modifier.execution_time).
* One click to hide the slowest modifiers in the viewport, and to bring them
  back (the list is stored in the scene, so it survives save / reload).
"""

import json
from collections import defaultdict

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel

from .common import TAB, human_bytes, human_count, mesh_tri_count

_scan = None


def scan(context):
    dg = context.evaluated_depsgraph_get()
    tri_cache = {}
    tris = defaultdict(int)
    instances = defaultdict(int)
    for inst in dg.object_instances:
        ob = inst.object
        data = getattr(ob, "data", None)
        if not isinstance(data, bpy.types.Mesh):
            continue
        key = data.as_pointer()
        if key not in tri_cache:
            tri_cache[key] = mesh_tri_count(data)
        owner = inst.parent.original if inst.is_instance and inst.parent else ob.original
        tris[owner.name] += tri_cache[key]
        if inst.is_instance:
            instances[owner.name] += 1

    from .textures import estimate_vram, image_size  # header-based, never loads pixels
    from .common import object_images
    objects = []
    mods = []
    for obj in context.scene.objects:
        mod_time = 0.0
        for m in obj.modifiers:
            t = getattr(m, "execution_time", 0.0) or 0.0
            mod_time += t
            mods.append((obj.name, m.name, m.type, t, m.show_viewport))
        base = mesh_tri_count(obj.data) if obj.type == "MESH" else 0
        vram = 0
        for img in object_images(obj):
            size = image_size(img)
            if size:
                vram += estimate_vram(img, size)
        if tris.get(obj.name) or mod_time or vram:
            objects.append(dict(name=obj.name, tris=tris.get(obj.name, 0), base=base,
                                instances=instances.get(obj.name, 0), time=mod_time, vram=vram,
                                mats=len([s for s in obj.material_slots if s.material])))
    mods.sort(key=lambda m: -m[3])
    return {"objects": objects, "mods": mods, "total_tris": sum(tris.values()),
            "timed": any(m[3] > 0 for m in mods)}


def _disabled(scene):
    try:
        return json.loads(scene.shiro_cost_disabled or "[]")
    except ValueError:
        return []


class SHIRO_OT_cost_scan(Operator):
    bl_idname = "shiro.cost_scan"
    bl_label = "Inspect Scene Cost"
    bl_description = "Rank objects and modifiers by triangles, evaluation time and VRAM"

    def execute(self, context):
        global _scan
        _scan = scan(context)
        self.report({"INFO"}, f"{human_count(_scan['total_tris'])} triangles evaluated")
        return {"FINISHED"}


class SHIRO_OT_cost_toggle_mod(Operator):
    bl_idname = "shiro.cost_toggle_mod"
    bl_label = "Toggle Modifier in Viewport"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty()
    modifier: StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        mod = obj.modifiers.get(self.modifier) if obj else None
        if mod is None:
            return {"CANCELLED"}
        mod.show_viewport = not mod.show_viewport
        bpy.ops.shiro.cost_scan()
        return {"FINISHED"}


class SHIRO_OT_cost_disable_heavy(Operator):
    bl_idname = "shiro.cost_disable_heavy"
    bl_label = "Hide Slow Modifiers"
    bl_description = "Turn off viewport display of modifiers slower than the threshold"
    bl_options = {"REGISTER", "UNDO"}

    threshold_ms: FloatProperty(name="Slower Than (ms)", default=5.0, min=0.0)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        if _scan is None:
            bpy.ops.shiro.cost_scan()
        scene = context.scene
        stored = _disabled(scene)
        n = 0
        for obj_name, mod_name, _t, time, shown in _scan["mods"]:
            if shown and time * 1000.0 >= self.threshold_ms:
                obj = bpy.data.objects.get(obj_name)
                mod = obj.modifiers.get(mod_name) if obj else None
                if mod is not None:
                    mod.show_viewport = False
                    stored.append([obj_name, mod_name])
                    n += 1
        scene.shiro_cost_disabled = json.dumps(stored)
        bpy.ops.shiro.cost_scan()
        self.report({"INFO"}, f"Hid {n} modifier(s)")
        return {"FINISHED"}


class SHIRO_OT_cost_restore(Operator):
    bl_idname = "shiro.cost_restore"
    bl_label = "Restore Hidden Modifiers"
    bl_description = "Turn viewport display back on for modifiers hidden by Hide Slow Modifiers"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_disabled(context.scene))

    def execute(self, context):
        n = 0
        for obj_name, mod_name in _disabled(context.scene):
            obj = bpy.data.objects.get(obj_name)
            mod = obj.modifiers.get(mod_name) if obj else None
            if mod is not None:
                mod.show_viewport = True
                n += 1
        context.scene.shiro_cost_disabled = "[]"
        bpy.ops.shiro.cost_scan()
        self.report({"INFO"}, f"Restored {n} modifier(s)")
        return {"FINISHED"}


SORT_KEY = {"TRIS": "tris", "TIME": "time", "VRAM": "vram", "INSTANCES": "instances"}


class SHIRO_PT_cost(Panel):
    bl_idname = "SHIRO_PT_cost"
    bl_label = "Cost Inspector"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("shiro.cost_scan", icon="VIEWZOOM")
        if _scan is None:
            return
        layout.label(text=f"Scene: {human_count(_scan['total_tris'])} tris (evaluated)",
                     icon="MESH_DATA")
        row = layout.row(align=True)
        row.prop(sc, "shiro_cost_sort", expand=True)
        key = SORT_KEY[sc.shiro_cost_sort]
        objs = sorted(_scan["objects"], key=lambda o: -o[key])[:sc.shiro_cost_top]
        top = max((o[key] for o in objs), default=0) or 1
        box = layout.box()
        for o in objs:
            r = box.row(align=True)
            r.operator("shiro.select_object", text="", icon="RESTRICT_SELECT_OFF",
                       emboss=False).name = o["name"]
            split = r.split(factor=0.4, align=True)
            split.label(text=o["name"])
            if key == "tris":
                text = human_count(o["tris"])
                if o["base"] and o["tris"] > o["base"] * 1.5:
                    text += f"  (base {human_count(o['base'])})"
                if o["instances"]:
                    text += f"  ×{o['instances']} inst"
            elif key == "time":
                text = f"{o['time'] * 1000:.1f} ms"
            elif key == "vram":
                text = human_bytes(o["vram"])
            else:
                text = f"{o['instances']} instances"
            split.progress(factor=o[key] / top, text=text)

        box = layout.box()
        head = box.row(align=True)
        head.label(text="Slowest modifiers", icon="MODIFIER")
        if not _scan["timed"]:
            sub = box.row()
            sub.active = False
            sub.label(text="No timings yet: move the timeline once, then inspect again")
        mods = [m for m in _scan["mods"] if m[3] > 0][:sc.shiro_cost_top]
        tmax = max((m[3] for m in mods), default=0) or 1
        for obj_name, mod_name, mtype, t, shown in mods:
            r = box.row(align=True)
            op = r.operator("shiro.cost_toggle_mod", text="", emboss=False,
                            icon="RESTRICT_VIEW_OFF" if shown else "RESTRICT_VIEW_ON")
            op.object_name, op.modifier = obj_name, mod_name
            split = r.split(factor=0.4, align=True)
            split.label(text=f"{obj_name} › {mod_name}")
            split.progress(factor=t / tmax, text=f"{t * 1000:.1f} ms")
        row = layout.row(align=True)
        row.operator("shiro.cost_disable_heavy", icon="HIDE_ON")
        row.operator("shiro.cost_restore", text="", icon="LOOP_BACK")
        layout.prop(sc, "shiro_cost_top")


classes = (SHIRO_OT_cost_scan, SHIRO_OT_cost_toggle_mod, SHIRO_OT_cost_disable_heavy,
           SHIRO_OT_cost_restore, SHIRO_PT_cost)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_cost_sort = EnumProperty(name="Sort", items=[
        ("TRIS", "Tris", "Evaluated triangles"), ("TIME", "Time", "Modifier evaluation time"),
        ("VRAM", "VRAM", "Texture memory"), ("INSTANCES", "Inst", "Instance count")],
        default="TRIS")
    bpy.types.Scene.shiro_cost_top = IntProperty(name="Show", default=10, min=3, max=50)
    bpy.types.Scene.shiro_cost_disabled = StringProperty(default="[]")


def unregister():
    global _scan
    _scan = None
    del bpy.types.Scene.shiro_cost_disabled
    del bpy.types.Scene.shiro_cost_top
    del bpy.types.Scene.shiro_cost_sort
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
