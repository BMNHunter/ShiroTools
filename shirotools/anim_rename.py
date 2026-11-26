"""Graph Editor: group rename, batch rename and proxy channels.

Ported to the Blender 5.x layered-action (channelbag) API. The legacy
``action.fcurves`` / ``action.groups`` API this used to rely on is gone.

* Rename Group (F2): rename the selected channels' group, or group them.
* Batch Rename Groups: find / replace (optionally regex) on group names.
* Proxy Channel (Shift+F2): move a channel's animation onto a custom
  property with a readable name and drive the original from it.
* Bake Back: undo a proxy, keys go back onto the original property.
"""

import re

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

KEY_VEC = ("co", "handle_left", "handle_right")
KEY_SCALAR = ("interpolation", "easing", "handle_left_type", "handle_right_type", "type",
              "back", "amplitude", "period")
INT_KEYS = {"interpolation", "easing", "handle_left_type", "handle_right_type", "type"}
FLOAT_SUBTYPES = {"NONE", "PIXEL", "UNSIGNED", "PERCENTAGE", "FACTOR", "ANGLE", "TIME",
                  "TIME_ABSOLUTE", "DISTANCE", "DISTANCE_CAMERA", "POWER", "TEMPERATURE",
                  "WAVELENGTH", "COLOR_TEMPERATURE", "FREQUENCY"}
VECTOR_TO_SCALAR = {"EULER": "ANGLE", "QUATERNION": "NONE", "AXISANGLE": "ANGLE",
                    "TRANSLATION": "DISTANCE", "XYZ": "NONE", "XYZ_LENGTH": "DISTANCE",
                    "COLOR": "FACTOR", "COLOR_GAMMA": "FACTOR", "DIRECTION": "NONE",
                    "VELOCITY": "NONE", "ACCELERATION": "NONE", "LAYER": "NONE"}


# ---------------------------------------------------------------------------
# channelbag helpers
# ---------------------------------------------------------------------------

def channelbag_of(fc):
    """(action, channelbag) owning F-Curve `fc`, or (None, None)."""
    act = fc.id_data
    if not isinstance(act, bpy.types.Action):
        return None, None
    for layer in act.layers:
        for strip in layer.strips:
            for cb in getattr(strip, "channelbags", ()):
                found = cb.fcurves.find(fc.data_path, index=fc.array_index)
                if found is not None and found == fc:
                    return act, cb
    return act, None


def owner_of(cb):
    users = cb.slot.users() if cb is not None and cb.slot is not None else ()
    return users[0] if users else None


def anim_fcurves(context):
    return [fc for fc in (context.selected_editable_fcurves or ())
            if isinstance(fc.id_data, bpy.types.Action)]


def active_anim_fcurve(context):
    fc = getattr(context, "active_editable_fcurve", None)
    if fc is not None and isinstance(fc.id_data, bpy.types.Action):
        return fc
    sel = anim_fcurves(context)
    return sel[0] if sel else None


def _graph_poll(context):
    area = context.area
    return area is not None and area.type == "GRAPH_EDITOR" and bool(anim_fcurves(context))


# ---------------------------------------------------------------------------
# property introspection
# ---------------------------------------------------------------------------

def split_path(path):
    """('parent', 'name', is_custom_prop)."""
    if path.endswith('"]'):
        i = path.rfind('["')
        return path[:i], path[i + 2:-2], True
    i = path.rfind(".")
    if i == -1:
        return "", path, False
    return path[:i], path[i + 1:], False


