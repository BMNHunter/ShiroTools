"""Unity Batch Exporter with a pre-export health check.

Presets (FBX, tuned for Unity's importer):
  Static Mesh          meshes, transforms baked so Unity shows 0 rotation / scale 1
  Skinned Mesh         meshes + armature, no animation
  Rig + Animations     meshes + armature + every action (or NLA strip) as a clip
  Animations Only      armature + actions, no mesh (for animation-only FBX files)
All presets: Apply Scalings "FBX All", -Z forward / Y up, no leaf bones,
deform bones only, optional embedded textures.

The health check flags what usually breaks on import: missing / default /
collapsed UVs, missing or non-Principled materials, procedural shaders that
need baking, missing texture files, unapplied or negative scale, rotation,
>4 bone influences, unnormalized weights, armature not in the export, ngons,
zero-area faces, loose geometry. Many issues have a one-click fix.
"""

import os

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .common import SEVERITY_ICON, TAB, image_abspath, iter_image_nodes, smart_uv

PROCEDURAL = {"TEX_NOISE", "TEX_VORONOI", "TEX_WAVE", "TEX_MAGIC", "TEX_GRADIENT",
              "TEX_CHECKER", "TEX_BRICK", "TEX_WHITE_NOISE", "TEX_GABOR", "TEX_SKY",
              "AMBIENT_OCCLUSION", "BEVEL", "LAYER_WEIGHT", "FRESNEL"}

PRESETS = {
    "STATIC": dict(object_types={"MESH", "EMPTY"}, bake_anim=False, bake_space_transform=True),
    "SKINNED": dict(object_types={"MESH", "ARMATURE", "EMPTY"}, bake_anim=False,
                    bake_space_transform=False),
    "ANIMATED": dict(object_types={"MESH", "ARMATURE", "EMPTY"}, bake_anim=True,
                     bake_space_transform=False),
    "ANIM_ONLY": dict(object_types={"ARMATURE"}, bake_anim=True, bake_space_transform=False),
}

_report = None  # {"issues": [...], "sets": [(name, [obj names])]}


class SHIRO_PG_export(PropertyGroup):
    preset: EnumProperty(name="Preset", items=[
        ("STATIC", "Static Mesh", "Props / environment. No armature, no animation"),
        ("SKINNED", "Skinned Mesh", "Characters without animation"),
        ("ANIMATED", "Rig + Animations", "Mesh, armature and all actions as clips"),
        ("ANIM_ONLY", "Animations Only", "Armature and actions only, no mesh"),
    ], default="STATIC")
    scope: EnumProperty(name="Files", items=[
        ("SELECTED", "Selection → 1 File", "Everything selected (with children) in one FBX"),
        ("EACH", "Each Object → File", "One FBX per selected top-level object (with children)"),
        ("COLLECTIONS", "Each Collection → File", "One FBX per child collection of the root"),
    ], default="SELECTED")
    root_collection: PointerProperty(name="Root", type=bpy.types.Collection,
                                     description="Empty: the scene collection")
    output_dir: StringProperty(name="Folder", subtype="DIR_PATH", default="//Export/")
    file_name: StringProperty(name="File Name", default="",
                              description="For 'Selection → 1 File'. Empty: active object name")
    embed_textures: BoolProperty(name="Embed Textures", default=True,
                                 description="Pack the textures into the FBX (path mode Copy)")
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    triangulate: BoolProperty(name="Triangulate", default=False)
    use_nla: BoolProperty(name="NLA Strips as Clips", default=False,
                          description="Off: every action becomes a clip")
    simplify: FloatProperty(name="Key Simplify", default=1.0, min=0.0, max=100.0,
                            description="0 keeps every baked key, higher removes more")
    block_on_errors: BoolProperty(name="Block on Errors", default=True)


# ---------------------------------------------------------------------------
# export sets
# ---------------------------------------------------------------------------

def _with_children(objs):
    out, seen = [], set()
    stack = list(objs)
    while stack:
        o = stack.pop(0)
        if o.as_pointer() in seen:
            continue
        seen.add(o.as_pointer())
        out.append(o)
        stack.extend(o.children)
    return out


