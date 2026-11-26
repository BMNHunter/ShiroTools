bl_info = {
    "name": "Invert Vertex Group Sides",
    "author": "Milan",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "Object Data Properties > Vertex Groups menu / Search",
    "description": "Swap .L <-> .R suffixes on all vertex groups of selected objects",
    "category": "Rigging",
}

import bpy


def swap_side(name):
    pairs = [(".L", ".R"), (".l", ".r"), ("_L", "_R"), ("_l", "_r")]
    for a, b in pairs:
        if name.endswith(a):
            return name[: -len(a)] + b
        if name.endswith(b):
            return name[: -len(b)] + a
    return name


class OBJECT_OT_invert_vgroup_sides(bpy.types.Operator):
    bl_idname = "object.invert_vgroup_sides"
    bl_label = "Invert Vertex Group Sides"
    bl_description = "Swap .L and .R suffixes on all vertex groups"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        total = 0
        for obj in context.selected_objects:
            if obj.type != "MESH":
                continue

            # Collect new names first to avoid collisions during rename.
            renames = []
            for vg in obj.vertex_groups:
                new = swap_side(vg.name)
                if new != vg.name:
                    renames.append((vg, new))

            # Temporary placeholder names dodge "name already exists" clashes
            # when an .L and its matching .R both swap into each other.
            for i, (vg, new) in enumerate(renames):
                vg.name = "__swap_tmp_%d__" % i

            for vg, new in renames:
                vg.name = new
                total += 1

        self.report({"INFO"}, "Swapped %d vertex group(s)" % total)
        return {"FINISHED"}


def menu_func(self, context):
    self.layout.operator(OBJECT_OT_invert_vgroup_sides.bl_idname)


def register():
    bpy.utils.register_class(OBJECT_OT_invert_vgroup_sides)
    bpy.types.MESH_MT_vertex_group_context_menu.append(menu_func)


def unregister():
    bpy.types.MESH_MT_vertex_group_context_menu.remove(menu_func)
    bpy.utils.unregister_class(OBJECT_OT_invert_vgroup_sides)


if __name__ == "__main__":
    register()
