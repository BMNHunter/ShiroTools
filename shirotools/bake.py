"""One-Click Baker.

What changed against the old baker:
* one bake call per map type for ALL materials (each writes into its own image
  node at once) instead of one full bake per material per map
* 1 sample for maps that do not need more, GPU when available
* raw ``image.save()`` instead of ``save_render()``, so the view transform
  (AgX) no longer alters base colour or corrupts normal maps
* the result goes into a NEW "<name>_baked" material; the original stays
  untouched and a toggle swaps the object between original and baked
* extra maps: Metallic, AO, Emission, Alpha; selected-to-active (high to low);
  one texture set per material or one shared set per object (atlas);
  channel packing presets for Unity (URP / HDRP) and ORM
"""

import os

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .common import TAB, smart_uv

BAKE_NODE = "SHIRO_BakeTarget"
THROWAWAY = "_shiro_bake_throwaway"

# key: (bake type, colour space, float buffer, suffix, emission source socket)
MAPS = {
    "DIFFUSE": ("DIFFUSE", "sRGB", False, "BaseColor", None),
    "ROUGHNESS": ("ROUGHNESS", "Non-Color", False, "Roughness", None),
    "METALLIC": ("EMIT", "Non-Color", False, "Metallic", "Metallic"),
    "NORMAL": ("NORMAL", "Non-Color", True, "Normal", None),
    "AO": ("AO", "Non-Color", False, "AO", None),
    "EMISSION": ("EMIT", "sRGB", False, "Emission", None),
    "ALPHA": ("EMIT", "Non-Color", False, "Alpha", "Alpha"),
}
ORDER = ("DIFFUSE", "ROUGHNESS", "METALLIC", "NORMAL", "AO", "EMISSION", "ALPHA")
PACKS = {
    # name: (suffix, [(channel source map, invert)] for R, G, B, A ; None = constant)
    "UNITY_URP": ("MetallicSmoothness", [("METALLIC", False), ("METALLIC", False),
                                         ("METALLIC", False), ("ROUGHNESS", True)]),
    "UNITY_HDRP": ("MaskMap", [("METALLIC", False), ("AO", False), (None, 1.0),
                               ("ROUGHNESS", True)]),
    "ORM": ("ORM", [("AO", False), ("ROUGHNESS", False), ("METALLIC", False), (None, 1.0)]),
}
RESOLUTIONS = [(str(s), f"{s // 1024}K" if s >= 1024 else str(s), "") for s in
               (512, 1024, 2048, 4096, 8192)]


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class SHIRO_PG_bake(PropertyGroup):
    map_diffuse: BoolProperty(name="Base Color", default=True)
    map_roughness: BoolProperty(name="Roughness", default=True)
    map_metallic: BoolProperty(name="Metallic", default=False)
    map_normal: BoolProperty(name="Normal", default=True)
    map_ao: BoolProperty(name="AO", default=False)
    map_emission: BoolProperty(name="Emission", default=False)
    map_alpha: BoolProperty(name="Alpha", default=False)

    resolution: EnumProperty(name="Resolution", items=RESOLUTIONS, default="2048")
    target: EnumProperty(name="Texture Sets", items=[
        ("PER_MATERIAL", "Per Material", "One texture set for every material"),
        ("PER_OBJECT", "Per Object", "One shared texture set per object. Needs non-overlapping "
                                     "UVs across all materials"),
    ], default="PER_MATERIAL")
    selected_to_active: BoolProperty(
        name="Selected to Active", default=False,
        description="Bake the other selected objects (high poly) onto the active one")
    cage_extrusion: FloatProperty(name="Extrusion", default=0.02, min=0.0, soft_max=1.0,
                                  subtype="DISTANCE")
    max_ray_distance: FloatProperty(name="Max Ray Distance", default=0.0, min=0.0,
                                    subtype="DISTANCE")
    cage_object: PointerProperty(name="Cage", type=bpy.types.Object,
                                 poll=lambda s, o: o.type == "MESH")
    margin: IntProperty(name="Margin", default=16, min=0, max=64, subtype="PIXEL")
    ao_samples: IntProperty(name="AO Samples", default=64, min=1, max=4096)
    use_gpu: BoolProperty(name="Use GPU", default=True)
    normal_convention: EnumProperty(name="Normal", items=[
        ("OPENGL", "OpenGL (Unity, Blender)", "Y+"),
        ("DIRECTX", "DirectX (Unreal)", "Y-"),
    ], default="OPENGL")
    output_dir: StringProperty(name="Folder", subtype="DIR_PATH", default="//Textures")
    auto_setup: BoolProperty(name="Create Baked Material", default=True)
    use_baked: BoolProperty(name="Switch to Baked", default=True,
                            description="Put the baked material on the object afterwards")
    pack: EnumProperty(name="Channel Pack", items=[
        ("NONE", "None", ""),
        ("UNITY_URP", "Unity URP / Built-in", "MetallicSmoothness: RGB metallic, A smoothness"),
        ("UNITY_HDRP", "Unity HDRP Mask", "R metallic, G AO, B detail mask, A smoothness"),
        ("ORM", "ORM (glTF / Unreal)", "R AO, G roughness, B metallic"),
    ], default="NONE")