def describe_property(owner, path):
    """Dict with kind (FLOAT/INT/BOOLEAN), subtype and min/max hints."""
    info = {"kind": "FLOAT", "subtype": "NONE"}
    parent_path, name, custom = split_path(path)
    try:
        parent = owner.path_resolve(parent_path) if parent_path else owner
    except (ValueError, TypeError):
        parent = None

    if parent is not None and not custom and hasattr(parent, "bl_rna"):
        prop = parent.bl_rna.properties.get(name)
        if prop is not None:
            info["kind"] = {"INT": "INT", "BOOLEAN": "BOOLEAN", "ENUM": "INT"}.get(prop.type,
                                                                                 "FLOAT")
            st = prop.subtype
            st = VECTOR_TO_SCALAR.get(st, st)
            info["subtype"] = st if st in FLOAT_SUBTYPES else "NONE"
            if prop.type in {"INT", "FLOAT"}:
                info["min"], info["max"] = prop.hard_min, prop.hard_max
                info["soft_min"], info["soft_max"] = prop.soft_min, prop.soft_max
            elif prop.type == "ENUM":
                info["min"], info["max"] = 0, max(0, len(prop.enum_items) - 1)
            return info

    if parent is not None and custom:
        try:
            value = parent[name]
            ui = parent.id_properties_ui(name).as_dict()
        except (KeyError, TypeError, AttributeError):
            value, ui = None, {}
        if isinstance(value, bool):
            info["kind"] = "BOOLEAN"
        elif isinstance(value, int):
            info["kind"] = "INT"
        st = ui.get("subtype", "NONE")
        st = VECTOR_TO_SCALAR.get(st, st)
        info["subtype"] = st if st in FLOAT_SUBTYPES else "NONE"
        for k in ("min", "max", "soft_min", "soft_max"):
            if k in ui:
                info[k] = ui[k]
        return info

    low = path.lower()
    if "rotation" in low or "angle" in low:
        info["subtype"] = "ANGLE"
    elif "location" in low or "position" in low:
        info["subtype"] = "DISTANCE"
    return info


# ---------------------------------------------------------------------------
# key copying
# ---------------------------------------------------------------------------

def copy_keys(src, dst):
    kps = src.keyframe_points
    n = len(kps)
    dst.keyframe_points.add(n)
    for attr in KEY_VEC:
        buf = np.empty(n * 2, dtype=np.float32)
        kps.foreach_get(attr, buf)
        dst.keyframe_points.foreach_set(attr, buf)
    for attr in KEY_SCALAR:
        buf = np.empty(n, dtype=np.int32 if attr in INT_KEYS else np.float32)
        kps.foreach_get(attr, buf)
        dst.keyframe_points.foreach_set(attr, buf)
    for attr in ("extrapolation", "color_mode", "color", "auto_smoothing", "hide", "lock",
                 "mute"):
        try:
            setattr(dst, attr, getattr(src, attr))
        except (AttributeError, TypeError, ValueError):
            pass
    for mod in src.modifiers:
        new = dst.modifiers.new(mod.type)
        for p in mod.bl_rna.properties:
            if p.is_readonly or p.identifier in {"rna_type", "type"}:
                continue
            try:
                setattr(new, p.identifier, getattr(mod, p.identifier))
            except (AttributeError, TypeError, ValueError):
                pass
    dst.update()


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SHIRO_OT_graph_rename_group(Operator):
    bl_idname = "graph.shiro_rename_group"
    bl_label = "Rename Group"
    bl_description = "Rename the group of the selected channels, or group ungrouped ones"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="Group Name")

    @classmethod
    def poll(cls, context):
        return _graph_poll(context)

    def invoke(self, context, event):
        fc = active_anim_fcurve(context)
        self.new_name = fc.group.name if fc is not None and fc.group else "New Group"
        return context.window_manager.invoke_props_dialog(self, width=280)

    def execute(self, context):
        name = self.new_name.strip()
        if not name:
            return {"CANCELLED"}
        fcurves = anim_fcurves(context)
        groups = {fc.group.as_pointer(): fc.group for fc in fcurves if fc.group}
        if groups:
            for grp in groups.values():
                grp.name = name
        else:
            made = {}
            for fc in fcurves:
                _act, cb = channelbag_of(fc)
                if cb is None:
                    continue
                key = cb.as_pointer()
                if key not in made:
                    made[key] = cb.groups.get(name) or cb.groups.new(name)
                fc.group = made[key]
        context.area.tag_redraw()
        return {"FINISHED"}


