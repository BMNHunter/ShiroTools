bl_info = {
    "name": "Geometry Nodes Shape Key Tools",
    "author": "Milan",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "Properties > Modifier Tab (when a Geometry Nodes modifier is active)",
    "description": "Adds 'Apply as Shape Key' and 'Save as Shape Key' buttons for Geometry Nodes modifiers",
    "category": "Object",
}

import bpy


class OBJECT_PT_geonodes_shapekey_tools(bpy.types.Panel):
    bl_label = "Shape Key Tools"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "modifier"

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None:
            return False
        md = obj.modifiers.active
        return md is not None and md.type == 'NODES'

    def draw(self, context):
        layout = self.layout
        md = context.object.modifiers.active

        row = layout.row(align=True)

        apply_op = row.operator(
            "object.modifier_apply_as_shapekey",
            text="Apply as Shape Key",
            icon='SHAPEKEY_DATA',
        )
        apply_op.modifier = md.name
        apply_op.keep_modifier = False

        save_op = row.operator(
            "object.modifier_apply_as_shapekey",
            text="Save as Shape Key",
            icon='SHAPEKEY_DATA',
        )
        save_op.modifier = md.name
        save_op.keep_modifier = True


classes = (OBJECT_PT_geonodes_shapekey_tools,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
