"""Camera Bookmarks: save / jump between camera angles, stored per scene.

Data compatibility: keeps ``Scene.camera_bookmarks`` and the original fields
(camera_name, matrix, lens, ortho_scale, is_ortho, clip_start, clip_end) so
bookmarks saved by the old add-on load. New bookmarks additionally store the
full camera data (type incl. panoramic, sensor, shift, DOF) as JSON.
"""

import json

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    FloatVectorProperty, IntProperty, PointerProperty, StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix

from .common import TAB

CAM_ATTRS = (
    "type", "lens", "lens_unit", "sensor_fit", "sensor_width", "sensor_height",
    "shift_x", "shift_y", "ortho_scale", "clip_start", "clip_end",
    "panorama_type", "fisheye_fov", "fisheye_lens",
)
DOF_ATTRS = ("use_dof", "focus_distance", "aperture_fstop", "aperture_blades",
             "aperture_rotation", "aperture_ratio")


def _is_camera(_self, obj):
    return obj.type == "CAMERA"


class SHIRO_PG_cam_bookmark(PropertyGroup):
    name: StringProperty(name="Name", default="Camera Angle")
    # legacy fields (kept for old files)
    camera_name: StringProperty()
    matrix: FloatVectorProperty(size=16)
    lens: FloatProperty()
    ortho_scale: FloatProperty()
    is_ortho: BoolProperty(default=False)
    clip_start: FloatProperty()
    clip_end: FloatProperty()
    # new
    camera: PointerProperty(type=bpy.types.Object, poll=_is_camera, name="Camera")
    data_json: StringProperty()
    focus_object: PointerProperty(type=bpy.types.Object, name="Focus Object")
    kind: EnumProperty(items=[("CAMERA", "Camera", ""), ("VIEW", "Viewport", "")],
                       default="CAMERA")
    view_perspective: StringProperty(default="PERSP")
    view_lens: FloatProperty(default=50.0)


def target_camera(context):
    obj = context.active_object
    if obj is not None and obj.type == "CAMERA":
        return obj
    return context.scene.camera


def store_camera(item, cam_obj):
    cam = cam_obj.data
    item.kind = "CAMERA"
    item.camera = cam_obj
    item.camera_name = cam_obj.name
    item.matrix = [v for row in cam_obj.matrix_world for v in row]
    item.lens, item.ortho_scale = cam.lens, cam.ortho_scale
    item.is_ortho = cam.type == "ORTHO"
    item.clip_start, item.clip_end = cam.clip_start, cam.clip_end
    data = {a: getattr(cam, a) for a in CAM_ATTRS if hasattr(cam, a)}
    data["dof"] = {a: getattr(cam.dof, a) for a in DOF_ATTRS if hasattr(cam.dof, a)}
    item.data_json = json.dumps(data)
    item.focus_object = cam.dof.focus_object


def apply_camera(item, cam_obj):
    m = item.matrix
    cam_obj.matrix_world = Matrix((m[0:4], m[4:8], m[8:12], m[12:16]))
    cam = cam_obj.data
    if item.data_json:
        data = json.loads(item.data_json)
        dof = data.pop("dof", {})
        # type first: panoramic settings only exist meaningfully after it
        for attr in ("type",) + tuple(a for a in data if a != "type"):
            if attr in data:
                try:
                    setattr(cam, attr, data[attr])
                except (AttributeError, TypeError, ValueError):
                    pass
        for attr, value in dof.items():
            try:
                setattr(cam.dof, attr, value)
            except (AttributeError, TypeError, ValueError):
                pass
        cam.dof.focus_object = item.focus_object
    else:  # legacy bookmark
        cam.type = "ORTHO" if item.is_ortho else "PERSP"
        cam.lens, cam.ortho_scale = item.lens, item.ortho_scale
        cam.clip_start, cam.clip_end = item.clip_start, item.clip_end


def resolve_camera(context, item):
    cam = item.camera
    if cam is None and item.camera_name:
        cam = context.scene.objects.get(item.camera_name)
    if cam is None or cam.type != "CAMERA":
        cam = target_camera(context)
    return cam if cam is not None and cam.type == "CAMERA" else None


def _view_region(context):
    space = context.space_data
    if space is not None and space.type == "VIEW_3D":
        return space, space.region_3d
    for area in context.screen.areas if context.screen else ():
        if area.type == "VIEW_3D":
            return area.spaces.active, area.spaces.active.region_3d
    return None, None


def _bookmarks(context):
    return context.scene.camera_bookmarks


def _active(context):
    scene = context.scene
    i = scene.camera_bookmark_index
    return scene.camera_bookmarks[i] if 0 <= i < len(scene.camera_bookmarks) else None