class SHIRO_PG_bake_slot(PropertyGroup):
    original: PointerProperty(type=bpy.types.Material)
    baked: PointerProperty(type=bpy.types.Material)


def wanted_maps(settings):
    maps = [k for k in ORDER if getattr(settings, "map_" + k.lower())]
    if settings.pack != "NONE":
        for src, _inv in PACKS[settings.pack][1]:
            if src and src not in maps:
                maps.append(src)
    return [k for k in ORDER if k in maps]


# ---------------------------------------------------------------------------
# material plumbing
# ---------------------------------------------------------------------------

def output_node(nt):
    try:
        out = nt.get_output_node("CYCLES")
    except (AttributeError, TypeError):
        out = None
    if out is None:
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    return out


def principled_of(nt):
    out = output_node(nt)
    if out is not None and out.inputs["Surface"].is_linked:
        node = out.inputs["Surface"].links[0].from_node
        if node.type == "BSDF_PRINCIPLED":
            return node
    return next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)


class EmissionSwap:
    """Temporarily route one Principled input through an Emission shader."""

    def __init__(self, materials, socket_name):
        self.materials = materials
        self.socket = socket_name
        self.saved = []

    def __enter__(self):
        for mat in self.materials:
            nt = mat.node_tree
            out = output_node(nt)
            if out is None:
                continue
            surf = out.inputs["Surface"]
            prev = surf.links[0].from_socket if surf.is_linked else None
            emit = nt.nodes.new("ShaderNodeEmission")
            emit.inputs["Strength"].default_value = 1.0
            bsdf = principled_of(nt)
            src = bsdf.inputs.get(self.socket) if bsdf else None
            if src is not None and src.is_linked:
                nt.links.new(src.links[0].from_socket, emit.inputs["Color"])
            else:
                v = src.default_value if src is not None else (1.0 if self.socket == "Alpha"
                                                                else 0.0)
                emit.inputs["Color"].default_value = (v, v, v, 1.0)
            nt.links.new(emit.outputs["Emission"], surf)
            target = nt.nodes.get(BAKE_NODE)
            if target is not None:  # adding a node may steal the active slot
                nt.nodes.active = target
            self.saved.append((nt, out, prev, emit))
        return self

    def __exit__(self, *exc):
        for nt, out, prev, emit in self.saved:
            nt.nodes.remove(emit)
            if prev is not None:
                nt.links.new(prev, out.inputs["Surface"])
        return False


def ensure_bake_node(mat):
    nt = mat.node_tree
    node = nt.nodes.get(BAKE_NODE)
    if node is None or node.type != "TEX_IMAGE":
        if node is not None:
            nt.nodes.remove(node)
        node = nt.nodes.new("ShaderNodeTexImage")
        node.name = node.label = BAKE_NODE
        node.location = (-700, -500)
    nt.nodes.active = node
    return node


