bl_info = {
    "name": "Marker Jump Panel",
    "author": "bmilan",
    "version": (1, 1),
    "blender": (5, 0, 0),
    "location": "Dope Sheet > N-panel > Marker Jump",
    "description": "Search and jump to markers.",
    "category": "Animation",
}

import bpy

class MarkerJumpProperties(bpy.types.PropertyGroup):
    search_filter: bpy.props.StringProperty(
        name="Search",
        description="Filter markers by name",
        default="",
    )
    invert_filter: bpy.props.BoolProperty(
        name="Invert Filter",
        description="Show markers NOT matching the search",
        default=False
    )

class MJUMP_OT_jump_to_marker(bpy.types.Operator):
    """Move playhead to selected marker"""
    bl_idname = "mjump.jump_to"
    bl_label = "Jump to Marker"
    bl_options = {'REGISTER', 'UNDO'}

    marker_name: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        marker = scene.timeline_markers.get(self.marker_name)
        if not marker:
            self.report({'WARNING'}, "Marker not found")
            return {'CANCELLED'}

        scene.frame_current = marker.frame
        return {'FINISHED'}


class MJUMP_OT_invert_search(bpy.types.Operator):
    """Invert marker search results"""
    bl_idname = "mjump.invert_search"
    bl_label = "Invert Search Results"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.marker_jump_props
        props.invert_filter = not props.invert_filter
        return {'FINISHED'}

class MJUMP_PT_panel(bpy.types.Panel):
    bl_label = "Marker Jump"
    bl_idname = "MJUMP_PT_panel"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Marker Jump"

    @classmethod
    def poll(cls, context):
        area = context.area
        return area and area.type == 'DOPESHEET_EDITOR'

    def draw(self, context):
        layout = self.layout
        props = context.scene.marker_jump_props

        row = layout.row(align=True)
        row.prop(props, "search_filter", text="", icon="VIEWZOOM")
        icon = 'ARROW_LEFTRIGHT' if props.invert_filter else 'FILTER'
        row.operator("mjump.invert_search", text="", icon=icon)

        markers = context.scene.timeline_markers
        flt = (props.search_filter or "").strip().lower()
        invert = props.invert_filter

        if not markers:
            layout.label(text="No markers found")
            return

        box = layout.box()

        shown_i = 0

        for m in sorted(markers, key=lambda x: x.frame):
            name_lower = (m.name or "").lower()

            if flt:
                contains = (flt in name_lower)
                if invert and contains:
                    continue
                if not invert and not contains:
                    continue

            row = box.row(align=True)
            if shown_i % 2 == 0:
                pass
            else:
                row.scale_y = 1.0

            row.label(text=f"{m.name}  (F{m.frame})", icon='MARKER')

            op = row.operator("mjump.jump_to", text="", icon='PLAY')
            op.marker_name = m.name

            shown_i += 1

        if shown_i == 0:
            layout.label(text="No markers match the filter")

classes = (
    MarkerJumpProperties,
    MJUMP_OT_jump_to_marker,
    MJUMP_OT_invert_search,
    MJUMP_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.marker_jump_props = bpy.props.PointerProperty(type=MarkerJumpProperties)

def unregister():
    del bpy.types.Scene.marker_jump_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