class SHIRO_OT_graph_batch_rename(Operator):
    bl_idname = "graph.shiro_batch_rename_groups"
    bl_label = "Batch Rename Groups"
    bl_description = "Find and replace in the names of the selected channels' groups"
    bl_options = {"REGISTER", "UNDO"}

    find: StringProperty(name="Find")
    replace: StringProperty(name="Replace")
    use_regex: BoolProperty(name="Regex", default=False)
    scope: EnumProperty(name="Scope", items=[
        ("SELECTED", "Selected Channels", "Groups of the selected channels"),
        ("ALL", "All Groups", "Every group in the selected channels' actions"),
    ], default="SELECTED")

    @classmethod
    def poll(cls, context):
        return _graph_poll(context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        if not self.find:
            return {"CANCELLED"}
        try:
            rx = re.compile(self.find) if self.use_regex else None
        except re.error as err:
            self.report({"ERROR"}, f"Bad regex: {err}")
            return {"CANCELLED"}
        targets = {}
        for fc in anim_fcurves(context):
            if self.scope == "ALL":
                _act, cb = channelbag_of(fc)
                for grp in cb.groups if cb else ():
                    targets[grp.as_pointer()] = grp
            elif fc.group:
                targets[fc.group.as_pointer()] = fc.group
        changed = 0
        for grp in targets.values():
            new = rx.sub(self.replace, grp.name) if rx else grp.name.replace(self.find,
                                                                             self.replace)
            if new and new != grp.name:
                grp.name = new
                changed += 1
        self.report({"INFO"}, f"Renamed {changed} group(s)")
        return {"FINISHED"}


class SHIRO_OT_graph_proxy(Operator):
    bl_idname = "graph.shiro_proxy_channel"
    bl_label = "Rename Channel (Proxy)"
    bl_description = ("Move this channel's keys onto a custom property with a readable "
                      "name and drive the original property from it")
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="Channel Name")

    @classmethod
    def poll(cls, context):
        return _graph_poll(context)

    def invoke(self, context, event):
        fc = active_anim_fcurve(context)
        if fc is None:
            return {"CANCELLED"}
        _parent, name, _custom = split_path(fc.data_path)
        if fc.array_index or self._is_array(fc):
            name = f"{name}_{'xyzw'[fc.array_index] if fc.array_index < 4 else fc.array_index}"
        self.new_name = name
        return context.window_manager.invoke_props_dialog(self, width=280)

    @staticmethod
    def _is_array(fc):
        _act, cb = channelbag_of(fc)
        return cb is not None and cb.fcurves.find(fc.data_path, index=1) is not None

    def execute(self, context):
        fc = active_anim_fcurve(context)
        try:
            owner, path, index = make_proxy(fc, self.new_name.strip(),
                                            context.scene.frame_current)
        except ValueError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        self.report({"INFO"}, f"'{self.new_name.strip()}' now drives {path}[{index}]")
        context.area.tag_redraw()
        return {"FINISHED"}


def make_proxy(fc, name, frame):
    """Move `fc`'s keys to custom property `name` and drive the original from it.

    Returns (owner, original_path, original_index). Raises ValueError.
    """
    _act, cb = channelbag_of(fc) if fc else (None, None)
    owner = owner_of(cb)
    if fc is None or cb is None or owner is None:
        raise ValueError("Could not find the animated data-block of this channel")
    if not name or '"' in name or "\\" in name:
        raise ValueError("Invalid name")
    if name in owner.keys():
        raise ValueError(f"Property '{name}' already exists on {owner.name}")

    info = describe_property(owner, fc.data_path)
    value = fc.evaluate(frame)
    if info["kind"] == "BOOLEAN":
        owner[name] = bool(round(value))
    elif info["kind"] == "INT":
        owner[name] = int(round(value))
    else:
        owner[name] = float(value)
    if info["kind"] != "BOOLEAN":
        ui = {k: info[k] for k in ("min", "max", "soft_min", "soft_max") if k in info}
        if info["kind"] == "INT":
            ui = {k: int(max(min(v, 2**31 - 1), -2**31)) for k, v in ui.items()}
        else:
            ui["subtype"] = info["subtype"]
            if "soft_min" not in ui:  # no hints: span the animated values
                ys = [k.co[1] for k in fc.keyframe_points] or [value]
                pad = max(1.0, (max(ys) - min(ys)) * 0.5)
                ui["soft_min"], ui["soft_max"] = min(ys) - pad, max(ys) + pad
        owner.id_properties_ui(name).update(**ui)

    new_path = f'["{name}"]'
    new_fc = cb.fcurves.new(new_path, index=0, group_name=fc.group.name if fc.group else "")
    copy_keys(fc, new_fc)
    orig_path, orig_index = fc.data_path, fc.array_index
    cb.fcurves.remove(fc)
    try:
        dfc = owner.driver_add(orig_path, orig_index)
    except TypeError:
        dfc = owner.driver_add(orig_path)
    drv = dfc.driver
    drv.type = "AVERAGE"
    var = drv.variables.new()
    var.name = "val"
    var.type = "SINGLE_PROP"
    tgt = var.targets[0]
    tgt.id_type = owner.id_type
    tgt.id = owner
    tgt.data_path = new_path
    return owner, orig_path, orig_index


