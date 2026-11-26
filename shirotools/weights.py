"""Weights: armature -> transfer -> smooth pipeline, side flipping, weight toolkit.

Replaces VG Weight Smoother and Invert Vertex Group Sides.

Speed: the smoother used to run a Python loop per vertex per pass and write
weights back one vertex at a time. Now weights are read in one pass, smoothed
with vectorised numpy over a CSR adjacency, and written back in batches of
equal (0.001-quantised) weight, typically 100x+ faster on dense meshes.

Data compatibility: per-object state still lives in ``Object.vg_smooth_settings``.
"""

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .common import TAB, object_mode

QUANT = 1e-3       # write-back precision, invisible in practice
EPS = 0.5e-3       # below this a weight counts as zero


# ---------------------------------------------------------------------------
# numpy core (pure, tested)
# ---------------------------------------------------------------------------

def edge_array(mesh):
    e = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", e)
    return e.reshape(-1, 2)


def build_csr(edges, vert_count):
    """(dst_sorted, starts, degree) adjacency for neighbour reductions."""
    src = np.concatenate([edges[:, 0], edges[:, 1]])
    dst = np.concatenate([edges[:, 1], edges[:, 0]])
    order = np.argsort(src, kind="stable")
    src, dst = src[order], dst[order]
    deg = np.bincount(src, minlength=vert_count)
    starts = np.zeros(vert_count, dtype=np.int64)
    if vert_count > 1:
        starts[1:] = np.cumsum(deg)[:-1]
    return dst, starts, deg


def smooth(weights, csr, iterations, strength, mask=None):
    """Laplacian smoothing of a (V, G) weight matrix. `mask`: verts allowed to change."""
    dst, starts, deg = csr
    has = deg > 0
    if not has.any():
        return weights.copy()
    w = weights.astype(np.float32, copy=True)
    inv = np.zeros_like(deg, dtype=np.float32)
    inv[has] = 1.0 / deg[has]
    change = has if mask is None else (has & mask)
    for _ in range(iterations):
        nsum = np.zeros_like(w)
        nsum[has] = np.add.reduceat(w[dst], starts[has], axis=0)
        mean = nsum * inv[:, None]
        w = np.where(change[:, None], w * (1.0 - strength) + mean * strength, w)
    return np.clip(w, 0.0, 1.0)


def read_weights(mesh, group_indices):
    """(V, G) matrix of the given vertex-group indices, one pass over the mesh."""
    col = {gi: c for c, gi in enumerate(group_indices)}
    w = np.zeros((len(mesh.vertices), len(group_indices)), dtype=np.float32)
    for v in mesh.vertices:
        for g in v.groups:
            c = col.get(g.group)
            if c is not None:
                w[v.index, c] = g.weight
    return w


def write_group(vg, column, only=None):
    """Write one weight column back with as few API calls as possible."""
    idx_all = np.arange(column.size) if only is None else np.nonzero(only)[0]
    vals = column[idx_all]
    zero = idx_all[vals < EPS]
    if zero.size:
        vg.remove(zero.tolist())
    keep = vals >= EPS
    idx, vals = idx_all[keep], vals[keep]
    if not idx.size:
        return
    q = np.round(vals / QUANT).astype(np.int32)
    order = np.argsort(q, kind="stable")
    q, idx = q[order], idx[order]
    uniq, first = np.unique(q, return_index=True)
    for value, chunk in zip(uniq, np.split(idx, first[1:])):
        vg.add(chunk.tolist(), float(value) * QUANT, "REPLACE")


