bl_info = {
    "name": "Marker Jump Panel",
    "author": "YourName",
    "version": (1, 3),
    "blender": (4, 2, 0),
    "location": "Dope Sheet (Timeline mode) > N-panel > Marker Jump",
    "description": "Search, jump, and invert marker search results",
    "category": "Timeline",
}

import bpy

# ------------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------------

class MarkerJumpProperties(bpy.types.PropertyGroup):
    search_filter: bpy.props.StringProperty(
        name="Search",
        description="Filter markers by name",
        default="",
        update=lambda self, ctx: None
    )
    invert_filter: bpy.props.BoolProperty(
        name="Invert Filter",
        description="Show markers NOT matching the search",
        default=False
    )

# ------------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------------

class MJUMP_OT_jump_to_marker(bpy.types.Operator):
    """Move playhead to selected marker"""
    bl_idname = "marker.jump_to"
    bl_label = "Jump to Marker"

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
    bl_idname = "marker.invert_search"
    bl_label = "Invert Search Results"

    def execute(self, context):
        props = context.scene.marker_jump_props
        props.invert_filter = not props.invert_filter
        return {'FINISHED'}

# ------------------------------------------------------------------------
# Panel
# ------------------------------------------------------------------------

class MJUMP_PT_panel(bpy.types.Panel):
    bl_label = "Marker Jump"
    bl_idname = "MJUMP_PT_panel"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Marker Jump"
    bl_context = "timeline"

    def draw(self, context):
        layout = self.layout
        props = context.scene.marker_jump_props

        # TOP: Search + invert toggle in one row
        row = layout.row(align=True)
        row.prop(props, "search_filter", text="", icon="VIEWZOOM")
        icon = 'ARROW_LEFTRIGHT' if props.invert_filter else 'FILTER'
        row.operator("marker.invert_search", text="", icon=icon)

        markers = context.scene.timeline_markers
        flt = props.search_filter.lower()
        invert = props.invert_filter

        if not markers:
            layout.label(text="No markers found")
            return

        # Scrollable box for marker list
        box = layout.box()
        for i, m in enumerate(sorted(markers, key=lambda x: x.frame)):
            name_lower = m.name.lower()
            # Apply search + invert
            if flt:
                if invert and flt in name_lower:
                    continue
                if not invert and flt not in name_lower:
                    continue

            # Alternating background for visibility
            if i % 2 == 0:
                row = box.row(align=True)
            else:
                row = box.row(align=True)
                row.scale_y = 1.0

            # Marker label with subtle icon
            row.label(text=f"{m.name}  (F{m.frame})", icon='MARKER')

            # Jump button icon-only
            op = row.operator("marker.jump_to", text="", icon='PLAY')
            op.marker_name = m.name

# ------------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------------

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
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.marker_jump_props

if __name__ == "__main__":
    register()