def find_proxy_driver(owner, prop_path):
    ad = owner.animation_data
    hits = []
    for dfc in ad.drivers if ad else ():
        variables = dfc.driver.variables
        if len(variables) != 1 or variables[0].type != "SINGLE_PROP":
            continue
        t = variables[0].targets[0]
        if t.id == owner and t.data_path == prop_path:
            hits.append(dfc)
    return hits


class SHIRO_OT_graph_unproxy(Operator):
    bl_idname = "graph.shiro_bake_back_proxy"
    bl_label = "Bake Back Proxy"
    bl_description = ("Undo a proxy channel: keys move back onto the original property, "
                      "the driver and the custom property are removed")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        fc = active_anim_fcurve(context) if _graph_poll(context) else None
        return fc is not None and split_path(fc.data_path)[2] and "." not in fc.data_path

    def execute(self, context):
        try:
            path, index = bake_back(active_anim_fcurve(context))
        except ValueError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Baked back onto {path}[{index}]")
        context.area.tag_redraw()
        return {"FINISHED"}


def bake_back(fc):
    """Undo make_proxy for proxy F-Curve `fc`. Returns (path, index). Raises ValueError."""
    _act, cb = channelbag_of(fc) if fc else (None, None)
    owner = owner_of(cb)
    if owner is None:
        raise ValueError("Could not find the animated data-block of this channel")
    drivers = find_proxy_driver(owner, fc.data_path)
    if len(drivers) != 1:
        raise ValueError(f"Expected exactly one driver using {fc.data_path}, "
                         f"found {len(drivers)}")
    dfc = drivers[0]
    path, index = dfc.data_path, dfc.array_index
    owner.driver_remove(path, index)
    back = cb.fcurves.new(path, index=index, group_name=fc.group.name if fc.group else "")
    copy_keys(fc, back)
    _parent, name, _c = split_path(fc.data_path)
    cb.fcurves.remove(fc)
    if name in owner.keys():
        del owner[name]
    return path, index


def menu_func(self, context):
    layout = self.layout
    layout.separator()
    layout.operator(SHIRO_OT_graph_rename_group.bl_idname, icon="OUTLINER_COLLECTION")
    layout.operator(SHIRO_OT_graph_batch_rename.bl_idname, icon="SORTALPHA")
    layout.operator(SHIRO_OT_graph_proxy.bl_idname, icon="DRIVER")
    layout.operator(SHIRO_OT_graph_unproxy.bl_idname, icon="LOOP_BACK")


MENUS = ("GRAPH_MT_context_menu", "GRAPH_MT_channel_context_menu")
classes = (SHIRO_OT_graph_rename_group, SHIRO_OT_graph_batch_rename, SHIRO_OT_graph_proxy,
           SHIRO_OT_graph_unproxy)
_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    for name in MENUS:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.append(menu_func)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Graph Editor", space_type="GRAPH_EDITOR")
        _keymaps.append((km, km.keymap_items.new(SHIRO_OT_graph_rename_group.bl_idname,
                                                 "F2", "PRESS")))
        _keymaps.append((km, km.keymap_items.new(SHIRO_OT_graph_proxy.bl_idname,
                                                 "F2", "PRESS", shift=True)))


def unregister():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    for name in MENUS:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            try:
                menu.remove(menu_func)
            except ValueError:
                pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