def weight_stats(mesh, deform_indices, limit=4):
    """Per-vertex influence counts and sums over the deform groups."""
    deform = set(deform_indices)
    n = len(mesh.vertices)
    count = np.zeros(n, dtype=np.int32)
    total = np.zeros(n, dtype=np.float32)
    for v in mesh.vertices:
        c, s = 0, 0.0
        for g in v.groups:
            if g.group in deform and g.weight >= EPS:
                c += 1
                s += g.weight
        count[v.index], total[v.index] = c, s
    return {
        "verts": n,
        "unweighted": np.nonzero(count == 0)[0],
        "over_limit": np.nonzero(count > limit)[0],
        "unnormalized": np.nonzero((count > 0) & (np.abs(total - 1.0) > 0.01))[0],
        "max_influences": int(count.max()) if n else 0,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def deform_group_indices(obj):
    arm = obj.find_armature()
    if arm is None:
        return [vg.index for vg in obj.vertex_groups]
    bones = {b.name for b in arm.data.bones if b.use_deform}
    return [vg.index for vg in obj.vertex_groups if vg.name in bones]


def select_mode(obj):
    return "BONE_DEFORM" if obj.find_armature() is not None else "ALL"


def run_on(obj, op, **kwargs):
    with bpy.context.temp_override(object=obj, active_object=obj):
        return op(**kwargs)


def flip_names(obj):
    renames = [(vg, bpy.utils.flip_name(vg.name)) for vg in obj.vertex_groups]
    renames = [(vg, new) for vg, new in renames if new != vg.name]
    for i, (vg, _new) in enumerate(renames):
        vg.name = f"__shiro_flip_{i}__"
    for vg, new in renames:
        vg.name = new
    return len(renames)


def sync_list(settings, obj):
    enabled = {i.name: i.enabled for i in settings.groups}
    settings.groups.clear()
    for vg in obj.vertex_groups:
        item = settings.groups.add()
        item.name = vg.name
        item.enabled = enabled.get(vg.name, True)


def get_s(context):
    obj = context.active_object
    return obj.vg_smooth_settings if obj is not None and obj.type == "MESH" else None


_check = {}  # session_uid -> stats


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class SHIRO_PG_vg_item(PropertyGroup):
    name: StringProperty(name="Group Name")
    enabled: BoolProperty(name="Include", default=True)


class SHIRO_PG_vg_settings(PropertyGroup):
    stage: EnumProperty(items=[("PARENT", "Parent", ""), ("TRANSFER", "Transfer", ""),
                               ("SMOOTH", "Smooth", "")], default="PARENT")
    armature_ob: PointerProperty(name="Armature", type=bpy.types.Object,
                                 poll=lambda s, o: o.type == "ARMATURE")
    donor_ob: PointerProperty(name="Donor Mesh", type=bpy.types.Object,
                              poll=lambda s, o: o.type == "MESH")
    groups: CollectionProperty(type=SHIRO_PG_vg_item)
    active_group_index: IntProperty(default=0)
    iterations: IntProperty(name="Iterations", default=5, min=1, max=200)
    strength: FloatProperty(name="Strength", default=0.5, min=0.0, max=1.0, subtype="FACTOR")
    # new
    deform_only: BoolProperty(name="Deform Bones Only", default=True,
                              description="Only create groups for bones with Deform enabled")
    mapping: EnumProperty(name="Mapping", items=[
        ("POLYINTERP_NEAREST", "Nearest Face Interpolated", "Best default for clothes / props"),
        ("NEAREST", "Nearest Vertex", "Copies one donor vertex, can look blocky"),
        ("POLYINTERP_VNORPROJ", "Projected Face Interpolated", "Along the target normals"),
    ], default="POLYINTERP_NEAREST")
    only_selected: BoolProperty(name="Selected Vertices Only", default=False,
                                description="Only change vertices selected in Edit Mode")
    normalize_after: BoolProperty(name="Normalize", default=True,
                                  description="Normalize deform weights after smoothing")
    limit_after: IntProperty(name="Limit Influences", default=0, min=0, max=16,
                             description="Max groups per vertex after smoothing (0 = off, "
                                         "Unity default skin weights = 4)")


# ---------------------------------------------------------------------------
# pipeline operators
# ---------------------------------------------------------------------------

class SHIRO_OT_vg_parent(Operator):
    bl_idname = "shiro.vg_parent"
    bl_label = "Apply Parenting"
    bl_description = "Parent the mesh to the armature (keep transform) with empty bone groups"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and s.armature_ob is not None

    def execute(self, context):
        tgt = context.active_object
        s = tgt.vg_smooth_settings
        arm = s.armature_ob
        existing = {vg.name for vg in tgt.vertex_groups}
        made = 0
        for bone in arm.data.bones:
            if s.deform_only and not bone.use_deform:
                continue
            if bone.name not in existing:
                tgt.vertex_groups.new(name=bone.name)
                made += 1
        mw = tgt.matrix_world.copy()
        tgt.parent = arm
        tgt.parent_type = "OBJECT"
        tgt.matrix_parent_inverse = arm.matrix_world.inverted()
        tgt.matrix_world = mw
        for mod in [m for m in tgt.modifiers if m.type == "ARMATURE"]:
            tgt.modifiers.remove(mod)
        mod = tgt.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = arm
        mod.use_vertex_groups = True
        self.report({"INFO"}, f"Parented to '{arm.name}', {made} new group(s)")
        s.stage = "TRANSFER"
        return {"FINISHED"}


class SHIRO_OT_vg_transfer(Operator):
    bl_idname = "shiro.vg_transfer"
    bl_label = "Transfer Weights"
    bl_description = "Copy vertex group weights from the donor mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and s.donor_ob is not None and s.donor_ob != context.active_object

    def execute(self, context):
        tgt = context.active_object
        s = tgt.vg_smooth_settings
        with object_mode(context):
            mod = tgt.modifiers.new(name="_shiro_vg_transfer", type="DATA_TRANSFER")
            mod.object = s.donor_ob
            mod.use_vert_data = True
            mod.data_types_verts = {"VGROUP_WEIGHTS"}
            mod.vert_mapping = s.mapping
            mod.layers_vgroup_select_src = "ALL"
            mod.layers_vgroup_select_dst = "NAME"
            mod.use_max_distance = False
            try:
                with context.temp_override(object=tgt, active_object=tgt):
                    # first in the stack, otherwise apply evaluates on the wrong mesh
                    bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=0)
                    bpy.ops.object.modifier_apply(modifier=mod.name)
            except RuntimeError as err:
                if mod.name in tgt.modifiers:
                    tgt.modifiers.remove(mod)
                self.report({"ERROR"}, f"Transfer failed: {err}")
                return {"CANCELLED"}
        sync_list(s, tgt)
        s.stage = "SMOOTH"
        self.report({"INFO"}, f"Weights transferred from '{s.donor_ob.name}'")
        return {"FINISHED"}