def export_sets(context, s):
    sel = list(context.selected_objects)
    if s.scope == "SELECTED":
        if not sel:
            return []
        name = s.file_name.strip() or (context.active_object.name if context.active_object
                                       else sel[0].name)
        return [(name, _with_children(sel))]
    if s.scope == "EACH":
        tops = [o for o in sel if o.parent not in sel]
        return [(o.name, _with_children([o])) for o in tops]
    root = s.root_collection or context.scene.collection
    return [(c.name, list(c.all_objects)) for c in root.children if c.all_objects]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

class Issue:
    __slots__ = ("obj", "severity", "msg", "fix")

    def __init__(self, obj, severity, msg, fix=""):
        self.obj, self.severity, self.msg, self.fix = obj, severity, msg, fix


def uv_findings(mesh):
    """Default / collapsed / out-of-range UV detection on the active UV map."""
    uvl = mesh.uv_layers.active
    nl, nf = len(mesh.loops), len(mesh.polygons)
    if uvl is None or nl == 0 or nf == 0:
        return []
    uv = np.empty(nl * 2, dtype=np.float32)
    uvl.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    ls = np.empty(nf, dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", ls)
    lo = np.minimum.reduceat(uv, ls, axis=0)
    hi = np.maximum.reduceat(uv, ls, axis=0)
    size = hi - lo
    out = []
    if float(np.ptp(uv[:, 0])) < 1e-6 and float(np.ptp(uv[:, 1])) < 1e-6:
        out.append(("ERROR", "UV map is collapsed (all UVs in one point)"))
        return out
    full = np.all(np.abs(lo) < 1e-3, axis=1) & np.all(np.abs(hi - 1.0) < 1e-3, axis=1)
    if nf > 1 and full.mean() > 0.9:
        out.append(("WARNING", "UVs look like default / reset UVs (every face fills 0-1)"))
    zero = np.sum(size[:, 0] * size[:, 1] < 1e-12)
    if zero > nf * 0.05:
        out.append(("WARNING", f"{int(zero)} face(s) with zero UV area"))
    if uv.min() < -0.001 or uv.max() > 1.001:
        out.append(("INFO", "UVs outside 0-1 (fine for tiling, not for lightmaps)"))
    return out


def material_findings(mat):
    out = []
    nt = mat.node_tree
    if nt is None:
        return [("WARNING", f"'{mat.name}': no node tree")]
    outn = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output),
                None) or next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    surf = outn.inputs["Surface"] if outn else None
    if surf is None or not surf.is_linked:
        out.append(("WARNING", f"'{mat.name}': output not connected"))
    elif surf.links[0].from_node.type != "BSDF_PRINCIPLED":
        out.append(("WARNING", f"'{mat.name}': not a Principled BSDF, Unity reads only "
                               "Principled values"))
    imgs = list(iter_image_nodes(nt))
    if not imgs:
        out.append(("WARNING", f"'{mat.name}': no textures, only flat values export"))
    for _node, img in imgs:
        if img.packed_file is None and img.source == "FILE" and img.filepath and \
                not os.path.exists(image_abspath(img)):
            out.append(("ERROR", f"'{mat.name}': texture file missing ({img.name})"))
    procedural = {n.bl_label for n in nt.nodes if n.type in PROCEDURAL}
    if procedural:
        out.append(("WARNING", f"'{mat.name}': procedural nodes ({', '.join(sorted(procedural))}) "
                               "don't export, bake them (One-Click Baker)"))
    return out


