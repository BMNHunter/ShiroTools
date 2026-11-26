bl_info = {
    "name": "Camera Bookmarks",
    "author": "Milan",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Camera",
    "description": "Save, list, and jump between camera angles, stored per scene",
    "category": "3D View",
}

import bpy
from mathutils import Matrix
from bpy.props import (
    StringProperty,
    FloatProperty,
    FloatVectorProperty,
    BoolProperty,
    CollectionProperty,
    IntProperty,
)
from bpy.types import PropertyGroup, Operator, UIList, Panel


def get_target_camera(context):
    """Pick the camera to save from or apply to: the active object if
    it's a camera, otherwise the scene's assigned camera."""
    obj = context.active_object
    if obj and obj.type == 'CAMERA':
        return obj
    return context.scene.camera


def store_camera_state(item, cam_obj):
    item.camera_name = cam_obj.name
    item.matrix = [v for row in cam_obj.matrix_world for v in row]
    item.lens = cam_obj.data.lens
    item.ortho_scale = cam_obj.data.ortho_scale
    item.is_ortho = cam_obj.data.type == 'ORTHO'
    item.clip_start = cam_obj.data.clip_start
    item.clip_end = cam_obj.data.clip_end


class CAMBM_CameraBookmark(PropertyGroup):
    name: StringProperty(name="Name", default="Camera Angle")
    camera_name: StringProperty()
    matrix: FloatVectorProperty(size=16)
    lens: FloatProperty()
    ortho_scale: FloatProperty()
    is_ortho: BoolProperty(default=False)
    clip_start: FloatProperty()
    clip_end: FloatProperty()


class CAMBM_OT_save(Operator):
    bl_idname = "cambm.save"
    bl_label = "Save Camera Angle"
    bl_description = "Store the current camera's transform and lens as a new bookmark"

    def execute(self, context):
        cam_obj = get_target_camera(context)
        if not cam_obj or cam_obj.type != 'CAMERA':
            self.report({'ERROR'}, "No active camera found")
            return {'CANCELLED'}

        scene = context.scene
        item = scene.camera_bookmarks.add()
        item.name = f"Angle {len(scene.camera_bookmarks):02d}"
        store_camera_state(item, cam_obj)

        scene.camera_bookmark_index = len(scene.camera_bookmarks) - 1
        return {'FINISHED'}


class CAMBM_OT_update(Operator):
    bl_idname = "cambm.update"
    bl_label = "Update Selected"
    bl_description = "Overwrite the selected bookmark with the current camera transform"

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return 0 <= scene.camera_bookmark_index < len(scene.camera_bookmarks)

    def execute(self, context):
        cam_obj = get_target_camera(context)
        if not cam_obj or cam_obj.type != 'CAMERA':
            self.report({'ERROR'}, "No active camera found")
            return {'CANCELLED'}

        scene = context.scene
        item = scene.camera_bookmarks[scene.camera_bookmark_index]
        store_camera_state(item, cam_obj)
        return {'FINISHED'}


class CAMBM_OT_load(Operator):
    bl_idname = "cambm.load"
    bl_label = "Load Angle"
    bl_description = "Move the camera to this saved angle"

    index: IntProperty()

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return len(scene.camera_bookmarks) > 0

    def execute(self, context):
        scene = context.scene
        if self.index < 0 or self.index >= len(scene.camera_bookmarks):
            return {'CANCELLED'}

        item = scene.camera_bookmarks[self.index]
        cam_obj = scene.objects.get(item.camera_name)
        if not cam_obj or cam_obj.type != 'CAMERA':
            cam_obj = get_target_camera(context)
        if not cam_obj or cam_obj.type != 'CAMERA':
            self.report({'ERROR'}, "No matching camera found in the scene")
            return {'CANCELLED'}

        m = item.matrix
        cam_obj.matrix_world = Matrix((m[0:4], m[4:8], m[8:12], m[12:16]))
        cam_obj.data.lens = item.lens
        cam_obj.data.ortho_scale = item.ortho_scale
        cam_obj.data.type = 'ORTHO' if item.is_ortho else 'PERSP'
        cam_obj.data.clip_start = item.clip_start
        cam_obj.data.clip_end = item.clip_end

        scene.camera_bookmark_index = self.index
        return {'FINISHED'}


class CAMBM_OT_delete(Operator):
    bl_idname = "cambm.delete"
    bl_label = "Delete"
    bl_description = "Remove the selected bookmark"

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return 0 <= scene.camera_bookmark_index < len(scene.camera_bookmarks)

    def execute(self, context):
        scene = context.scene
        index = scene.camera_bookmark_index
        scene.camera_bookmarks.remove(index)
        scene.camera_bookmark_index = min(index, len(scene.camera_bookmarks) - 1)
        return {'FINISHED'}


class CAMBM_OT_move(Operator):
    bl_idname = "cambm.move"
    bl_label = "Move Bookmark"
    bl_description = "Reorder the selected bookmark"

    direction: StringProperty()

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return 0 <= scene.camera_bookmark_index < len(scene.camera_bookmarks)

    def execute(self, context):
        scene = context.scene
        index = scene.camera_bookmark_index
        new_index = index + (-1 if self.direction == 'UP' else 1)
        if new_index < 0 or new_index >= len(scene.camera_bookmarks):
            return {'CANCELLED'}

        scene.camera_bookmarks.move(index, new_index)
        scene.camera_bookmark_index = new_index
        return {'FINISHED'}


class CAMBM_UL_bookmarks(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='CAMERA_DATA')
        label = "Ortho" if item.is_ortho else f"{item.lens:.0f}mm"
        row.label(text=label)


class CAMBM_PT_panel(Panel):
    bl_label = "Camera Bookmarks"
    bl_idname = "CAMBM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Camera"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        row = layout.row()
        row.template_list(
            "CAMBM_UL_bookmarks", "",
            scene, "camera_bookmarks",
            scene, "camera_bookmark_index",
            rows=5,
        )

        col = row.column(align=True)
        col.operator("cambm.save", icon='ADD', text="")
        col.operator("cambm.delete", icon='REMOVE', text="")
        col.separator()
        col.operator("cambm.move", icon='TRIA_UP', text="").direction = 'UP'
        col.operator("cambm.move", icon='TRIA_DOWN', text="").direction = 'DOWN'

        layout.separator()
        load_row = layout.row(align=True)
        load_row.scale_y = 1.4
        load_op = load_row.operator("cambm.load", text="Load Angle", icon='PLAY')
        load_op.index = scene.camera_bookmark_index

        layout.operator("cambm.update", text="Update Selected", icon='FILE_REFRESH')


classes = (
    CAMBM_CameraBookmark,
    CAMBM_OT_save,
    CAMBM_OT_update,
    CAMBM_OT_load,
    CAMBM_OT_delete,
    CAMBM_OT_move,
    CAMBM_UL_bookmarks,
    CAMBM_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.camera_bookmarks = CollectionProperty(type=CAMBM_CameraBookmark)
    bpy.types.Scene.camera_bookmark_index = IntProperty(default=-1)


def unregister():
    del bpy.types.Scene.camera_bookmark_index
    del bpy.types.Scene.camera_bookmarks
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