class SHIRO_OT_vg_smooth(Operator):
    bl_idname = "shiro.vg_smooth"
    bl_label = "Apply Smoothing"
    bl_description = "Laplacian-smooth the ticked vertex groups"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and len(s.groups) > 0

    def execute(self, context):
        obj = context.active_object
        s = obj.vg_smooth_settings
        names = {i.name for i in s.groups if i.enabled}
        groups = [vg for vg in obj.vertex_groups if vg.name in names and not vg.lock_weight]
        if not groups:
            self.report({"WARNING"}, "No unlocked groups ticked")
            return {"CANCELLED"}
        with object_mode(context):
            mesh = obj.data
            mask = None
            if s.only_selected:
                mask = np.zeros(len(mesh.vertices), dtype=bool)
                mesh.vertices.foreach_get("select", mask)
                if not mask.any():
                    self.report({"WARNING"}, "No vertices selected")
                    return {"CANCELLED"}
            csr = build_csr(edge_array(mesh), len(mesh.vertices))
            w = read_weights(mesh, [vg.index for vg in groups])
            sw = smooth(w, csr, s.iterations, s.strength, mask)
            for c, vg in enumerate(groups):
                write_group(vg, sw[:, c], mask)
            if s.limit_after:  # limit first, normalizing afterwards keeps sums at 1
                run_on(obj, bpy.ops.object.vertex_group_limit_total,
                       group_select_mode=select_mode(obj), limit=s.limit_after)
            if s.normalize_after:
                run_on(obj, bpy.ops.object.vertex_group_normalize_all,
                       group_select_mode=select_mode(obj), lock_active=False)
            mesh.update()
        _check.pop(obj.session_uid, None)
        self.report({"INFO"}, f"Smoothed {len(groups)} group(s) × {s.iterations}")
        return {"FINISHED"}


