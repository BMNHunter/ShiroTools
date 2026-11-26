"""
Vertex Group Weight Smoother — with Auto-Rig & Transfer Pipeline
Blender 4.0+ Addon

Per-object state: each mesh object stores its own wizard stage and parameters.
Switching the active object switches the panel context automatically.

Stage 1 – Parent:   Pick armature → parent active mesh with empty vertex groups
Stage 2 – Transfer: Pick a donor mesh → transfer weights via nearest-vertex
Stage 3 – Smooth:   Iterative Laplacian smoothing on selected groups + purge
"""

bl_info = {
    "name": "VG Weight Smoother",
    "author": "Claude",
    "version": (3, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > VG Smoother",
    "description": (
        "Per-object 3-stage pipeline: parent to armature, "
        "transfer weights from donor, then smooth"
    ),
    "category": "Mesh",
}

import bpy
import bmesh
import numpy as np
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    CollectionProperty,
    FloatProperty,
    EnumProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup, Panel, Operator, UIList


# ============================================================================
# Property Groups  (stored on each Object, not on the Scene)
# ============================================================================

class VGSmoothItem(PropertyGroup):
    """One row in the vertex-group list."""
    name:    StringProperty(name="Group Name")
    enabled: BoolProperty(name="Include", default=True)


class VGSmoothSettings(PropertyGroup):
    """All wizard state for one mesh object."""

    stage: EnumProperty(
        items=[
            ("PARENT",   "Parent",   "Set up armature parenting"),
            ("TRANSFER", "Transfer", "Transfer weights from donor"),
            ("SMOOTH",   "Smooth",   "Smooth and finalise weights"),
        ],
        default="PARENT",
    )

    # Stage 1 — armature to parent this object to
    armature_ob: PointerProperty(
        name="Armature",
        description="Armature object to parent this mesh to",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "ARMATURE",
    )

    # Stage 2 — donor mesh to copy weights from
    donor_ob: PointerProperty(
        name="Donor Mesh",
        description="Mesh to inherit vertex-group weights from",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "MESH",
    )

    # Stage 3 — group list + smoothing params
    groups:             CollectionProperty(type=VGSmoothItem)
    active_group_index: IntProperty(default=0)

    iterations: IntProperty(
        name="Iterations",
        description="Number of Laplacian smoothing passes",
        default=5, min=1, max=100,
    )
    strength: FloatProperty(
        name="Strength",
        description="Blend factor toward neighbour average per pass (0–1)",
        default=0.5, min=0.0, max=1.0,
    )


# ============================================================================
# Helper — get settings for the active mesh object (or None)
# ============================================================================

def get_s(context):
    """Return the VGSmoothSettings for the active mesh, or None."""
    obj = context.active_object
    if obj and obj.type == "MESH":
        return obj.vg_smooth_settings
    return None


# ============================================================================
# Core smoothing math
# ============================================================================

def build_adjacency(bm):
    adj = [[] for _ in range(len(bm.verts))]
    bm.verts.ensure_lookup_table()
    for v in bm.verts:
        for e in v.link_edges:
            adj[v.index].append(e.other_vert(v).index)
    return adj


def smooth_weights(weights, adjacency, iterations, strength):
    w = weights.copy()
    for _ in range(iterations):
        nw = w.copy()
        for i, nbrs in enumerate(adjacency):
            if nbrs:
                nw[i] = w[i] * (1.0 - strength) + np.mean(w[nbrs]) * strength
        w = nw
    return np.clip(w, 0.0, 1.0)


# ============================================================================
# Stage 1 – Parent active mesh to armature with empty vertex groups
# ============================================================================

class VGSMOOTHER_OT_parent(Operator):
    """Parent the active mesh to the chosen armature with empty vertex groups
    (one group per bone). Uses Keep Transform."""
    bl_idname  = "vgsmoother.parent"
    bl_label   = "Apply Parenting"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and s.armature_ob is not None

    def execute(self, context):
        tgt = context.active_object
        s   = tgt.vg_smooth_settings
        arm = s.armature_ob

        if arm is None or arm.type != "ARMATURE":
            self.report({"ERROR"}, "No valid armature selected.")
            return {"CANCELLED"}

        # Create one empty vertex group per bone
        existing = {vg.name for vg in tgt.vertex_groups}
        for bone in arm.data.bones:
            if bone.name not in existing:
                tgt.vertex_groups.new(name=bone.name)

        # Parent with Keep Transform
        world_matrix = tgt.matrix_world.copy()
        tgt.parent       = arm
        tgt.parent_type  = "OBJECT"
        tgt.matrix_parent_inverse = arm.matrix_world.inverted()
        tgt.matrix_world = world_matrix

        # Add / replace Armature modifier
        for mod in list(tgt.modifiers):
            if mod.type == "ARMATURE":
                tgt.modifiers.remove(mod)
        mod = tgt.modifiers.new(name="Armature", type="ARMATURE")
        mod.object            = arm
        mod.use_vertex_groups = True

        self.report(
            {"INFO"},
            f"Parented '{tgt.name}' to '{arm.name}' "
            f"with {len(arm.data.bones)} empty vertex groups.",
        )
        s.stage = "TRANSFER"
        return {"FINISHED"}


# ============================================================================
# Stage 2 – Transfer weights from donor via nearest-vertex
# ============================================================================

class VGSMOOTHER_OT_transfer(Operator):
    """Transfer vertex group weights from the donor mesh to the active mesh
    using nearest-vertex mapping (Data Transfer modifier method)."""
    bl_idname  = "vgsmoother.transfer"
    bl_label   = "Transfer Weights"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return (
            s is not None
            and s.donor_ob is not None
            and s.donor_ob is not context.active_object
        )

    def execute(self, context):
        tgt   = context.active_object
        s     = tgt.vg_smooth_settings
        donor = s.donor_ob

        if donor is None or donor.type != "MESH":
            self.report({"ERROR"}, "No valid donor mesh selected.")
            return {"CANCELLED"}
        if donor is tgt:
            self.report({"ERROR"}, "Donor and target must be different objects.")
            return {"CANCELLED"}

        # Add Data Transfer modifier, apply, remove
        mod = tgt.modifiers.new(name="_vg_transfer_tmp", type="DATA_TRANSFER")
        mod.object           = donor
        mod.use_vert_data    = True
        mod.data_types_verts = {"VGROUP_WEIGHTS"}
        mod.vert_mapping     = "NEAREST"
        mod.use_max_distance = False

        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = tgt

        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except RuntimeError as exc:
            tgt.modifiers.remove(mod)
            context.view_layer.objects.active = prev_active
            self.report({"ERROR"}, f"Modifier apply failed: {exc}")
            return {"CANCELLED"}

        context.view_layer.objects.active = prev_active

        self.report(
            {"INFO"},
            f"Transferred weights from '{donor.name}' → '{tgt.name}' "
            "(nearest vertex).",
        )

        # Advance to Stage 3, pre-populate group list
        s.stage = "SMOOTH"
        s.groups.clear()
        for vg in tgt.vertex_groups:
            item         = s.groups.add()
            item.name    = vg.name
            item.enabled = True

        return {"FINISHED"}


# ============================================================================
# Stage 3a – Apply Laplacian weight smoothing
# ============================================================================

class VGSMOOTHER_OT_apply(Operator):
    """Apply Laplacian weight smoothing to all toggled-on vertex groups."""
    bl_idname  = "vgsmoother.apply"
    bl_label   = "Apply Smoothing"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and len(s.groups) > 0

    def execute(self, context):
        obj  = context.active_object
        s    = obj.vg_smooth_settings
        mesh = obj.data

        target_names = {item.name for item in s.groups if item.enabled}
        if not target_names:
            self.report({"WARNING"}, "No vertex groups are toggled on.")
            return {"CANCELLED"}

        bm = bmesh.new()
        bm.from_mesh(mesh)
        adj = build_adjacency(bm)
        bm.free()

        vc = len(mesh.vertices)
        processed = 0

        for vg in obj.vertex_groups:
            if vg.name not in target_names:
                continue
            w = np.zeros(vc, dtype=np.float64)
            for v in mesh.vertices:
                for g in v.groups:
                    if g.group == vg.index:
                        w[v.index] = g.weight
                        break
            sw = smooth_weights(w, adj, s.iterations, s.strength)
            for i in range(vc):
                val = float(sw[i])
                if val > 0.0:
                    vg.add([i], val, "REPLACE")
                else:
                    try:
                        vg.remove([i])
                    except RuntimeError:
                        pass
            processed += 1

        mesh.update()
        self.report(
            {"INFO"},
            f"Smoothed {processed} group(s) over {s.iterations} iterations.",
        )
        return {"FINISHED"}


# ============================================================================
# Stage 3b – Purge empty vertex groups
# ============================================================================

class VGSMOOTHER_OT_purge_empty(Operator):
    """Delete toggled-on vertex groups whose total weight is zero."""
    bl_idname  = "vgsmoother.purge_empty"
    bl_label   = "Purge Empty Groups"
    bl_options = {"REGISTER", "UNDO"}

    threshold: FloatProperty(
        name="Zero Threshold",
        default=0.0, min=0.0, max=0.01, precision=4,
    )

    @classmethod
    def poll(cls, context):
        s = get_s(context)
        return s is not None and len(s.groups) > 0

    def execute(self, context):
        obj  = context.active_object
        s    = obj.vg_smooth_settings
        mesh = obj.data

        target_names = {item.name for item in s.groups if item.enabled}
        if not target_names:
            self.report({"WARNING"}, "No groups toggled on.")
            return {"CANCELLED"}

        weight_sum = {vg.index: 0.0 for vg in obj.vertex_groups
                      if vg.name in target_names}
        for v in mesh.vertices:
            for g in v.groups:
                if g.group in weight_sum:
                    weight_sum[g.group] += g.weight

        to_delete = [
            vg.name for vg in obj.vertex_groups
            if vg.name in target_names
            and weight_sum.get(vg.index, 0.0) <= self.threshold
        ]
        if not to_delete:
            self.report({"INFO"}, "No empty groups found.")
            return {"FINISHED"}

        for name in to_delete:
            vg = obj.vertex_groups.get(name)
            if vg:
                obj.vertex_groups.remove(vg)

        s.groups.clear()
        for vg in obj.vertex_groups:
            item         = s.groups.add()
            item.name    = vg.name
            item.enabled = True

        mesh.update()
        self.report(
            {"INFO"},
            f"Deleted {len(to_delete)} empty group(s): {', '.join(to_delete)}",
        )
        return {"FINISHED"}


# ============================================================================
# Manual scan — jump to Stage 3 for the active mesh
# ============================================================================

class VGSMOOTHER_OT_scan(Operator):
    """Scan the active mesh's vertex groups and jump directly to Stage 3."""
    bl_idname  = "vgsmoother.scan"
    bl_label   = "Scan Vertex Groups"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        s   = obj.vg_smooth_settings

        if not obj.vertex_groups:
            self.report({"WARNING"}, "Object has no vertex groups.")
            return {"CANCELLED"}

        s.groups.clear()
        for vg in obj.vertex_groups:
            item         = s.groups.add()
            item.name    = vg.name
            item.enabled = True

        s.stage = "SMOOTH"
        self.report({"INFO"}, f"Scanned {len(s.groups)} vertex groups.")
        return {"FINISHED"}


# ============================================================================
# Misc operators
# ============================================================================

class VGSMOOTHER_OT_toggle_all(Operator):
    bl_idname = "vgsmoother.toggle_all"
    bl_label  = "Toggle All"
    enable: BoolProperty(default=True)

    def execute(self, context):
        s = get_s(context)
        if s:
            for item in s.groups:
                item.enabled = self.enable
        return {"FINISHED"}


class VGSMOOTHER_OT_reset(Operator):
    """Reset the wizard for the active object back to Stage 1."""
    bl_idname  = "vgsmoother.reset"
    bl_label   = "Start Over"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_s(context) is not None

    def execute(self, context):
        s = context.active_object.vg_smooth_settings
        s.stage       = "PARENT"
        s.armature_ob = None
        s.donor_ob    = None
        s.groups.clear()
        return {"FINISHED"}


# ============================================================================
# UI List
# ============================================================================

class VGSMOOTHER_UL_groups(UIList):
    bl_idname = "VGSMOOTHER_UL_groups"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.label(text=item.name, icon="GROUP_VERTEX")


# ============================================================================
# Panel
# ============================================================================

class VGSMOOTHER_PT_main(Panel):
    bl_label       = "VG Weight Smoother"
    bl_idname      = "VGSMOOTHER_PT_main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "VG Smoother"

    def draw(self, context):
        layout = self.layout
        obj    = context.active_object

        # ── Guard: need an active mesh ───────────────────────────────────
        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh object", icon="ERROR")
            return

        s = obj.vg_smooth_settings

        # ── Active object label ──────────────────────────────────────────
        header = layout.box()
        header.label(text=f"Object: {obj.name}", icon="MESH_DATA")

        # ── Stage breadcrumb ─────────────────────────────────────────────
        row = layout.row(align=True)
        row.alert = (s.stage == "PARENT")
        row.label(
            text="① Parent",
            icon="ARMATURE_DATA" if s.stage == "PARENT" else "CHECKMARK",
        )
        row.alert = (s.stage == "TRANSFER")
        row.label(
            text="② Transfer",
            icon="MOD_DATA_TRANSFER" if s.stage == "TRANSFER"
                 else ("CHECKMARK" if s.stage == "SMOOTH" else "BLANK1"),
        )
        row.alert = (s.stage == "SMOOTH")
        row.label(
            text="③ Smooth",
            icon="MOD_SMOOTH" if s.stage == "SMOOTH" else "BLANK1",
        )
        layout.separator()

        # ════════════════════════════════════════════════════════════════
        # STAGE 1 – PARENT
        # ════════════════════════════════════════════════════════════════
        if s.stage == "PARENT":
            box = layout.box()
            box.label(text="Step 1 — Parent to Armature", icon="ARMATURE_DATA")
            box.label(text="Armature:")
            box.prop(s, "armature_ob", text="")

            box.separator()
            btn = box.row()
            btn.enabled = s.armature_ob is not None
            btn.scale_y = 1.4
            btn.operator("vgsmoother.parent", icon="LINKED",
                         text="Apply Parenting  →")

            layout.separator()
            row = layout.row()
            row.alignment = "RIGHT"
            row.operator("vgsmoother.scan", text="Skip → already rigged",
                         icon="FORWARD")

        # ════════════════════════════════════════════════════════════════
        # STAGE 2 – TRANSFER
        # ════════════════════════════════════════════════════════════════
        elif s.stage == "TRANSFER":
            box = layout.box()
            box.label(text="Step 2 — Transfer Weights", icon="MOD_DATA_TRANSFER")
            box.label(text=f"Target: {obj.name}", icon="MESH_DATA")
            box.separator(factor=0.4)

            box.label(text="Donor Mesh (copy weights from):")
            box.prop(s, "donor_ob", text="")
            if s.donor_ob is not None and s.donor_ob is obj:
                box.label(text="Donor and target are the same", icon="ERROR")

            box.separator(factor=0.4)
            box.label(text="Mapping: Nearest Vertex", icon="VERTEXSEL")
            box.separator()

            can_transfer = (
                s.donor_ob is not None
                and s.donor_ob is not obj
            )
            btn = box.row()
            btn.enabled = can_transfer
            btn.scale_y = 1.4
            btn.operator("vgsmoother.transfer", icon="MOD_DATA_TRANSFER",
                         text="Transfer Weights  →")

            layout.separator()
            layout.operator("vgsmoother.reset", icon="LOOP_BACK",
                            text="← Back to Stage 1")

        # ════════════════════════════════════════════════════════════════
        # STAGE 3 – SMOOTH
        # ════════════════════════════════════════════════════════════════
        elif s.stage == "SMOOTH":
            layout.operator("vgsmoother.scan", icon="FILE_REFRESH",
                            text="Re-scan Vertex Groups")
            layout.separator()

            if s.groups:
                layout.template_list(
                    "VGSMOOTHER_UL_groups", "",
                    s, "groups",
                    s, "active_group_index",
                    rows=6,
                )
                row = layout.row(align=True)
                op = row.operator("vgsmoother.toggle_all",
                                  text="All On", icon="CHECKMARK")
                op.enable = True
                op = row.operator("vgsmoother.toggle_all",
                                  text="All Off", icon="X")
                op.enable = False

                layout.separator()
                cbox = layout.box()
                cbox.label(text="Cleanup", icon="TRASH")
                cbox.operator(
                    "vgsmoother.purge_empty", icon="X",
                    text="Purge Empty Groups (toggled-on)",
                )

                layout.separator()
                sbox = layout.box()
                sbox.label(text="Smoothing Settings", icon="MOD_SMOOTH")
                sbox.prop(s, "iterations")
                sbox.prop(s, "strength")

                layout.separator()
                btn = layout.row()
                btn.scale_y = 1.4
                btn.operator("vgsmoother.apply", icon="PLAY",
                             text="Apply Smoothing")
            else:
                layout.label(text="No groups — press Re-scan.", icon="INFO")

            layout.separator()
            layout.operator("vgsmoother.reset", icon="LOOP_BACK",
                            text="← Start Over")


# ============================================================================
# Registration
# ============================================================================

classes = (
    VGSmoothItem,
    VGSmoothSettings,
    VGSMOOTHER_OT_parent,
    VGSMOOTHER_OT_transfer,
    VGSMOOTHER_OT_apply,
    VGSMOOTHER_OT_purge_empty,
    VGSMOOTHER_OT_scan,
    VGSMOOTHER_OT_toggle_all,
    VGSMOOTHER_OT_reset,
    VGSMOOTHER_UL_groups,
    VGSMOOTHER_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    # Stored per-object so each mesh has its own independent wizard state
    bpy.types.Object.vg_smooth_settings = PointerProperty(type=VGSmoothSettings)


def unregister():
    del bpy.types.Object.vg_smooth_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
