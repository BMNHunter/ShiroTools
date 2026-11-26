bl_info = {
    "name": "Ranger",
    "author": "bmilan",
    "version": (1, 4),
    "blender": (4, 2, 0),
    "location": "Dope Sheet (Timeline mode) > N-panel > Ranger",
    "description": "Manage frame-range presets for the timeline",
    "category": "Timeline",
}

import bpy

class TimelinePreset(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Preset Name", default="New Preset")
    start: bpy.props.IntProperty(name="Start Frame", default=1)
    end: bpy.props.IntProperty(name="End Frame", default=250)

class TimelineManagerProperties(bpy.types.PropertyGroup):
    presets: bpy.props.CollectionProperty(type=TimelinePreset)
    active_index: bpy.props.IntProperty(default=-1)

class TMANAGER_OT_edit_preset(bpy.types.Operator):
    """Edit selected preset"""
    bl_idname = "tmanager.edit_preset"
    bl_label = "Edit Preset"

    index: bpy.props.IntProperty()
    name: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()

    def invoke(self, context, event):
        preset = context.scene.timeline_manager_props.presets[self.index]
        self.name = preset.name
        self.start = preset.start
        self.end = preset.end
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Edit the selected preset:")
        layout.separator()
        layout.prop(self, "name")
        layout.prop(self, "start")
        layout.prop(self, "end")

    def execute(self, context):
        preset = context.scene.timeline_manager_props.presets[self.index]
        preset.name = self.name
        preset.start = self.start
        preset.end = self.end
        return {'FINISHED'}

class TMANAGER_OT_add_preset(bpy.types.Operator):
    """Add a new frame range preset"""
    bl_idname = "tmanager.add_preset"
    bl_label = "Add Frame Range Preset"

    name: bpy.props.StringProperty(name="Preset Name", default="New Preset")
    start: bpy.props.IntProperty(name="Start Frame", default=1)
    end: bpy.props.IntProperty(name="End Frame", default=250)

    def invoke(self, context, event):
        scene = context.scene
        self.start = scene.frame_start
        self.end = scene.frame_end
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Enter details for the new frame-range preset:")
        layout.separator()
        layout.prop(self, "name")
        layout.prop(self, "start")
        layout.prop(self, "end")

    def execute(self, context):
        scene = context.scene
        manager = scene.timeline_manager_props
        new_preset = manager.presets.add()
        new_preset.name = self.name
        new_preset.start = self.start
        new_preset.end = self.end
        manager.active_index = len(manager.presets) - 1
        return {'FINISHED'}

class TMANAGER_OT_apply_preset(bpy.types.Operator):
    bl_idname = "tmanager.apply_preset"
    bl_label = "Apply Preset"

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        preset = scene.timeline_manager_props.presets[self.index]
        scene.frame_start = preset.start
        scene.frame_end = preset.end
        scene.timeline_manager_props.active_index = self.index
        return {'FINISHED'}

class TMANAGER_OT_remove_preset(bpy.types.Operator):
    bl_idname = "tmanager.remove_preset"
    bl_label = "Remove Preset"

    index: bpy.props.IntProperty()

    def execute(self, context):
        manager = context.scene.timeline_manager_props
        manager.presets.remove(self.index)
        if manager.active_index >= len(manager.presets):
            manager.active_index = len(manager.presets) - 1
        return {'FINISHED'}

class TMANAGER_PT_panel(bpy.types.Panel):
    bl_label = "Timeline Manager"
    bl_idname = "TMANAGER_PT_panel"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Timeline Manager"
    bl_context = "timeline"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        manager = scene.timeline_manager_props

        row = layout.row(align=True)
        row.operator("tmanager.add_preset", icon='ADD', text="Add Preset")

        box = layout.box()
        if not manager.presets:
            box.label(text="No presets")
            return

        for i, p in enumerate(manager.presets):
            active = (i == manager.active_index)
            row = box.row(align=True)
            if active:
                row.alert = True

            op_apply = row.operator(
                "tmanager.apply_preset",
                text=f"{p.name} [{p.start}-{p.end}]",
                emboss=True
            )
            op_apply.index = i

            op_edit = row.operator("tmanager.edit_preset", text="", icon='GREASEPENCIL')
            op_edit.index = i

            op_remove = row.operator("tmanager.remove_preset", text="", icon='X')
            op_remove.index = i

classes = (
    TimelinePreset,
    TimelineManagerProperties,
    TMANAGER_OT_edit_preset,
    TMANAGER_OT_add_preset,
    TMANAGER_OT_apply_preset,
    TMANAGER_OT_remove_preset,
    TMANAGER_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.timeline_manager_props = bpy.props.PointerProperty(type=TimelineManagerProperties)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.timeline_manager_props

if __name__ == "__main__":
    register()