def check_object(obj, s, export_names, seen_mats):
    issues = []
    add = lambda sev, msg, fix="": issues.append(Issue(obj.name, sev, msg, fix))
    if obj.hide_get() or not obj.visible_get():
        add("WARNING", "Hidden: it can't be selected, so it won't export", "UNHIDE")
    sc = obj.scale
    if any(v < 0 for v in sc):
        add("ERROR", "Negative scale: normals flip in Unity", "APPLY_SCALE")
    elif any(abs(v - 1.0) > 1e-4 for v in sc):
        add("WARNING", f"Unapplied scale ({sc.x:.3g}, {sc.y:.3g}, {sc.z:.3g})", "APPLY_SCALE")
    if obj.type in {"MESH", "ARMATURE"} and any(abs(v) > 1e-4 for v in obj.rotation_euler) \
            and obj.rotation_mode not in {"QUATERNION", "AXIS_ANGLE"}:
        add("WARNING" if s.preset == "STATIC" else "INFO", "Unapplied rotation", "APPLY_ROT")

    if obj.type == "ARMATURE" and s.preset in {"ANIMATED", "ANIM_ONLY"}:
        if not bpy.data.actions:
            add("WARNING", "No actions in the file, nothing to export as clips")
    if obj.type != "MESH" or s.preset == "ANIM_ONLY":
        return issues

    me = obj.data
    if not me.uv_layers:
        add("ERROR", "No UV map", "SMART_UV")
    else:
        for sev, msg in uv_findings(me):
            add(sev, msg)
    if not obj.material_slots or not any(sl.material for sl in obj.material_slots):
        add("WARNING", "No material")
    if any(sl.material is None for sl in obj.material_slots):
        add("WARNING", "Empty material slot(s)")
    for sl in obj.material_slots:
        mat = sl.material
        if mat is None or mat.as_pointer() in seen_mats:
            continue
        seen_mats.add(mat.as_pointer())
        for sev, msg in material_findings(mat):
            add(sev, msg, "SELECT")

    nf = len(me.polygons)
    if nf:
        tot = np.empty(nf, dtype=np.int32)
        me.polygons.foreach_get("loop_total", tot)
        ngons = int((tot > 4).sum())
        if ngons:
            add("INFO", f"{ngons} ngon(s): Unity triangulates them, tangents may differ")
        area = np.empty(nf, dtype=np.float32)
        me.polygons.foreach_get("area", area)
        zero = int((area < 1e-10).sum())
        if zero:
            add("WARNING", f"{zero} zero-area face(s)")
    if len(me.vertices) > 65535:
        add("INFO", f"{len(me.vertices)} vertices: Unity switches to 32-bit indices")
    used = np.zeros(len(me.vertices), dtype=bool)
    if len(me.edges):
        ev = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", ev)
        used[ev] = True
    loose = int((~used).sum())
    if loose:
        add("INFO", f"{loose} loose vertex/vertices")

    arm = obj.find_armature()
    if s.preset == "STATIC" and arm is not None:
        add("INFO", "Has an armature but the preset is Static Mesh")
    if s.preset in {"SKINNED", "ANIMATED"}:
        if arm is None:
            add("INFO", "No armature modifier: exports as a rigid mesh")
        else:
            if arm.name not in export_names:
                add("ERROR", f"Armature '{arm.name}' is not part of this export", "ADD_ARMATURE")
            from .weights import deform_group_indices, weight_stats
            st = weight_stats(me, deform_group_indices(obj), 4)
            if len(st["over_limit"]):
                add("WARNING", f"{len(st['over_limit'])} vert(s) with >4 bone influences",
                    "LIMIT")
            if len(st["unnormalized"]):
                add("WARNING", f"{len(st['unnormalized'])} vert(s) with unnormalized weights",
                    "NORMALIZE")
            if len(st["unweighted"]):
                add("WARNING", f"{len(st['unweighted'])} unweighted vert(s) (won't move)")
    if me.shape_keys and s.apply_modifiers and any(m.type != "ARMATURE" for m in obj.modifiers):
        add("INFO", "Shape keys + applied modifiers: shape keys may be dropped")
    return issues


def run_check(context, s):
    sets = export_sets(context, s)
    issues = []
    seen_mats = set()
    types = PRESETS[s.preset]["object_types"]
    for _name, objs in sets:
        names = {o.name for o in objs}
        for obj in objs:
            if obj.type in types:
                issues += check_object(obj, s, names, seen_mats)
    return {"issues": issues, "sets": [(n, [o.name for o in objs]) for n, objs in sets]}


def counts(report):
    c = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for i in report["issues"]:
        c[i.severity] += 1
    return c


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SHIRO_OT_export_check(Operator):
    bl_idname = "shiro.export_check"
    bl_label = "Check"
    bl_description = "Run the pre-export health check on what would be exported"

    def execute(self, context):
        global _report
        _report = run_check(context, context.scene.shiro_export)
        c = counts(_report)
        self.report({"INFO"}, f"{c['ERROR']} error(s), {c['WARNING']} warning(s)")
        return {"FINISHED"}