def remove_empty_groups(obj, names=None, threshold=0.0):
    used = set()
    for v in obj.data.vertices:
        for g in v.groups:
            if g.weight > threshold:
                used.add(g.group)
    doomed = [vg for vg in obj.vertex_groups
              if vg.index not in used and (names is None or vg.name in names)
              and not vg.lock_weight]
    removed = [vg.name for vg in doomed]
    for vg in doomed:
        obj.vertex_groups.remove(vg)
    return removed


class SHIRO_OT_vg_purge(Operator):
    bl_idname = "shiro.vg_purge"
    bl_label = "Purge Empty Groups"
    bl_description = "Delete ticked groups that have no weights"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and len(s.groups) > 0

    def execute(self, context):
        obj = context.active_object
        s = obj.vg_smooth_settings
        with object_mode(context):
            removed = remove_empty_groups(obj, {i.name for i in s.groups if i.enabled})
        sync_list(s, obj)
        self.report({"INFO"}, f"Removed {len(removed)} empty group(s)")
        return {"FINISHED"}


class SHIRO_OT_vg_scan(Operator):
    bl_idname = "shiro.vg_scan"
    bl_label = "Scan Vertex Groups"
    bl_description = "List this mesh's vertex groups and go to the smoothing stage"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_s(context) is not None

    def execute(self, context):
        obj = context.active_object
        if not obj.vertex_groups:
            self.report({"WARNING"}, "No vertex groups")
            return {"CANCELLED"}
        sync_list(obj.vg_smooth_settings, obj)
        obj.vg_smooth_settings.stage = "SMOOTH"
        return {"FINISHED"}


class SHIRO_OT_vg_toggle_all(Operator):
    bl_idname = "shiro.vg_toggle_all"
    bl_label = "Toggle All"
    bl_options = {"REGISTER", "UNDO"}

    enable: BoolProperty(default=True)
    deform_only: BoolProperty(default=False)

    def execute(self, context):
        s = get_s(context)
        if s is None:
            return {"CANCELLED"}
        obj = context.active_object
        deform = {obj.vertex_groups[i].name for i in deform_group_indices(obj)}
        for item in s.groups:
            item.enabled = self.enable and (not self.deform_only or item.name in deform)
        return {"FINISHED"}


class SHIRO_OT_vg_reset(Operator):
    bl_idname = "shiro.vg_reset"
    bl_label = "Start Over"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = get_s(context)
        if s is None:
            return {"CANCELLED"}
        s.stage = "PARENT"
        s.armature_ob = s.donor_ob = None
        s.groups.clear()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# toolkit operators (all selected meshes)
# ---------------------------------------------------------------------------

def _meshes(context):
    objs = [o for o in context.selected_objects if o.type == "MESH"]
    if not objs and context.active_object and context.active_object.type == "MESH":
        objs = [context.active_object]
    return objs


class SHIRO_OT_wt_check(Operator):
    bl_idname = "shiro.wt_check"
    bl_label = "Check Weights"
    bl_description = "Count unweighted, over-limit and unnormalized vertices"

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            return {"CANCELLED"}
        with object_mode(context):
            limit = context.scene.shiro_wt_limit
            _check[obj.session_uid] = weight_stats(obj.data, deform_group_indices(obj), limit)
        return {"FINISHED"}


