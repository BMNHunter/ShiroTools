# SPDX-License-Identifier: GPL-2.0-or-later
"""
Surface Deform Target Check
---------------------------
Validates a mesh against the constraints the Surface Deform modifier places on
its target object, and lets you select each class of problem element.

Checked constraints (from the Blender manual, Surface Deform Modifier):
    * no edges with more than two faces
    * no concave faces
    * no overlapping vertices (doubles)
    * no faces with collinear edges
"""

import bpy
import bmesh
from math import radians, sin
from mathutils.kdtree import KDTree

bl_info = {
    "name": "Surface Deform Target Check",
    "author": "Milan",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar > SD Check",
    "description": "Check a mesh for Surface Deform target validity and select problem elements",
    "category": "Mesh",
}


# ---------------------------------------------------------------------------
# Category table
# ---------------------------------------------------------------------------
# key, UI label, mesh domain, panel icon
CATEGORIES = (
    ("multi_face_edges", "Edges with 3+ faces", "EDGE", "EDGESEL"),
    ("concave_faces", "Concave faces", "FACE", "FACESEL"),
    ("double_verts", "Overlapping vertices", "VERT", "VERTEXSEL"),
    ("collinear_faces", "Faces with collinear edges", "FACE", "FACESEL"),
    ("collinear_verts", "Collinear corner vertices", "VERT", "VERTEXSEL"),
)

CATEGORY_LOOKUP = {key: (label, domain, icon) for key, label, domain, icon in CATEGORIES}

# Rows drawn in the panel. The fifth category is an optional sub-selection of
# the fourth, so it gets its own smaller row.
MAIN_ROWS = ("multi_face_edges", "concave_faces", "double_verts", "collinear_faces")

# Per-object analysis cache: object name -> result dict.
_results = {}

EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def find_multi_face_edges(bm):
    """Edges shared by three or more faces."""
    return [e.index for e in bm.edges if len(e.link_faces) > 2]


def find_double_verts(bm, distance):
    """Vertices that sit within `distance` of another vertex."""
    if distance <= 0.0:
        return []

    verts = bm.verts[:]

    try:
        result = bmesh.ops.find_doubles(bm, verts=verts, dist=distance)
        targetmap = result.get("targetmap", {})
        hits = set()
        for source, target in targetmap.items():
            hits.add(source.index)
            hits.add(target.index)
        return sorted(hits)
    except Exception:
        pass

    # Fallback: pure mathutils KD-tree lookup.
    tree = KDTree(len(verts))
    for v in verts:
        tree.insert(v.co, v.index)
    tree.balance()

    hits = set()
    for v in verts:
        for _co, index, _dist in tree.find_range(v.co, distance):
            if index != v.index:
                hits.add(v.index)
                hits.add(index)
    return sorted(hits)


def analyze_faces(bm, concave_threshold, collinear_threshold):
    """Single pass over the faces collecting concavity and collinearity.

    `concave_threshold` and `collinear_threshold` are sine values, so they can
    be compared directly against the length of a normalized cross product.
    """
    concave_faces = []
    collinear_faces = []
    collinear_verts = set()

    for f in bm.faces:
        loops = f.loops[:]
        count = len(loops)
        normal = f.normal.copy()

        usable_normal = normal.length_squared > 1e-12
        if usable_normal:
            normal.normalize()

        turns_positive = False
        turns_negative = False
        has_straight_corner = False

        for i in range(count):
            prev_co = loops[i - 1].vert.co
            corner_co = loops[i].vert.co
            next_co = loops[(i + 1) % count].vert.co

            incoming = corner_co - prev_co
            outgoing = next_co - corner_co

            if incoming.length < EPSILON or outgoing.length < EPSILON:
                has_straight_corner = True
                collinear_verts.add(loops[i].vert.index)
                continue

            cross = incoming.normalized().cross(outgoing.normalized())

            # |cross| is the sine of the turn angle. A value near zero means the
            # two edges continue in a straight line (or fold back on themselves).
            if cross.length < collinear_threshold:
                has_straight_corner = True
                collinear_verts.add(loops[i].vert.index)
                continue

            if not usable_normal:
                continue

            signed = cross.dot(normal)
            if signed > concave_threshold:
                turns_positive = True
            elif signed < -concave_threshold:
                turns_negative = True

        # A convex polygon turns the same way at every corner. Mixed signs mean
        # at least one reflex corner.
        if turns_positive and turns_negative:
            concave_faces.append(f.index)

        if has_straight_corner:
            collinear_faces.append(f.index)

    return concave_faces, collinear_faces, sorted(collinear_verts)