class SHIRO_OT_export_fix(Operator):
    bl_idname = "shiro.export_fix"
    bl_label = "Fix"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty()
    fix: StringProperty()

    @classmethod
    def description(cls, context, props):
        return {"APPLY_SCALE": "Apply scale", "APPLY_ROT": "Apply rotation",
                "SMART_UV": "Smart UV Project", "LIMIT": "Limit to 4 influences + normalize",
                "NORMALIZE": "Normalize deform weights", "UNHIDE": "Unhide the object",
                "SELECT": "Select the object", "ADD_ARMATURE": "Select the armature too",
                "ALL": "Apply every automatic fix"}.get(props.fix, "")

    def execute(self, context):
        if self.fix == "ALL":
            for issue in list(_report["issues"]) if _report else []:
                if issue.fix in {"APPLY_SCALE", "APPLY_ROT", "LIMIT", "NORMALIZE", "UNHIDE"}:
                    obj = bpy.data.objects.get(issue.obj)
                    if obj is not None:
                        apply_fix(context, obj, issue.fix)
        else:
            obj = bpy.data.objects.get(self.object_name)
            if obj is None:
                return {"CANCELLED"}
            apply_fix(context, obj, self.fix)
        bpy.ops.shiro.export_check()
        return {"FINISHED"}


def apply_fix(context, obj, fix):
    if fix == "UNHIDE":
        obj.hide_set(False)
        obj.hide_viewport = False
    elif fix in {"APPLY_SCALE", "APPLY_ROT"}:
        with context.temp_override(selected_editable_objects=[obj], active_object=obj,
                                   object=obj):
            bpy.ops.object.transform_apply(location=False, rotation=fix == "APPLY_ROT",
                                           scale=fix == "APPLY_SCALE", isolate_users=True)
    elif fix == "SMART_UV":
        smart_uv(context, [obj])
    elif fix in {"LIMIT", "NORMALIZE"}:
        from .weights import run_on, select_mode
        if fix == "LIMIT":
            run_on(obj, bpy.ops.object.vertex_group_limit_total,
                   group_select_mode=select_mode(obj), limit=4)
        run_on(obj, bpy.ops.object.vertex_group_normalize_all,
               group_select_mode=select_mode(obj), lock_active=False)
    elif fix in {"SELECT", "ADD_ARMATURE"}:
        target = obj.find_armature() if fix == "ADD_ARMATURE" else obj
        if target is not None:
            try:
                target.select_set(True)
            except RuntimeError:
                pass
            if fix == "SELECT":
                context.view_layer.objects.active = target


def fbx_options(s):
    p = PRESETS[s.preset]
    return dict(
        object_types=p["object_types"], use_mesh_modifiers=s.apply_modifiers,
        mesh_smooth_type="FACE", use_tspace=False, use_triangles=s.triangulate,  # Unity computes Mikk tangents
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL", global_scale=1.0,
        axis_forward="-Z", axis_up="Y", use_space_transform=True,
        bake_space_transform=p["bake_space_transform"],
        add_leaf_bones=False, primary_bone_axis="Y", secondary_bone_axis="X",
        use_armature_deform_only=True, armature_nodetype="NULL",
        bake_anim=p["bake_anim"], bake_anim_use_all_bones=True,
        bake_anim_use_nla_strips=s.use_nla, bake_anim_use_all_actions=not s.use_nla,
        bake_anim_force_startend_keying=True, bake_anim_simplify_factor=s.simplify,
        path_mode="COPY" if s.embed_textures else "AUTO", embed_textures=s.embed_textures,
        use_custom_props=False, colors_type="SRGB",
    )