def remove_bake_nodes(materials):
    for mat in materials:
        node = mat.node_tree.nodes.get(BAKE_NODE) if mat.node_tree else None
        if node is not None:
            mat.node_tree.nodes.remove(node)


def get_image(name, size, is_float, colorspace, alpha=False):
    img = bpy.data.images.get(name)
    if img is not None and (tuple(img.size) != (size, size) or img.is_float != is_float):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, size, size, alpha=alpha, float_buffer=is_float)
    img.colorspace_settings.name = colorspace
    return img


def save_image(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()  # raw pixels: no view transform, float buffers become 16-bit PNG
    img.filepath = bpy.path.relpath(path) if bpy.data.filepath else path
    img.source = "FILE"


def clean(name):
    return bpy.path.clean_name(name)


def build_baked_material(name, maps, existing=None):
    """Principled material wired to the baked `maps` {key: image}."""
    mat = existing or bpy.data.materials.get(name) or bpy.data.materials.new(name)
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (500, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (100, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    y = 400
    for key in ORDER:
        img = maps.get(key)
        if img is None:
            continue
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image, tex.label, tex.location = img, MAPS[key][3], (-500, y)
        y -= 300
        col = tex.outputs["Color"]
        if key == "DIFFUSE":
            nt.links.new(col, bsdf.inputs["Base Color"])
        elif key == "ROUGHNESS":
            nt.links.new(col, bsdf.inputs["Roughness"])
        elif key == "METALLIC":
            nt.links.new(col, bsdf.inputs["Metallic"])
        elif key == "ALPHA":
            nt.links.new(col, bsdf.inputs["Alpha"])
        elif key == "EMISSION":
            nt.links.new(col, bsdf.inputs["Emission Color"])
            bsdf.inputs["Emission Strength"].default_value = 1.0
        elif key == "NORMAL":
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-180, y + 300)
            nt.links.new(col, nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        elif key == "AO":
            tex.label = "AO (for export, not used by the shader)"
    return mat


def pack_channels(pack, maps, size, name, path):
    suffix, layout = PACKS[pack]
    out = np.ones((size * size, 4), dtype=np.float32)
    cache = {}
    for ch, (src, inv) in enumerate(layout):
        if src is None:
            out[:, ch] = inv
            continue
        img = maps.get(src)
        if img is None:
            out[:, ch] = 1.0 if src == "AO" else 0.0
            continue
        if src not in cache:
            buf = np.empty(size * size * 4, dtype=np.float32)
            img.pixels.foreach_get(buf)
            cache[src] = buf.reshape(-1, 4)[:, 0]
        vals = cache[src]
        out[:, ch] = 1.0 - vals if inv else vals
    img = get_image(name, size, False, "Non-Color", alpha=True)
    img.alpha_mode = "CHANNEL_PACKED"
    img.pixels.foreach_set(out.ravel())
    save_image(img, path)
    return img


# ---------------------------------------------------------------------------
# the bake
# ---------------------------------------------------------------------------

class _SceneState:
    """Snapshot / restore every render setting the bake touches."""

    BAKE_ATTRS = ("margin", "margin_type", "use_selected_to_active", "cage_extrusion",
                  "max_ray_distance", "use_cage", "cage_object", "use_pass_direct",
                  "use_pass_indirect", "use_pass_color", "normal_space", "normal_g",
                  "target", "use_clear")

    def __init__(self, scene):
        self.scene = scene

    def __enter__(self):
        s = self.scene
        self.engine = s.render.engine
        self.samples = s.cycles.samples
        self.device = s.cycles.device
        self.bake = {a: getattr(s.render.bake, a) for a in self.BAKE_ATTRS}
        return self

    def __exit__(self, *exc):
        s = self.scene
        s.render.engine = self.engine
        s.cycles.samples = self.samples
        s.cycles.device = self.device
        for a, v in self.bake.items():
            try:
                setattr(s.render.bake, a, v)
            except (AttributeError, TypeError):
                pass
        return False


def gpu_available(context):
    addon = context.preferences.addons.get("cycles")
    return addon is not None and addon.preferences.compute_device_type not in {"NONE", ""}


def unique_materials(obj, enabled_only=False):
    seen, out = set(), []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.as_pointer() in seen:
            continue
        if enabled_only and not mat.shiro_bake_enabled:
            continue
        seen.add(mat.as_pointer())
        out.append(mat)
    return out


def bake_object(context, target, sources, settings, report):
    """Bake `settings` maps for `target`. Returns {set key: {map: image}}."""
    scene = context.scene
    size = int(settings.resolution)
    out_root = bpy.path.abspath(settings.output_dir)
    out_dir = os.path.join(out_root, clean(target.name))
    bake_mats = unique_materials(target)
    enabled = [m for m in bake_mats if m.shiro_bake_enabled]
    shade_mats = []
    for src in sources or [target]:
        shade_mats += [m for m in unique_materials(src) if src != target or m in enabled]
    throwaway = bpy.data.images.get(THROWAWAY) or bpy.data.images.new(THROWAWAY, 1, 1)

    sets = {}  # set key -> {map: image}
    per_object = settings.target == "PER_OBJECT"
    rb = scene.render.bake
    rb.margin = settings.margin
    rb.margin_type = "EXTEND"
    rb.use_clear = True
    rb.target = "IMAGE_TEXTURES"
    rb.use_selected_to_active = bool(sources)
    if sources:
        rb.cage_extrusion = settings.cage_extrusion
        rb.max_ray_distance = settings.max_ray_distance
        rb.use_cage = settings.cage_object is not None
        rb.cage_object = settings.cage_object
    rb.normal_g = "NEG_Y" if settings.normal_convention == "DIRECTX" else "POS_Y"

    maps = wanted_maps(settings)
    wm = context.window_manager
    wm.progress_begin(0, len(maps))
    try:
        for step, key in enumerate(maps):
            bake_type, cs, is_float, suffix, emit_from = MAPS[key]
            for mat in bake_mats:
                node = ensure_bake_node(mat)
                if mat not in enabled:
                    node.image = throwaway
                    continue
                set_key = target.name if per_object else mat.name
                base = clean(target.name) if per_object else \
                    f"{clean(target.name)}_{clean(mat.name)}"
                img = get_image(f"{base}_{suffix}", size, is_float, cs, alpha=key == "ALPHA")
                node.image = img
                sets.setdefault(set_key, {})[key] = img
            scene.cycles.samples = settings.ao_samples if key == "AO" else 1
            if key == "DIFFUSE":
                rb.use_pass_direct = rb.use_pass_indirect = False
                rb.use_pass_color = True
            if emit_from:
                with EmissionSwap(shade_mats, emit_from):
                    bpy.ops.object.bake(type=bake_type)
            else:
                bpy.ops.object.bake(type=bake_type)
            done = set()
            for images in sets.values():
                img = images.get(key)
                if img is not None and img.name not in done:
                    save_image(img, os.path.join(out_dir, img.name + ".png"))
                    done.add(img.name)
            wm.progress_update(step + 1)
    finally:
        wm.progress_end()
        remove_bake_nodes(bake_mats)

    if settings.pack != "NONE":
        suffix = PACKS[settings.pack][0]
        for set_key, images in sets.items():
            base = clean(target.name) if per_object else f"{clean(target.name)}_{clean(set_key)}"
            name = f"{base}_{suffix}"
            pack_channels(settings.pack, images, size, name, os.path.join(out_dir, name + ".png"))
    report({"INFO"}, f"{target.name}: {len(maps)} map(s) × {len(sets)} set(s) → {out_dir}")
    return sets


def apply_baked_materials(target, sets, settings):
    per_object = settings.target == "PER_OBJECT"
    target.shiro_bake_slots.clear()
    shared = None
    for slot in target.material_slots:
        item = target.shiro_bake_slots.add()
        mat = slot.material
        if mat is not None and mat.shiro_bake_source is not None:
            mat = mat.shiro_bake_source  # re-baking while the baked one is on
        item.original = mat
        if mat is None or not mat.shiro_bake_enabled:
            continue
        if per_object:
            if shared is None:
                name = f"{clean(target.name)}_baked"
                shared = build_baked_material(name, sets[target.name])
            baked = shared
        else:
            baked = build_baked_material(f"{mat.name}_baked", sets.get(mat.name, {}),
                                         mat.shiro_baked)
        baked.shiro_bake_source = mat
        mat.shiro_baked = baked
        item.baked = baked
    if settings.use_baked:
        set_slots(target, baked=True)


def set_slots(obj, baked):
    for slot, item in zip(obj.material_slots, obj.shiro_bake_slots):
        mat = item.baked if baked else item.original
        if mat is not None:
            slot.material = mat


def showing_baked(obj):
    pairs = list(zip(obj.material_slots, obj.shiro_bake_slots))
    return any(item.baked is not None and slot.material == item.baked for slot, item in pairs)


class SHIRO_OT_bake(Operator):
    bl_idname = "shiro.bake"
    bl_label = "Bake"
    bl_description = "Bake the chosen maps for the selected mesh objects"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects) or (
            context.active_object is not None and context.active_object.type == "MESH")

    def execute(self, context):
        scene = context.scene
        settings = scene.shiro_bake
        if not bpy.data.filepath and settings.output_dir.startswith("//"):
            self.report({"ERROR"}, "Save the .blend file first (or set an absolute folder)")
            return {"CANCELLED"}
        if not wanted_maps(settings):
            self.report({"ERROR"}, "No maps enabled")
            return {"CANCELLED"}

        active = context.active_object
        selected = [o for o in context.selected_objects if o.type == "MESH"]
        if active is not None and active.type == "MESH" and active not in selected:
            selected.append(active)
        if settings.selected_to_active:
            if active is None or len(selected) < 2:
                self.report({"ERROR"}, "Selected to Active needs a high poly selection plus "
                                       "the active low poly object")
                return {"CANCELLED"}
            jobs = [(active, [o for o in selected if o != active])]
        else:
            jobs = [(o, None) for o in selected]

        for target, _src in jobs:
            if not target.data.uv_layers:
                self.report({"ERROR"}, f"'{target.name}' has no UV map (use Smart UV)")
                return {"CANCELLED"}
            if not any(m.shiro_bake_enabled for m in unique_materials(target)):
                self.report({"ERROR"}, f"'{target.name}' has no enabled materials")
                return {"CANCELLED"}

        prev_sel = list(context.selected_objects)
        prev_active = context.view_layer.objects.active
        prev_mode = active.mode if active else "OBJECT"
        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        try:
            with _SceneState(scene):
                scene.render.engine = "CYCLES"
                if settings.use_gpu and gpu_available(context):
                    scene.cycles.device = "GPU"
                for target, sources in jobs:
                    for o in context.view_layer.objects:
                        o.select_set(False)
                    for o in (sources or []) + [target]:
                        o.select_set(True)
                    context.view_layer.objects.active = target
                    # the baked material must not be the one being baked
                    if target.shiro_bake_slots and showing_baked(target):
                        set_slots(target, baked=False)
                    sets = bake_object(context, target, sources, settings, self.report)
                    if settings.auto_setup:
                        apply_baked_materials(target, sets, settings)
        except RuntimeError as err:
            self.report({"ERROR"}, f"Bake failed: {err}")
            return {"CANCELLED"}
        finally:
            for o in context.view_layer.objects:
                o.select_set(o in prev_sel)
            context.view_layer.objects.active = prev_active
            thr = bpy.data.images.get(THROWAWAY)
            if thr is not None and thr.users == 0:
                bpy.data.images.remove(thr)
            if prev_mode != "OBJECT":
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except RuntimeError:
                    pass
        return {"FINISHED"}


class SHIRO_OT_bake_toggle(Operator):
    bl_idname = "shiro.bake_toggle"
    bl_label = "Original / Baked"
    bl_description = "Swap the selected objects between their original and baked materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.shiro_bake_slots] or (
            [context.active_object] if context.active_object else [])
        for obj in objs:
            if obj.shiro_bake_slots:
                set_slots(obj, baked=not showing_baked(obj))
        return {"FINISHED"}


class SHIRO_OT_bake_smart_uv(Operator):
    bl_idname = "shiro.bake_smart_uv"
    bl_label = "Smart UV Project"
    bl_description = "Unwrap the selected meshes that have no UV map yet"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        todo = [o for o in context.selected_objects if o.type == "MESH" and not o.data.uv_layers]
        smart_uv(context, todo)
        self.report({"INFO"}, f"Unwrapped {len(todo)} object(s)")
        return {"FINISHED"}


class SHIRO_UL_bake_slots(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        mat = item.material
        row = layout.row(align=True)
        if mat is None:
            row.label(text="(empty slot)", icon="BLANK1")
            return
        row.prop(mat, "shiro_bake_enabled", text="")
        row.label(text=mat.name, icon="MATERIAL" if mat.shiro_bake_source is None
                  else "SHADING_TEXTURE")


class SHIRO_PT_bake(Panel):
    bl_idname = "SHIRO_PT_bake"
    bl_label = "One-Click Baker"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.shiro_bake
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh", icon="INFO")
            return
        layout.template_list("SHIRO_UL_bake_slots", "", obj, "material_slots",
                             obj, "active_material_index", rows=3)
        if not obj.data.uv_layers:
            row = layout.row()
            row.alert = True
            row.operator("shiro.bake_smart_uv", icon="UV")

        grid = layout.grid_flow(columns=2, even_columns=True, align=True)
        for key in ORDER:
            grid.prop(s, "map_" + key.lower(), toggle=True)
        col = layout.column(align=True)
        col.prop(s, "resolution")
        col.prop(s, "target", text="")
        col.prop(s, "pack")
        col.prop(s, "output_dir")

        box = layout.box()
        box.prop(s, "selected_to_active")
        if s.selected_to_active:
            col = box.column(align=True)
            col.prop(s, "cage_extrusion")
            col.prop(s, "max_ray_distance")
            col.prop(s, "cage_object")
        sub = layout.column(align=True)
        sub.use_property_split = True
        sub.prop(s, "margin")
        if s.map_ao or s.pack in {"UNITY_HDRP", "ORM"}:
            sub.prop(s, "ao_samples")
        sub.prop(s, "normal_convention")
        sub.prop(s, "use_gpu")
        sub.prop(s, "auto_setup")
        if s.auto_setup:
            sub.prop(s, "use_baked")

        row = layout.row()
        row.scale_y = 1.8
        n = len([o for o in context.selected_objects if o.type == "MESH"]) or 1
        label = "Bake High → Low" if s.selected_to_active else (
            f"Bake {n} Objects" if n > 1 else "Bake")
        row.operator("shiro.bake", text=label, icon="RENDER_STILL")
        if obj.shiro_bake_slots:
            row = layout.row()
            row.operator("shiro.bake_toggle",
                         text="Show Original" if showing_baked(obj) else "Show Baked",
                         icon="ARROW_LEFTRIGHT")


classes = (SHIRO_PG_bake, SHIRO_PG_bake_slot, SHIRO_OT_bake, SHIRO_OT_bake_toggle,
           SHIRO_OT_bake_smart_uv, SHIRO_UL_bake_slots, SHIRO_PT_bake)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_bake = PointerProperty(type=SHIRO_PG_bake)
    bpy.types.Object.shiro_bake_slots = CollectionProperty(type=SHIRO_PG_bake_slot)
    bpy.types.Material.shiro_bake_enabled = BoolProperty(
        name="Bake", default=True, description="Include this material in the bake")
    bpy.types.Material.shiro_baked = PointerProperty(type=bpy.types.Material)
    bpy.types.Material.shiro_bake_source = PointerProperty(type=bpy.types.Material)


def unregister():
    del bpy.types.Material.shiro_bake_source
    del bpy.types.Material.shiro_baked
    del bpy.types.Material.shiro_bake_enabled
    del bpy.types.Object.shiro_bake_slots
    del bpy.types.Scene.shiro_bake
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
