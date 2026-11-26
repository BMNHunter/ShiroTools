"""Surface Deform Target Check, vectorised, with fixes and a real bind test.

Checks the constraints the Surface Deform modifier puts on its target:
edges with 3+ faces, concave faces, overlapping vertices, collinear corners.
The face analysis runs in numpy over the whole mesh at once (no per-corner
Python loop). Results are keyed by session_uid so renaming does not lose them.
"""

from math import radians, sin

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .common import TAB

CATEGORIES = (
    ("multi_face_edges", "Edges with 3+ faces", "EDGE", "EDGESEL"),
    ("concave_faces", "Concave faces", "FACE", "FACESEL"),
    ("double_verts", "Overlapping vertices", "VERT", "VERTEXSEL"),
    ("collinear_faces", "Faces with collinear edges", "FACE", "FACESEL"),
    ("collinear_verts", "Collinear corner vertices", "VERT", "VERTEXSEL"),
)
LOOKUP = {k: (label, dom, icon) for k, label, dom, icon in CATEGORIES}
MAIN = ("multi_face_edges", "concave_faces", "double_verts", "collinear_faces")
EPS = 1e-9

_results = {}


# ---------------------------------------------------------------------------
# analysis (pure, tested)
# ---------------------------------------------------------------------------

def _fget(coll, attr, n, width=1, dtype=np.float32):
    # native dtypes (float32 / int32) let foreach_get memcpy instead of converting
    buf = np.empty(n * width, dtype=dtype)
    coll.foreach_get(attr, buf)
    return buf.reshape(-1, width) if width > 1 else buf


def _corner(mesh, attr_name, loop_attr):
    """Corner int data; the attribute path is ~3x faster than mesh.loops."""
    n = len(mesh.loops)
    attr = mesh.attributes.get(attr_name)
    buf = np.empty(n, dtype=np.int32)
    if attr is not None and len(attr.data) == n:
        attr.data.foreach_get("value", buf)
    else:
        mesh.loops.foreach_get(loop_attr, buf)
    return buf


def analyze_mesh(mesh, merge_distance, concave_angle, collinear_angle):
    nv, ne, nf, nl = len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops)
    co = _fget(mesh.vertices, "co", nv, 3).astype(np.float64)
    result = {"topology": (nv, ne, nf)}

    le = _corner(mesh, ".corner_edge", "edge_index")
    counts = np.bincount(le, minlength=ne) if nl else np.zeros(ne, dtype=np.int32)
    result["multi_face_edges"] = np.nonzero(counts > 2)[0]

    if nf:
        # face corners are a sorted offsets array (Blender 4.0+): corner i belongs
        # to face poly[i] and the corners of one face are contiguous
        ls = _fget(mesh.polygons, "loop_start", nf, dtype=np.int32)
        lt = _fget(mesh.polygons, "loop_total", nf, dtype=np.int32)
        lv = _corner(mesh, ".corner_vert", "vertex_index")
        pn = _fget(mesh.polygon_normals, "vector", nf, 3).astype(np.float64)
        poly = np.repeat(np.arange(nf), lt)
        start = ls[poly]
        pos = np.arange(nl) - start
        prev = start + (pos - 1) % lt[poly]
        nxt = start + (pos + 1) % lt[poly]
        a, b, c = co[lv[prev]], co[lv], co[lv[nxt]]
        inc, out = b - a, c - b
        li = np.linalg.norm(inc, axis=1)
        lo = np.linalg.norm(out, axis=1)
        degenerate = (li < EPS) | (lo < EPS)
        with np.errstate(invalid="ignore", divide="ignore"):
            inc_n = inc / li[:, None]
            out_n = out / lo[:, None]
        cross = np.cross(inc_n, out_n)
        cl = np.linalg.norm(cross, axis=1)
        straight = degenerate | ~(cl >= sin(collinear_angle))
        nlen = np.linalg.norm(pn, axis=1)
        usable = nlen > 1e-6
        with np.errstate(invalid="ignore", divide="ignore"):
            n_unit = pn / np.where(usable, nlen, 1.0)[:, None]
        signed = (np.nan_to_num(cross) * n_unit[poly]).sum(axis=1)
        thr = sin(concave_angle)
        ok = ~straight & usable[poly]
        pos_turn = ok & (signed > thr)
        neg_turn = ok & (signed < -thr)

        def per_face(flags):  # bincount: ufunc.at is ~10x slower
            return np.bincount(poly, weights=flags, minlength=nf) > 0

        result["concave_faces"] = np.nonzero(per_face(pos_turn) & per_face(neg_turn))[0]
        result["collinear_faces"] = np.nonzero(per_face(straight))[0]
        result["collinear_verts"] = np.unique(lv[straight])
    else:
        for k in ("concave_faces", "collinear_faces", "collinear_verts"):
            result[k] = np.zeros(0, dtype=np.int32)

    result["double_verts"] = find_doubles(mesh, merge_distance)
    return result