class SHIRO_OT_export(Operator):
    bl_idname = "shiro.export_unity"
    bl_label = "Export"
    bl_description = "Check, then export FBX file(s) for Unity"

    force: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        global _report
        s = context.scene.shiro_export
        out_dir = bpy.path.abspath(s.output_dir)
        if s.output_dir.startswith("//") and not bpy.data.filepath:
            self.report({"ERROR"}, "Save the .blend file first (or use an absolute folder)")
            return {"CANCELLED"}
        _report = run_check(context, s)
        if s.block_on_errors and not self.force and counts(_report)["ERROR"]:
            self.report({"ERROR"}, "Health check found errors, fix them or use Export Anyway")
            return {"CANCELLED"}
        sets = export_sets(context, s)
        if not sets:
            self.report({"WARNING"}, "Nothing to export")
            return {"CANCELLED"}
        os.makedirs(out_dir, exist_ok=True)
        prev_sel = list(context.selected_objects)
        prev_active = context.view_layer.objects.active
        written = []
        try:
            if context.active_object and context.active_object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for name, objs in sets:
                for o in context.view_layer.objects:
                    o.select_set(False)
                for o in objs:
                    try:
                        o.select_set(True)
                    except RuntimeError:
                        pass
                path = os.path.join(out_dir, bpy.path.clean_name(name) + ".fbx")
                bpy.ops.export_scene.fbx(filepath=path, use_selection=True, **fbx_options(s))
                written.append(path)
        finally:
            for o in context.view_layer.objects:
                try:
                    o.select_set(o in prev_sel)
                except RuntimeError:
                    pass
            context.view_layer.objects.active = prev_active
        self.report({"INFO"}, f"Exported {len(written)} file(s) to {out_dir}")
        return {"FINISHED"}


class SHIRO_PT_export(Panel):
    bl_idname = "SHIRO_PT_export"
    bl_label = "Unity Export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.shiro_export
        col = layout.column(align=True)
        col.prop(s, "preset", text="")
        col.prop(s, "scope", text="")
        if s.scope == "COLLECTIONS":
            col.prop(s, "root_collection")
        elif s.scope == "SELECTED":
            col.prop(s, "file_name")
        col.prop(s, "output_dir")
        grid = layout.grid_flow(columns=2, align=True)
        grid.prop(s, "embed_textures", toggle=True)
        grid.prop(s, "apply_modifiers", toggle=True)
        grid.prop(s, "triangulate", toggle=True)
        grid.prop(s, "block_on_errors", toggle=True)
        if s.preset in {"ANIMATED", "ANIM_ONLY"}:
            row = layout.row(align=True)
            row.prop(s, "use_nla", toggle=True)
            row.prop(s, "simplify")

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("shiro.export_check", icon="VIEWZOOM")
        r = _report
        blocked = r is not None and s.block_on_errors and counts(r)["ERROR"] > 0
        sub = row.row(align=True)
        sub.alert = blocked
        if blocked:
            sub.operator("shiro.export_unity", text="Export Anyway", icon="ERROR").force = True
        else:
            sub.operator("shiro.export_unity", icon="EXPORT")
        if r is None:
            return
        c = counts(r)
        box = layout.box()
        head = box.row(align=True)
        head.label(text=f"{len(r['sets'])} file(s)", icon="FILE")
        head.label(text=str(c["ERROR"]), icon="CANCEL")
        head.label(text=str(c["WARNING"]), icon="ERROR")
        head.label(text=str(c["INFO"]), icon="INFO")
        if any(i.fix in {"APPLY_SCALE", "APPLY_ROT", "LIMIT", "NORMALIZE", "UNHIDE"}
               for i in r["issues"]):
            box.operator("shiro.export_fix", text="Fix All Automatic", icon="TOOL_SETTINGS"
                         ).fix = "ALL"
        if not r["issues"]:
            box.label(text="Ready for Unity", icon="CHECKMARK")
        current = None
        for issue in sorted(r["issues"], key=lambda i: (i.obj, ["ERROR", "WARNING",
                                                                "INFO"].index(i.severity))):
            if issue.obj != current:
                current = issue.obj
                box.label(text=current, icon="OBJECT_DATA")
            row = box.row(align=True)
            row.alert = issue.severity == "ERROR"
            row.separator(factor=1.5)
            row.label(text=issue.msg, icon=SEVERITY_ICON[issue.severity])
            if issue.fix:
                op = row.operator("shiro.export_fix", text="", icon="TOOL_SETTINGS")
                op.object_name, op.fix = issue.obj, issue.fix


classes = (SHIRO_PG_export, SHIRO_OT_export_check, SHIRO_OT_export_fix, SHIRO_OT_export,
           SHIRO_PT_export)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_export = PointerProperty(type=SHIRO_PG_export)


def unregister():
    global _report
    _report = None
    del bpy.types.Scene.shiro_export
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
