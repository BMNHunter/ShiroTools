"""ShiroTools: one toolbag for timeline, camera, animation, textures, baking,
weights, shape keys, mesh checks, cloth layers, ivy, scene cleanup, render
queue, Unity export and scene cost inspection.

Every tool is a module that can be switched off in the add-on preferences.
"""

import importlib
import traceback

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from . import common

# (module, label, description)
MODULES = (
    ("timeline", "Timeline", "Markers, range presets, BPM beats, colour tags, overlay"),
    ("camera", "Camera Bookmarks", "Save and jump between camera angles"),
    ("anim_rename", "Graph Rename", "Group rename, batch rename, proxy channels (F2)"),
    ("textures", "Textures", "Texture stats / VRAM and resizing"),
    ("bake", "One-Click Baker", "Bake maps, baked material, Unity channel packing"),
    ("weights", "Weights", "Rig/transfer/smooth pipeline and weight toolkit"),
    ("shapekeys", "Shape Keys", "Shape key <-> attribute, frames to shape keys"),
    ("meshcheck", "Surface Deform Check", "Validate and fix Surface Deform targets"),
    ("cloth_layers", "Cloth Layers", "Collision-distance layers for GN cloth"),
    ("ivygen", "IvyGen", "Procedural ivy over edges and surfaces, leaf/bark materials"),
    ("scene_doctor", "Scene Doctor", "Missing files, duplicates, orphans"),
    ("render_queue", "Render Queue", "Batch render ranges, bookmarks and camera cuts"),
    ("exporter", "Unity Export", "FBX presets for Unity with a health check"),
    ("cost_inspector", "Cost Inspector", "Heaviest objects and slowest modifiers"),
)

_active = {}          # module name -> module, currently registered
_tab = common.TAB     # tab the panels currently use

# Properties keep their old add-on names so existing .blend data loads. Disabling
# one of those old add-ons runs its unregister(), which deletes the shared
# property and blanks our panels. We remember every property we add and put
# back any that vanish.
_OWNERS = (bpy.types.Scene, bpy.types.Object, bpy.types.Material)
_owned = []           # (owner type, name, deferred definition)


def _prop_names():
    return {o: set(o.bl_rna.properties.keys()) for o in _OWNERS}


def _heal():
    for owner, name, deferred in _owned:
        if name not in owner.bl_rna.properties:
            try:
                setattr(owner, name, deferred)
                print(f"[ShiroTools] restored {owner.__name__}.{name}")
            except (TypeError, ValueError, RuntimeError):
                pass
    return 1.0


def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _retab(module, tab):
    for cls in getattr(module, "classes", ()):
        if issubclass(cls, Panel) and getattr(cls, "bl_region_type", "") == "UI" \
                and getattr(cls, "bl_category", None) in {common.TAB, _tab}:
            cls.bl_category = tab


def _register_modules():
    global _tab
    prefs = _prefs()
    tab = (prefs.tab_name.strip() if prefs else "") or common.TAB
    for name, _label, _desc in MODULES:
        if prefs is not None and not getattr(prefs, "use_" + name):
            continue
        try:
            module = importlib.import_module(f".{name}", __package__)
            _retab(module, tab)
            before = _prop_names()
            module.register()
            _active[name] = module
            for owner, names in _prop_names().items():
                for prop in names - before[owner]:
                    _owned.append((owner, prop, getattr(owner, prop)))
        except Exception:  # one broken tool must not take the others down
            print(f"[ShiroTools] failed to register '{name}':")
            traceback.print_exc()
    _tab = tab


def _unregister_modules():
    _heal()  # a module's unregister() expects its properties to exist
    _owned.clear()
    for name in reversed(list(_active)):
        try:
            _active[name].unregister()
        except Exception:
            print(f"[ShiroTools] failed to unregister '{name}':")
            traceback.print_exc()
    _active.clear()


def _reload(self, context):
    _unregister_modules()
    _register_modules()


def _prefs_annotations():
    ann = {"tab_name": StringProperty(name="Sidebar Tab", default=common.TAB, update=_reload,
                                      description="N-panel tab used by every ShiroTools panel")}
    for name, label, desc in MODULES:
        ann["use_" + name] = BoolProperty(name=label, description=desc, default=True,
                                          update=_reload)
    return ann


class ShiroToolsPreferences(AddonPreferences):
    bl_idname = __package__
    __annotations__ = _prefs_annotations()

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "tab_name")
        layout.label(text="Tools")
        grid = layout.grid_flow(columns=2, even_columns=True)
        for name, label, desc in MODULES:
            box = grid.box()
            row = box.row()
            row.prop(self, "use_" + name)
            sub = box.row()
            sub.active = False
            sub.label(text=desc)


class SHIRO_OT_select_object(Operator):
    bl_idname = "shiro.select_object"
    bl_label = "Select Object"
    bl_description = "Select this object and make it active"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    def execute(self, context):
        obj = context.scene.objects.get(self.name)
        if obj is None:
            self.report({"WARNING"}, f"'{self.name}' is not in this scene (instanced?)")
            return {"CANCELLED"}
        for o in context.selected_objects:
            o.select_set(False)
        try:
            obj.select_set(True)
        except RuntimeError:
            pass
        context.view_layer.objects.active = obj
        return {"FINISHED"}


_core = (ShiroToolsPreferences, SHIRO_OT_select_object)


def register():
    for cls in _core:
        bpy.utils.register_class(cls)
    _register_modules()
    bpy.app.timers.register(_heal, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_heal):
        bpy.app.timers.unregister(_heal)
    _unregister_modules()
    for cls in reversed(_core):
        bpy.utils.unregister_class(cls)