class SHIRO_OT_wt_select(Operator):
    bl_idname = "shiro.wt_select"
    bl_label = "Select Problem Vertices"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(items=[("unweighted", "Unweighted", ""), ("over_limit", "Over Limit", ""),
                              ("unnormalized", "Unnormalized", "")])

    def execute(self, context):
        obj = context.active_object
        stats = _check.get(obj.session_uid) if obj else None
        if stats is None:
            return {"CANCELLED"}
        with object_mode(context):
            sel = np.zeros(len(obj.data.vertices), dtype=bool)
            idx = stats[self.kind]
            sel[idx[idx < sel.size]] = True
            obj.data.vertices.foreach_set("select", sel)
            obj.data.update()
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="VERT")
        return {"FINISHED"}


WT_ACTIONS = [
    ("LIMIT", "Limit + Normalize", "Keep the strongest N influences, then normalize"),
    ("NORMALIZE", "Normalize All", "Deform weights sum to 1"),
    ("CLEAN", "Clean", "Remove weights below the threshold"),
    ("MIRROR", "Mirror All Groups", "Mirror weights and swap L/R groups (X axis)"),
    ("FLIP_NAMES", "Invert Sides", "Swap .L/.R, _L/_R, Left/Right ... in group names"),
    ("ADD_MIRRORED", "Add Missing Mirror Groups", "Create empty counterparts for sided groups"),
    ("REMOVE_EMPTY", "Remove Empty Groups", "Delete groups without weights"),
    ("REMOVE_NONBONE", "Remove Non-Bone Groups", "Delete groups that match no armature bone"),
]