class SHIRO_OT_cam_save(Operator):
    bl_idname = "shiro.cam_save"
    bl_label = "Save Camera Angle"
    bl_description = ("Store the active camera (or scene camera) as a bookmark. "
                      "Shift: store the viewport view instead")
    bl_options = {"REGISTER", "UNDO"}

    viewport: BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.viewport = event.shift
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        if self.viewport:
            space, rv3d = _view_region(context)
            if rv3d is None:
                self.report({"ERROR"}, "No 3D viewport")
                return {"CANCELLED"}
            item = scene.camera_bookmarks.add()
            item.kind = "VIEW"
            item.matrix = [v for row in rv3d.view_matrix for v in row]
            item.view_perspective = rv3d.view_perspective
            item.view_lens = space.lens
            item.name = f"View {len(scene.camera_bookmarks):02d}"
        else:
            cam = target_camera(context)
            if cam is None:
                self.report({"ERROR"}, "No camera found")
                return {"CANCELLED"}
            item = scene.camera_bookmarks.add()
            store_camera(item, cam)
            item.name = f"Angle {len(scene.camera_bookmarks):02d}"
        scene.camera_bookmark_index = len(scene.camera_bookmarks) - 1
        return {"FINISHED"}


class SHIRO_OT_cam_update(Operator):
    bl_idname = "shiro.cam_update"
    bl_label = "Update Bookmark"
    bl_description = "Overwrite the selected bookmark with the current camera"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active(context) is not None

    def execute(self, context):
        item = _active(context)
        if item.kind == "VIEW":
            space, rv3d = _view_region(context)
            if rv3d is None:
                return {"CANCELLED"}
            item.matrix = [v for row in rv3d.view_matrix for v in row]
            item.view_perspective, item.view_lens = rv3d.view_perspective, space.lens
            return {"FINISHED"}
        cam = item.camera or target_camera(context)
        if cam is None:
            self.report({"ERROR"}, "No camera found")
            return {"CANCELLED"}
        store_camera(item, cam)
        return {"FINISHED"}


class SHIRO_OT_cam_load(Operator):
    bl_idname = "shiro.cam_load"
    bl_label = "Load Angle"
    bl_description = "Move the camera to this angle. Ctrl: also insert keyframes"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)
    keyframe: BoolProperty(name="Insert Keyframe", default=False, options={"SKIP_SAVE"})
    make_active: BoolProperty(name="Make Scene Camera", default=True)

    def invoke(self, context, event):
        self.keyframe = event.ctrl
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        i = self.index if self.index >= 0 else scene.camera_bookmark_index
        if not (0 <= i < len(scene.camera_bookmarks)):
            return {"CANCELLED"}
        item = scene.camera_bookmarks[i]
        scene.camera_bookmark_index = i

        if item.kind == "VIEW":
            space, rv3d = _view_region(context)
            if rv3d is None:
                self.report({"ERROR"}, "No 3D viewport")
                return {"CANCELLED"}
            m = item.matrix
            rv3d.view_matrix = Matrix((m[0:4], m[4:8], m[8:12], m[12:16]))
            rv3d.view_perspective = item.view_perspective if item.view_perspective != "CAMERA" \
                else "PERSP"
            space.lens = item.view_lens
            return {"FINISHED"}

        cam = resolve_camera(context, item)
        if cam is None:
            self.report({"ERROR"}, "No matching camera in the scene")
            return {"CANCELLED"}
        apply_camera(item, cam)
        if self.make_active:
            scene.camera = cam
        if self.keyframe:
            cam.keyframe_insert("location")
            cam.keyframe_insert({"QUATERNION": "rotation_quaternion",
                                 "AXIS_ANGLE": "rotation_axis_angle"}.get(cam.rotation_mode,
                                                                          "rotation_euler"))
            cam.data.keyframe_insert("lens")
        return {"FINISHED"}


class SHIRO_OT_cam_delete(Operator):
    bl_idname = "shiro.cam_delete"
    bl_label = "Delete Bookmark"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active(context) is not None

    def execute(self, context):
        scene = context.scene
        i = scene.camera_bookmark_index
        scene.camera_bookmarks.remove(i)
        scene.camera_bookmark_index = min(i, len(scene.camera_bookmarks) - 1)
        return {"FINISHED"}


class SHIRO_OT_cam_move(Operator):
    bl_idname = "shiro.cam_move"
    bl_label = "Move Bookmark"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        scene = context.scene
        i = scene.camera_bookmark_index
        j = i - 1 if self.direction == "UP" else i + 1
        if not (0 <= i < len(scene.camera_bookmarks) and 0 <= j < len(scene.camera_bookmarks)):
            return {"CANCELLED"}
        scene.camera_bookmarks.move(i, j)
        scene.camera_bookmark_index = j
        return {"FINISHED"}