def find_doubles(mesh, distance):
    if distance <= 0.0 or not len(mesh.vertices):
        return np.zeros(0, dtype=np.int32)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        res = bmesh.ops.find_doubles(bm, verts=bm.verts[:], dist=distance)
        hits = set()
        for src, dst in res["targetmap"].items():
            hits.add(src.index)
            hits.add(dst.index)
    finally:
        bm.free()
    return np.array(sorted(hits), dtype=np.int32)


def mesh_topology(mesh):
    return len(mesh.vertices), len(mesh.edges), len(mesh.polygons)


def run_check(context, obj):
    s = context.scene.sdcheck_settings
    evaluated = s.use_evaluated and obj.mode == "OBJECT"
    if obj.mode == "EDIT":
        obj.update_from_editmode()
    if evaluated:
        ev = obj.evaluated_get(context.evaluated_depsgraph_get())
        mesh = ev.to_mesh()
        try:
            res = analyze_mesh(mesh, s.merge_distance, s.concave_angle, s.collinear_angle)
        finally:
            ev.to_mesh_clear()
    else:
        res = analyze_mesh(obj.data, s.merge_distance, s.concave_angle, s.collinear_angle)
    res["evaluated"] = evaluated
    _results[obj.session_uid] = res
    return res


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class SHIRO_PG_sdcheck(PropertyGroup):
    merge_distance: FloatProperty(name="Merge Distance", default=0.0001, min=0.0, soft_max=0.1,
                                  precision=6, step=0.01, subtype="DISTANCE")
    concave_angle: FloatProperty(name="Concave Threshold", default=radians(0.5), min=0.0,
                                 max=radians(45.0), subtype="ANGLE")
    collinear_angle: FloatProperty(name="Collinear Threshold", default=radians(0.5), min=0.0,
                                   max=radians(45.0), subtype="ANGLE")
    use_evaluated: BoolProperty(name="Use Evaluated Mesh", default=False,
                                description="Check the mesh after modifiers. Selection and "
                                            "fixes are unavailable in this mode")
    skip_hidden: BoolProperty(name="Skip Hidden", default=True)


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

def _mesh_poll(context):
    obj = context.active_object
    return obj is not None and obj.type == "MESH"


class SHIRO_OT_sd_check(Operator):
    bl_idname = "shiro.sd_check"
    bl_label = "Check Mesh"
    bl_description = "Validate the active mesh as a Surface Deform target"

    @classmethod
    def poll(cls, context):
        return _mesh_poll(context)

    def execute(self, context):
        res = run_check(context, context.active_object)
        total = sum(len(res[k]) for k in MAIN)
        if total:
            self.report({"WARNING"}, f"{total} problem element(s)")
        else:
            self.report({"INFO"}, "Valid Surface Deform target")
        return {"FINISHED"}