def analyze_bmesh(bm, settings):
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    concave_threshold = sin(settings.concave_angle)
    collinear_threshold = sin(settings.collinear_angle)

    concave_faces, collinear_faces, collinear_verts = analyze_faces(
        bm, concave_threshold, collinear_threshold
    )

    return {
        "multi_face_edges": find_multi_face_edges(bm),
        "concave_faces": concave_faces,
        "double_verts": find_double_verts(bm, settings.merge_distance),
        "collinear_faces": collinear_faces,
        "collinear_verts": collinear_verts,
        "topology": (len(bm.verts), len(bm.edges), len(bm.faces)),
    }


def mesh_topology(mesh):
    return (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class SDCHECK_PG_settings(bpy.types.PropertyGroup):
    merge_distance: bpy.props.FloatProperty(
        name="Merge Distance",
        description="Vertices closer together than this count as doubles",
        default=0.0001,
        min=0.0,
        soft_max=0.1,
        precision=6,
        step=0.01,
        subtype="DISTANCE",
    )
    concave_angle: bpy.props.FloatProperty(
        name="Concave Threshold",
        description="Minimum reflex corner angle that marks a face as concave",
        default=radians(0.5),
        min=0.0,
        max=radians(45.0),
        subtype="ANGLE",
    )
    collinear_angle: bpy.props.FloatProperty(
        name="Collinear Threshold",
        description="Corners that turn by less than this angle count as collinear",
        default=radians(0.5),
        min=0.0,
        max=radians(45.0),
        subtype="ANGLE",
    )
    use_evaluated: bpy.props.BoolProperty(
        name="Use Evaluated Mesh",
        description=(
            "Check the mesh after modifiers, which is what Surface Deform binds "
            "against. Element selection is unavailable in this mode"
        ),
        default=False,
    )
    skip_hidden: bpy.props.BoolProperty(
        name="Skip Hidden",
        description="Leave hidden elements out of the selection",
        default=True,
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class SDCHECK_OT_analyze(bpy.types.Operator):
    bl_idname = "sdcheck.analyze"
    bl_label = "Check Mesh"
    bl_description = "Validate the active mesh as a Surface Deform target"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.sdcheck_settings
        evaluated = settings.use_evaluated and obj.mode == "OBJECT"

        if evaluated:
            depsgraph = context.evaluated_depsgraph_get()
            obj_eval = obj.evaluated_get(depsgraph)
            mesh_eval = obj_eval.to_mesh()
            bm = bmesh.new()
            bm.from_mesh(mesh_eval)
            result = analyze_bmesh(bm, settings)
            bm.free()
            obj_eval.to_mesh_clear()
        elif obj.mode == "EDIT":
            bm = bmesh.from_edit_mesh(obj.data)
            result = analyze_bmesh(bm, settings)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            result = analyze_bmesh(bm, settings)
            bm.free()

        result["evaluated"] = evaluated
        _results[obj.name] = result

        total = sum(len(result[key]) for key in MAIN_ROWS)
        if total == 0:
            self.report({"INFO"}, "Mesh looks valid as a Surface Deform target")
        else:
            self.report({"WARNING"}, "%d problem element(s) found" % total)

        return {"FINISHED"}


class SDCHECK_OT_select(bpy.types.Operator):
    bl_idname = "sdcheck.select"
    bl_label = "Select Problem Elements"
    bl_description = "Select the elements found by the last check"
    bl_options = {"REGISTER", "UNDO"}

    category: bpy.props.EnumProperty(
        name="Category",
        items=[(key, label, label) for key, label, _domain, _icon in CATEGORIES],
    )
    extend: bpy.props.BoolProperty(
        name="Extend",
        description="Keep the existing selection",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.sdcheck_settings
        result = _results.get(obj.name)

        if result is None:
            self.report({"ERROR"}, "Run the check first")
            return {"CANCELLED"}

        if result.get("evaluated"):
            self.report({"ERROR"}, "Evaluated-mesh results cannot be mapped back to the original mesh")
            return {"CANCELLED"}

        if mesh_topology(obj.data) != result["topology"]:
            self.report({"ERROR"}, "The mesh changed since the last check. Run it again")
            return {"CANCELLED"}

        indices = result.get(self.category, [])
        if not indices:
            self.report({"INFO"}, "Nothing to select in this category")
            return {"CANCELLED"}

        _label, domain, _icon = CATEGORY_LOOKUP[self.category]

        if obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")

        if domain == "VERT":
            context.tool_settings.mesh_select_mode = (True, False, False)
        elif domain == "EDGE":
            context.tool_settings.mesh_select_mode = (False, True, False)
        else:
            context.tool_settings.mesh_select_mode = (False, False, True)

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        if domain == "VERT":
            sequence = bm.verts
        elif domain == "EDGE":
            sequence = bm.edges
        else:
            sequence = bm.faces

        if not self.extend:
            for face in bm.faces:
                face.select_set(False)
            for edge in bm.edges:
                edge.select_set(False)
            for vert in bm.verts:
                vert.select_set(False)

        selected = 0
        skipped = 0
        for index in indices:
            if index >= len(sequence):
                continue
            element = sequence[index]
            if element.hide and settings.skip_hidden:
                skipped += 1
                continue
            element.select_set(True)
            selected += 1

        bm.select_flush_mode()
        bmesh.update_edit_mesh(mesh)

        if skipped > 0:
            self.report({"INFO"}, "Selected %d, skipped %d hidden" % (selected, skipped))
        else:
            self.report({"INFO"}, "Selected %d element(s)" % selected)

        return {"FINISHED"}


class SDCHECK_OT_clear(bpy.types.Operator):
    bl_idname = "sdcheck.clear"
    bl_label = "Clear Results"
    bl_description = "Discard the cached check results for this object"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.name in _results

    def execute(self, context):
        _results.pop(context.active_object.name, None)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class SDCHECK_PT_panel(bpy.types.Panel):
    bl_label = "Surface Deform Target Check"
    bl_idname = "SDCHECK_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SD Check"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        settings = context.scene.sdcheck_settings

        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh object", icon="INFO")
            return

        layout.label(text=obj.name, icon="OUTLINER_OB_MESH")

        column = layout.column(align=True)
        column.prop(settings, "merge_distance")
        column.prop(settings, "concave_angle")
        column.prop(settings, "collinear_angle")

        options = layout.column(align=True)
        options.prop(settings, "use_evaluated")
        options.prop(settings, "skip_hidden")

        if settings.use_evaluated and obj.mode == "EDIT":
            layout.label(text="Evaluated mode needs Object Mode", icon="ERROR")

        layout.operator("sdcheck.analyze", icon="CHECKMARK")

        result = _results.get(obj.name)
        if result is None:
            layout.label(text="No results yet", icon="INFO")
            return

        box = layout.box()

        stale = mesh_topology(obj.data) != result["topology"]
        evaluated = result.get("evaluated", False)

        if evaluated:
            box.label(text="Evaluated mesh, counts only", icon="MODIFIER")
        elif stale:
            box.label(text="Mesh changed, results are stale", icon="ERROR")

        total = 0
        for key in MAIN_ROWS:
            label, _domain, icon = CATEGORY_LOOKUP[key]
            count = len(result[key])
            total += count

            row = box.row(align=True)

            if count == 0:
                row.label(text="%s: 0" % label, icon="CHECKMARK")
            else:
                row.label(text="%s: %d" % (label, count), icon="ERROR")
                sub = row.row(align=True)
                sub.enabled = not evaluated and not stale
                op = sub.operator("sdcheck.select", text="", icon=icon)
                op.category = key
                op.extend = False

            if key == "collinear_faces" and len(result["collinear_verts"]) > 0:
                sub_row = box.row(align=True)
                sub_row.separator(factor=2.0)
                sub_row.label(text="corner verts: %d" % len(result["collinear_verts"]))
                sub = sub_row.row(align=True)
                sub.enabled = not evaluated and not stale
                op = sub.operator("sdcheck.select", text="", icon="VERTEXSEL")
                op.category = "collinear_verts"
                op.extend = False

        if total == 0:
            box.label(text="Valid target", icon="SOLO_ON")

        layout.operator("sdcheck.clear", icon="TRASH")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
classes = (
    SDCHECK_PG_settings,
    SDCHECK_OT_analyze,
    SDCHECK_OT_select,
    SDCHECK_OT_clear,
    SDCHECK_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sdcheck_settings = bpy.props.PointerProperty(type=SDCHECK_PG_settings)


def unregister():
    del bpy.types.Scene.sdcheck_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _results.clear()


if __name__ == "__main__":
    register()