class SHIRO_OT_wt_op(Operator):
    bl_idname = "shiro.wt_op"
    bl_label = "Weight Tool"
    bl_description = "Run a weight cleanup on every selected mesh"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(items=WT_ACTIONS)

    def execute(self, context):
        sc = context.scene
        total = 0
        with object_mode(context):
            for obj in _meshes(context):
                mode = select_mode(obj)
                if self.action == "LIMIT":
                    run_on(obj, bpy.ops.object.vertex_group_limit_total,
                           group_select_mode=mode, limit=sc.shiro_wt_limit)
                    run_on(obj, bpy.ops.object.vertex_group_normalize_all,
                           group_select_mode=mode, lock_active=False)
                elif self.action == "NORMALIZE":
                    run_on(obj, bpy.ops.object.vertex_group_normalize_all,
                           group_select_mode=mode, lock_active=False)
                elif self.action == "CLEAN":
                    run_on(obj, bpy.ops.object.vertex_group_clean, group_select_mode="ALL",
                           limit=sc.shiro_wt_clean, keep_single=True)
                elif self.action == "MIRROR":
                    if obj.vertex_groups:
                        run_on(obj, bpy.ops.object.vertex_group_mirror, mirror_weights=True,
                               flip_group_names=True, all_groups=True, use_topology=False)
                elif self.action == "FLIP_NAMES":
                    total += flip_names(obj)
                elif self.action == "ADD_MIRRORED":
                    names = {vg.name for vg in obj.vertex_groups}
                    for name in list(names):
                        other = bpy.utils.flip_name(name)
                        if other != name and other not in names:
                            obj.vertex_groups.new(name=other)
                            names.add(other)
                            total += 1
                elif self.action == "REMOVE_EMPTY":
                    total += len(remove_empty_groups(obj))
                elif self.action == "REMOVE_NONBONE":
                    arm = obj.find_armature()
                    if arm is None:
                        continue
                    bones = {b.name for b in arm.data.bones}
                    for vg in [v for v in obj.vertex_groups if v.name not in bones]:
                        obj.vertex_groups.remove(vg)
                        total += 1
                _check.pop(obj.session_uid, None)
                s = obj.vg_smooth_settings
                if s.groups:
                    sync_list(s, obj)
        label = next(name for key, name, _d in WT_ACTIONS if key == self.action)
        self.report({"INFO"}, f"{label}: done" + (f" ({total})" if total else ""))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class SHIRO_UL_vg(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        vg = context.active_object.vertex_groups.get(item.name)
        row.label(text=item.name, icon="LOCKED" if vg and vg.lock_weight else "GROUP_VERTEX")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = bpy.types.UI_UL_list.filter_items_by_name(
            self.filter_name, self.bitflag_filter_item, items, "name")
        return flags or [self.bitflag_filter_item] * len(items), []


class _WeightsPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"


class SHIRO_PT_weights(_WeightsPanel, Panel):
    bl_idname = "SHIRO_PT_weights"
    bl_label = "Weights"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pass


class SHIRO_PT_weights_pipeline(_WeightsPanel, Panel):
    bl_idname = "SHIRO_PT_weights_pipeline"
    bl_label = "Rig & Smooth"
    bl_parent_id = "SHIRO_PT_weights"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        s = obj.vg_smooth_settings
        row = layout.row(align=True)
        for key, label, icon in (("PARENT", "1 Parent", "ARMATURE_DATA"),
                                 ("TRANSFER", "2 Transfer", "MOD_DATA_TRANSFER"),
                                 ("SMOOTH", "3 Smooth", "MOD_SMOOTH")):
            sub = row.row(align=True)
            sub.alert = s.stage == key
            sub.prop_enum(s, "stage", key, text=label, icon=icon)

        if s.stage == "PARENT":
            box = layout.box()
            box.prop(s, "armature_ob", text="")
            box.prop(s, "deform_only")
            big = box.row()
            big.scale_y = 1.4
            big.operator("shiro.vg_parent", icon="LINKED", text="Parent  →")
            layout.operator("shiro.vg_scan", text="Skip, already rigged", icon="FORWARD")
        elif s.stage == "TRANSFER":
            box = layout.box()
            box.prop(s, "donor_ob", text="Donor")
            box.prop(s, "mapping", text="")
            if s.donor_ob is not None and s.donor_ob == obj:
                box.label(text="Donor and target are the same", icon="ERROR")
            big = box.row()
            big.scale_y = 1.4
            big.operator("shiro.vg_transfer", icon="MOD_DATA_TRANSFER", text="Transfer  →")
            layout.operator("shiro.vg_scan", text="Skip to Smooth", icon="FORWARD")
        else:
            row = layout.row(align=True)
            row.operator("shiro.vg_scan", text="Re-scan", icon="FILE_REFRESH")
            row.operator("shiro.vg_reset", text="", icon="LOOP_BACK")
            if not s.groups:
                layout.label(text="No groups, press Re-scan", icon="INFO")
                return
            layout.template_list("SHIRO_UL_vg", "", s, "groups", s, "active_group_index",
                                 rows=6)
            row = layout.row(align=True)
            op = row.operator("shiro.vg_toggle_all", text="All", icon="CHECKBOX_HLT")
            op.enable, op.deform_only = True, False
            op = row.operator("shiro.vg_toggle_all", text="Deform", icon="BONE_DATA")
            op.enable, op.deform_only = True, True
            op = row.operator("shiro.vg_toggle_all", text="None", icon="CHECKBOX_DEHLT")
            op.enable = False
            col = layout.column(align=True)
            col.prop(s, "iterations")
            col.prop(s, "strength", slider=True)
            col.prop(s, "only_selected")
            row = col.row(align=True)
            row.prop(s, "normalize_after", toggle=True)
            row.prop(s, "limit_after")
            big = layout.row(align=True)
            big.scale_y = 1.4
            big.operator("shiro.vg_smooth", icon="PLAY")
            big.operator("shiro.vg_purge", text="", icon="TRASH")


class SHIRO_PT_weights_toolkit(_WeightsPanel, Panel):
    bl_idname = "SHIRO_PT_weights_toolkit"
    bl_label = "Toolkit"
    bl_parent_id = "SHIRO_PT_weights"

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        obj = context.active_object
        stats = _check.get(obj.session_uid)
        row = layout.row(align=True)
        row.operator("shiro.wt_check", icon="VIEWZOOM")
        row.prop(sc, "shiro_wt_limit", text="Max")
        if stats is not None:
            box = layout.box()
            box.label(text=f"{stats['verts']} verts · max {stats['max_influences']} influences")
            for kind, label in (("unweighted", "Unweighted"), ("over_limit", "Over limit"),
                                ("unnormalized", "Not normalized")):
                n = len(stats[kind])
                r = box.row(align=True)
                r.alert = n > 0
                r.label(text=f"{label}: {n}", icon="ERROR" if n else "CHECKMARK")
                if n:
                    r.operator("shiro.wt_select", text="", icon="RESTRICT_SELECT_OFF").kind = kind
        col = layout.column(align=True)
        col.operator("shiro.wt_op", text="Limit + Normalize", icon="SORTSIZE").action = "LIMIT"
        col.operator("shiro.wt_op", text="Normalize", icon="NORMALIZE_FCURVES").action = \
            "NORMALIZE"
        row = col.row(align=True)
        row.operator("shiro.wt_op", text="Clean", icon="BRUSH_DATA").action = "CLEAN"
        row.prop(sc, "shiro_wt_clean", text="")
        col = layout.column(align=True)
        col.operator("shiro.wt_op", text="Mirror All Groups", icon="MOD_MIRROR").action = "MIRROR"
        col.operator("shiro.wt_op", text="Invert Sides (names)",
                     icon="ARROW_LEFTRIGHT").action = "FLIP_NAMES"
        col.operator("shiro.wt_op", text="Add Missing Mirror Groups",
                     icon="ADD").action = "ADD_MIRRORED"
        col = layout.column(align=True)
        col.operator("shiro.wt_op", text="Remove Empty Groups", icon="TRASH").action = \
            "REMOVE_EMPTY"
        col.operator("shiro.wt_op", text="Remove Non-Bone Groups", icon="BONE_DATA").action = \
            "REMOVE_NONBONE"


def vg_menu(self, context):
    self.layout.separator()
    self.layout.operator("shiro.wt_op", text="Invert Sides (Shiro)",
                         icon="ARROW_LEFTRIGHT").action = "FLIP_NAMES"


classes = (SHIRO_PG_vg_item, SHIRO_PG_vg_settings, SHIRO_OT_vg_parent, SHIRO_OT_vg_transfer,
           SHIRO_OT_vg_smooth, SHIRO_OT_vg_purge, SHIRO_OT_vg_scan, SHIRO_OT_vg_toggle_all,
           SHIRO_OT_vg_reset, SHIRO_OT_wt_check, SHIRO_OT_wt_select, SHIRO_OT_wt_op,
           SHIRO_UL_vg, SHIRO_PT_weights, SHIRO_PT_weights_pipeline, SHIRO_PT_weights_toolkit)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.vg_smooth_settings = PointerProperty(type=SHIRO_PG_vg_settings)
    bpy.types.Scene.shiro_wt_limit = IntProperty(name="Max Influences", default=4, min=1, max=16)
    bpy.types.Scene.shiro_wt_clean = FloatProperty(name="Threshold", default=0.01, min=0.0,
                                                   max=0.5, precision=3)
    bpy.types.MESH_MT_vertex_group_context_menu.append(vg_menu)


def unregister():
    bpy.types.MESH_MT_vertex_group_context_menu.remove(vg_menu)
    del bpy.types.Scene.shiro_wt_clean
    del bpy.types.Scene.shiro_wt_limit
    del bpy.types.Object.vg_smooth_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _check.clear()