class SHIRO_OT_sd_select(Operator):
    bl_idname = "shiro.sd_select"
    bl_label = "Select Problem Elements"
    bl_options = {"REGISTER", "UNDO"}

    category: EnumProperty(items=[(k, l, l) for k, l, _d, _i in CATEGORIES])
    extend: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return _mesh_poll(context)

    def execute(self, context):
        obj = context.active_object
        res = _results.get(obj.session_uid)
        if res is None or res["evaluated"]:
            self.report({"ERROR"}, "Run the check first (not in evaluated mode)")
            return {"CANCELLED"}
        if obj.mode == "EDIT":
            obj.update_from_editmode()
        if mesh_topology(obj.data) != res["topology"]:
            self.report({"ERROR"}, "Mesh changed since the check, run it again")
            return {"CANCELLED"}
        indices = res[self.category]
        if not len(indices):
            return {"CANCELLED"}
        _label, domain, _icon = LOOKUP[self.category]
        if obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
        context.tool_settings.mesh_select_mode = (domain == "VERT", domain == "EDGE",
                                                  domain == "FACE")
        bm = bmesh.from_edit_mesh(obj.data)
        seq = {"VERT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[domain]
        seq.ensure_lookup_table()
        if not self.extend:
            for elems in (bm.faces, bm.edges, bm.verts):
                for e in elems:
                    e.select_set(False)
        skip = context.scene.sdcheck_settings.skip_hidden
        selected = skipped = 0
        for i in indices:
            el = seq[int(i)]
            if el.hide and skip:
                skipped += 1
                continue
            el.select_set(True)
            selected += 1
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        self.report({"INFO"}, f"Selected {selected}" + (f", skipped {skipped} hidden"
                                                        if skipped else ""))
        return {"FINISHED"}


class SHIRO_OT_sd_fix(Operator):
    bl_idname = "shiro.sd_fix"
    bl_label = "Fix"
    bl_description = "Repair this category on the mesh, then re-check"
    bl_options = {"REGISTER", "UNDO"}

    category: EnumProperty(items=[
        ("double_verts", "Merge Doubles", "Merge the overlapping vertices"),
        ("concave_faces", "Triangulate Concave", "Triangulate the concave faces"),
        ("collinear_verts", "Dissolve Collinear", "Dissolve straight corner vertices "
                                                  "that sit between exactly two edges"),
    ])

    @classmethod
    def poll(cls, context):
        return _mesh_poll(context)

    def execute(self, context):
        obj = context.active_object
        res = _results.get(obj.session_uid)
        if res is None or res["evaluated"]:
            return {"CANCELLED"}
        edit = obj.mode == "EDIT"
        if edit:
            obj.update_from_editmode()
        if mesh_topology(obj.data) != res["topology"]:
            self.report({"ERROR"}, "Mesh changed since the check, run it again")
            return {"CANCELLED"}
        settings = context.scene.sdcheck_settings
        if edit:
            bm = bmesh.from_edit_mesh(obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        idx = [int(i) for i in res[self.category]]
        before = len(idx)
        if self.category == "double_verts":
            bmesh.ops.remove_doubles(bm, verts=[bm.verts[i] for i in idx],
                                     dist=settings.merge_distance)
        elif self.category == "concave_faces":
            bmesh.ops.triangulate(bm, faces=[bm.faces[i] for i in idx],
                                  quad_method="BEAUTY", ngon_method="BEAUTY")
        else:
            verts = [bm.verts[i] for i in idx if len(bm.verts[i].link_edges) == 2]
            bmesh.ops.dissolve_verts(bm, verts=verts)
            before = len(verts)
        if edit:
            bmesh.update_edit_mesh(obj.data)
        else:
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()
        res = run_check(context, obj)
        left = len(res["collinear_verts" if self.category == "collinear_verts"
                       else self.category])
        self.report({"INFO"}, f"Fixed {before} element(s), {left} left")
        return {"FINISHED"}


def binders_of(obj):
    for ob in bpy.data.objects:
        for mod in ob.modifiers:
            if mod.type == "SURFACE_DEFORM" and mod.target == obj:
                yield ob, mod


class SHIRO_OT_sd_bind(Operator):
    bl_idname = "shiro.sd_bind"
    bl_label = "Bind / Unbind"
    bl_description = "Bind (or unbind) this Surface Deform modifier and report the result"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty()
    modifier: StringProperty()

    def execute(self, context):
        ob = bpy.data.objects.get(self.object_name)
        mod = ob.modifiers.get(self.modifier) if ob else None
        if mod is None:
            return {"CANCELLED"}
        was = mod.is_bound
        with context.temp_override(object=ob, active_object=ob):
            bpy.ops.object.surfacedeform_bind(modifier=mod.name)
        context.view_layer.update()
        if not was and not mod.is_bound:
            self.report({"ERROR"}, f"{ob.name}: bind failed, see the modifier's error")
        else:
            self.report({"INFO"}, f"{ob.name}: {'bound' if mod.is_bound else 'unbound'}")
        return {"FINISHED"}


class SHIRO_OT_sd_clear(Operator):
    bl_idname = "shiro.sd_clear"
    bl_label = "Clear Results"

    def execute(self, context):
        obj = context.active_object
        if obj is not None:
            _results.pop(obj.session_uid, None)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

FIXABLE = {"double_verts": "double_verts", "concave_faces": "concave_faces",
           "collinear_faces": "collinear_verts"}


class SHIRO_PT_sdcheck(Panel):
    bl_idname = "SHIRO_PT_sdcheck"
    bl_label = "Surface Deform Check"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        s = context.scene.sdcheck_settings
        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh", icon="INFO")
            return
        col = layout.column(align=True)
        col.prop(s, "merge_distance")
        col.prop(s, "concave_angle")
        col.prop(s, "collinear_angle")
        row = layout.row(align=True)
        row.prop(s, "use_evaluated", toggle=True)
        row.prop(s, "skip_hidden", toggle=True)
        big = layout.row(align=True)
        big.scale_y = 1.4
        big.operator("shiro.sd_check", icon="CHECKMARK")
        big.operator("shiro.sd_clear", text="", icon="TRASH")

        res = _results.get(obj.session_uid)
        if res is not None:
            box = layout.box()
            stale = not res["evaluated"] and mesh_topology(obj.data) != res["topology"] and \
                obj.mode != "EDIT"
            if res["evaluated"]:
                box.label(text="Evaluated mesh: counts only", icon="MODIFIER")
            elif stale:
                box.label(text="Mesh changed, results are stale", icon="ERROR")
            usable = not res["evaluated"] and not stale
            total = 0
            for key in MAIN:
                label, _dom, icon = LOOKUP[key]
                n = len(res[key])
                total += n
                row = box.row(align=True)
                row.alert = n > 0
                row.label(text=f"{label}: {n}", icon="ERROR" if n else "CHECKMARK")
                if n:
                    sub = row.row(align=True)
                    sub.enabled = usable
                    sub.operator("shiro.sd_select", text="", icon=icon).category = key
                    if key in FIXABLE:
                        sub.operator("shiro.sd_fix", text="", icon="TOOL_SETTINGS").category = \
                            FIXABLE[key]
                if key == "collinear_faces" and len(res["collinear_verts"]):
                    r = box.row(align=True)
                    r.separator(factor=2.0)
                    r.label(text=f"corner verts: {len(res['collinear_verts'])}")
                    sub = r.row(align=True)
                    sub.enabled = usable
                    sub.operator("shiro.sd_select", text="", icon="VERTEXSEL").category = \
                        "collinear_verts"
            if total == 0:
                box.label(text="Valid target", icon="SOLO_ON")

        binders = list(binders_of(obj))
        if binders:
            box = layout.box()
            box.label(text="Surface Deform users", icon="MOD_MESHDEFORM")
            for ob, mod in binders:
                row = box.row(align=True)
                row.label(text=f"{ob.name} › {mod.name}",
                          icon="LINKED" if mod.is_bound else "UNLINKED")
                op = row.operator("shiro.sd_bind", text="Unbind" if mod.is_bound else "Bind")
                op.object_name, op.modifier = ob.name, mod.name


classes = (SHIRO_PG_sdcheck, SHIRO_OT_sd_check, SHIRO_OT_sd_select, SHIRO_OT_sd_fix,
           SHIRO_OT_sd_bind, SHIRO_OT_sd_clear, SHIRO_PT_sdcheck)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sdcheck_settings = PointerProperty(type=SHIRO_PG_sdcheck)


def unregister():
    del bpy.types.Scene.sdcheck_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _results.clear()