class SHIRO_OT_cam_new_camera(Operator):
    bl_idname = "shiro.cam_new_camera"
    bl_label = "New Camera from Bookmark"
    bl_description = "Create a separate camera object at this angle"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active(context) is not None

    def execute(self, context):
        item = _active(context)
        data = bpy.data.cameras.new(item.name)
        cam = bpy.data.objects.new(item.name, data)
        context.collection.objects.link(cam)
        m = item.matrix
        mat = Matrix((m[0:4], m[4:8], m[8:12], m[12:16]))
        if item.kind == "VIEW":
            cam.matrix_world = mat.inverted()
            data.lens = item.view_lens
        else:
            apply_camera(item, cam)
        item.kind, item.camera = "CAMERA", cam
        store_camera(item, cam)
        return {"FINISHED"}


class SHIRO_OT_cam_to_marker(Operator):
    bl_idname = "shiro.cam_to_marker"
    bl_label = "Camera Cut Here"
    bl_description = ("Add a marker at the current frame bound to this bookmark's camera "
                      "(creates a dedicated camera if needed), for camera switching")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active(context) is not None

    def execute(self, context):
        scene = context.scene
        item = _active(context)
        cam = item.camera if item.kind == "CAMERA" else None
        shared = cam is not None and sum(
            1 for b in scene.camera_bookmarks if b.camera == cam) > 1
        if cam is None or shared:
            # one camera per cut, otherwise every cut would show the same angle
            bpy.ops.shiro.cam_new_camera()
            cam = item.camera
        marker = scene.timeline_markers.new(item.name, frame=scene.frame_current)
        marker.camera = cam
        return {"FINISHED"}


class SHIRO_UL_cam_bookmarks(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False,
                 icon="VIEW3D" if item.kind == "VIEW" else "CAMERA_DATA")
        info = row.row()
        info.alignment = "RIGHT"
        info.active = False
        if item.kind == "VIEW":
            info.label(text="view")
        else:
            kind = "Ortho" if item.is_ortho else f"{item.lens:.0f}mm"
            if item.data_json:
                t = json.loads(item.data_json).get("type")
                if t == "PANO":
                    kind = "Pano"
            info.label(text=kind)
        row.operator("shiro.cam_load", text="", icon="PLAY", emboss=False).index = index


class SHIRO_PT_camera(Panel):
    bl_idname = "SHIRO_PT_camera"
    bl_label = "Camera Bookmarks"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row()
        row.template_list("SHIRO_UL_cam_bookmarks", "", scene, "camera_bookmarks",
                          scene, "camera_bookmark_index", rows=5)
        col = row.column(align=True)
        col.operator("shiro.cam_save", icon="ADD", text="")
        col.operator("shiro.cam_delete", icon="REMOVE", text="")
        col.separator()
        col.operator("shiro.cam_move", icon="TRIA_UP", text="").direction = "UP"
        col.operator("shiro.cam_move", icon="TRIA_DOWN", text="").direction = "DOWN"

        item = _active(context)
        hint = layout.row()
        hint.active = False
        hint.label(text="+ Shift: viewport view · Load + Ctrl: keyframe")
        if item is None:
            return
        big = layout.row(align=True)
        big.scale_y = 1.4
        big.operator("shiro.cam_load", text="Load", icon="PLAY").index = -1
        big.operator("shiro.cam_update", text="Update", icon="FILE_REFRESH")
        if item.kind == "CAMERA":
            layout.prop(item, "camera", icon="OUTLINER_OB_CAMERA")
        row = layout.row(align=True)
        row.operator("shiro.cam_new_camera", icon="OUTLINER_OB_CAMERA", text="New Camera")
        row.operator("shiro.cam_to_marker", icon="MARKER_HLT", text="Cut Here")


classes = (
    SHIRO_PG_cam_bookmark, SHIRO_OT_cam_save, SHIRO_OT_cam_update, SHIRO_OT_cam_load,
    SHIRO_OT_cam_delete, SHIRO_OT_cam_move, SHIRO_OT_cam_new_camera, SHIRO_OT_cam_to_marker,
    SHIRO_UL_cam_bookmarks, SHIRO_PT_camera,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.camera_bookmarks = CollectionProperty(type=SHIRO_PG_cam_bookmark)
    bpy.types.Scene.camera_bookmark_index = IntProperty(default=-1)


def unregister():
    del bpy.types.Scene.camera_bookmark_index
    del bpy.types.Scene.camera_bookmarks
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
