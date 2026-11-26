bl_info = {
    "name": "Bake Shape Key to Attribute",
    "author": "Milan (with Claude)",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "Properties > Object Data > Shape Keys",
    "description": "Bake the active shape key into a named per-vertex mesh attribute",
    "category": "Mesh",
}

import bpy
from bpy.props import StringProperty, EnumProperty, BoolProperty, PointerProperty
from bpy.types import Operator, PropertyGroup


MODE_ITEMS = [
    ('ABSOLUTE', "Absolute Position",
     "Store the shape key's vertex coordinates directly (use this for GN Set Position)"),
    ('RELATIVE', "Offset From Basis",
     "Store the shape key as an offset vector from the basis shape"),
]


class BSK_Props(PropertyGroup):
    attribute_name: StringProperty(
        name="Attribute Name",
        description="Name of the per-vertex attribute to create or overwrite",
        default="fixed_pos",
    )
    mode: EnumProperty(
        name="Mode",
        items=MODE_ITEMS,
        default='ABSOLUTE',
    )
    overwrite: BoolProperty(
        name="Overwrite if exists",
        description="If an attribute with this name already exists, replace its contents",
        default=True,
    )


class BSK_OT_bake(Operator):
    bl_idname = "object.bake_shapekey_to_attribute"
    bl_label = "Bake Shape Key to Attribute"
    bl_description = "Write the active shape key's per-vertex positions into a mesh attribute"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.object
        return (o is not None
                and o.type == 'MESH'
                and o.data.shape_keys is not None
                and o.active_shape_key is not None)

    def execute(self, context):
        props = context.scene.bsk_props
        name = props.attribute_name.strip()
        if not name:
            self.report({'ERROR'}, "Attribute name is empty")
            return {'CANCELLED'}

        obj = context.object
        me = obj.data
        kb = obj.active_shape_key
        basis = me.shape_keys.reference_key

        n = len(kb.data)
        if n != len(me.vertices):
            self.report({'ERROR'}, "Shape key data length doesn't match vertex count")
            return {'CANCELLED'}

        existing = me.attributes.get(name)
        if existing is not None:
            if not props.overwrite:
                self.report({'ERROR'},
                            f"Attribute '{name}' already exists (enable Overwrite)")
                return {'CANCELLED'}
            if existing.domain != 'POINT' or existing.data_type != 'FLOAT_VECTOR':
                try:
                    me.attributes.remove(existing)
                    existing = None
                except Exception as e:
                    self.report({'ERROR'}, f"Couldn't replace attribute: {e}")
                    return {'CANCELLED'}

        if existing is None:
            try:
                attr = me.attributes.new(name=name, type='FLOAT_VECTOR', domain='POINT')
            except Exception as e:
                self.report({'ERROR'}, f"Couldn't create attribute '{name}': {e}")
                return {'CANCELLED'}
        else:
            attr = existing

        if props.mode == 'ABSOLUTE':
            for i, v in enumerate(kb.data):
                attr.data[i].vector = v.co
        else:
            for i in range(n):
                attr.data[i].vector = kb.data[i].co - basis.data[i].co

        me.update()
        self.report({'INFO'},
                    f"Baked '{kb.name}' -> '{name}' ({n} verts, {props.mode})")
        return {'FINISHED'}


def _draw_in_shapekey_panel(self, context):
    layout = self.layout
    obj = context.object
    if not (obj and obj.type == 'MESH' and obj.data.shape_keys):
        return
    props = context.scene.bsk_props
    active_kb = obj.active_shape_key

    box = layout.box()
    box.label(text="Bake to Mesh Attribute", icon='RNA')
    if active_kb is None:
        box.label(text="No active shape key selected", icon='ERROR')
        return
    box.label(text=f'Source: "{active_kb.name}"')
    col = box.column(align=True)
    col.prop(props, "attribute_name", text="Name")
    col.prop(props, "mode", text="")
    col.prop(props, "overwrite", toggle=True)
    col.operator(BSK_OT_bake.bl_idname,
                 icon='EXPORT',
                 text=f'Bake "{active_kb.name}"')


classes = (BSK_Props, BSK_OT_bake)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.bsk_props = PointerProperty(type=BSK_Props)
    bpy.types.DATA_PT_shape_keys.append(_draw_in_shapekey_panel)


def unregister():
    try:
        bpy.types.DATA_PT_shape_keys.remove(_draw_in_shapekey_panel)
    except Exception:
        pass
    if hasattr(bpy.types.Scene, "bsk_props"):
        del bpy.types.Scene.bsk_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
