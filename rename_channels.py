import bpy

bl_info = {
    "name": "Graph Editor: Quick Rename",
    "author": "Gemini",
    "version": (1, 6),
    "blender": (4, 2, 0),
    "location": "Graph Editor > F2 or Right Click",
    "description": "Rename Groups or Create Proxy Channels with Smart Unit Detection",
    "category": "Animation",
}

def get_active_fcurve(context):
    """Helper to find the single active fcurve"""
    if context.selected_editable_fcurves:
        return context.selected_editable_fcurves[0]
    return None

def get_property_subtype(obj, data_path):
    """
    Robustly determines the subtype (Angle, Distance, etc) of a data path
    by resolving the actual property definition.
    """
    parent = obj
    prop_name = data_path
    
    try:
        # 1. Attempt to resolve parent object and property name from path
        if data_path.endswith('"]'):
            # Handle: modifiers["Name"]["Prop"] or ["Prop"]
            last_bracket = data_path.rfind('[')
            if last_bracket != -1:
                parent_path = data_path[:last_bracket]
                content = data_path[last_bracket+1:-1] # Inside brackets
                
                # Ensure it's a string key (quoted), not an array index
                if content.startswith('"') and content.endswith('"'):
                    prop_name = content[1:-1]
                    if parent_path:
                        parent = obj.path_resolve(parent_path)
                else:
                    # If it's an integer index access (e.g. location[0]), 
                    # we can't look up metadata on the *index*, we look up the array prop.
                    # We assume the path provided is the array base if possible, 
                    # but F-Curves usually give the full path. 
                    # If we are here, we likely can't get ID-Prop metadata for an array item easily.
                    pass 

        elif "." in data_path:
            # Handle: pose.bones["Bone"].rotation_euler
            last_dot = data_path.rfind('.')
            parent_path = data_path[:last_dot]
            prop_name = data_path[last_dot+1:]
            parent = obj.path_resolve(parent_path)

        # 2. Check Built-in RNA properties (e.g., rotation_euler)
        if hasattr(parent, "bl_rna"):
            prop_def = parent.bl_rna.properties.get(prop_name)
            if prop_def:
                st = prop_def.subtype
                # Map generic RNA subtypes to ID-Property compatible subtypes
                if st in {'EULER', 'QUATERNION', 'AXISANGLE', 'ANGLE'}: return 'ANGLE'
                if st in {'TRANSLATION', 'DISTANCE', 'LENGTH'}: return 'DISTANCE'
                if st in {'FACTOR', 'PERCENTAGE'}: return 'FACTOR'
                if st in {'COLOR', 'COLOR_GAMMA'}: return 'COLOR'
        
        # 3. Check ID Properties (Custom Props & Modifier Inputs)
        if hasattr(parent, "id_properties_ui"):
            try:
                ui_data = parent.id_properties_ui(prop_name).as_dict()
                if 'subtype' in ui_data:
                    st = ui_data['subtype']
                    # Normalize
                    if st in {'EULER', 'QUATERNION', 'AXISANGLE', 'ANGLE'}: return 'ANGLE'
                    if st in {'TRANSLATION', 'DISTANCE', 'LENGTH'}: return 'DISTANCE'
                    return st
            except:
                pass
                
    except:
        pass

    # 4. Fallback Heuristics (String matching) if resolution failed
    path_lower = data_path.lower()
    if "rotation" in path_lower or "angle" in path_lower or "phase" in path_lower:
        return 'ANGLE'
    if "location" in path_lower or "position" in path_lower:
        return 'DISTANCE'
        
    return 'NONE'

class GRAPH_OT_rename_active_group(bpy.types.Operator):
    """Rename the Group of the selected F-Curve"""
    bl_idname = "graph.rename_active_group"
    bl_label = "Rename Group"
    bl_options = {'REGISTER', 'UNDO'}

    new_name: bpy.props.StringProperty(name="New Group Name")

    @classmethod
    def poll(cls, context):
        return (context.area.type == 'GRAPH_EDITOR' and 
                context.object and 
                context.object.animation_data and
                context.object.animation_data.action)

    def invoke(self, context, event):
        fcurve = get_active_fcurve(context)
        if fcurve and fcurve.group:
            self.new_name = fcurve.group.name
        else:
            self.new_name = "New Group"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        fcurves = context.selected_editable_fcurves
        if not fcurves:
            self.report({'WARNING'}, "No channel selected to group.")
            return {'CANCELLED'}

        target_group = fcurves[0].group
        action = context.object.animation_data.action

        if target_group:
            target_group.name = self.new_name
        else:
            new_grp = action.groups.new(name=self.new_name)
            for fc in fcurves:
                fc.group = new_grp

        context.area.tag_redraw()
        return {'FINISHED'}


class GRAPH_OT_create_proxy_channel(bpy.types.Operator):
    """Creates a Custom Property, moves animation to it, and drives the original."""
    bl_idname = "graph.create_proxy_channel"
    bl_label = "Rename Channel (Proxy)"
    bl_options = {'REGISTER', 'UNDO'}

    new_name: bpy.props.StringProperty(name="New Channel Name")

    @classmethod
    def poll(cls, context):
        return (context.area.type == 'GRAPH_EDITOR' and 
                context.object and 
                context.object.animation_data and
                context.object.animation_data.action)

    def invoke(self, context, event):
        fcurve = get_active_fcurve(context)
        if not fcurve:
            self.report({'WARNING'}, "Please select a channel first.")
            return {'CANCELLED'}
            
        path = fcurve.data_path
        # Suggest a clean name
        if '"' in path:
            self.new_name = path.split('"')[-2]
        elif "[" in path and "]" in path:
            self.new_name = path.split('"')[-2]
        else:
            self.new_name = path.split('.')[-1]
            
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.object
        original_fcurve = get_active_fcurve(context)
        
        if not original_fcurve:
             self.report({'WARNING'}, "No channel selected.")
             return {'CANCELLED'}

        # 1. Setup New Property Name
        prop_name = self.new_name
        if prop_name in obj:
            self.report({'WARNING'}, f"Property '{prop_name}' already exists. Aborting.")
            return {'CANCELLED'}

        # 2. Determine Subtype & Value Type
        is_integer = False
        subtype = get_property_subtype(obj, original_fcurve.data_path)

        # Check if it is an integer (Boolean/Int sockets)
        try:
            actual_val = obj.path_resolve(original_fcurve.data_path)
            if isinstance(actual_val, (list, tuple, bpy.types.bpy_prop_array)):
                if original_fcurve.array_index < len(actual_val):
                    actual_val = actual_val[original_fcurve.array_index]
            
            if isinstance(actual_val, int) and not isinstance(actual_val, bool): 
                # Python bools are ints, but we usually want booleans to stay min/max 0-1
                # Strict int check usually implies Integer socket or similar
                is_integer = True
        except:
            pass

        # 3. Create the Property
        current_val = original_fcurve.evaluate(context.scene.frame_current)
        
        if is_integer:
            obj[prop_name] = int(current_val)
            obj.id_properties_ui(prop_name).update(min=-10000, max=10000)
        else:
            obj[prop_name] = current_val
            # Apply the discovered subtype (ANGLE, DISTANCE, etc)
            obj.id_properties_ui(prop_name).update(
                min=-10000.0, 
                max=10000.0, 
                subtype=subtype
            )

        # 4. Create New F-Curve
        new_path = f'["{prop_name}"]'
        action = obj.animation_data.action
        new_fcurve = action.fcurves.new(data_path=new_path)
        
        if original_fcurve.group:
            new_fcurve.group = original_fcurve.group

        # 5. Copy Keyframes
        kp_source = original_fcurve.keyframe_points
        new_fcurve.keyframe_points.add(len(kp_source))
        
        for i, kp in enumerate(kp_source):
            new_kp = new_fcurve.keyframe_points[i]
            new_kp.co = kp.co
            new_kp.interpolation = kp.interpolation
            new_kp.easing = kp.easing
            new_kp.handle_left = kp.handle_left
            new_kp.handle_right = kp.handle_right
            new_kp.handle_left_type = kp.handle_left_type
            new_kp.handle_right_type = kp.handle_right_type

        new_fcurve.update()

        # 6. Add Driver to Original
        original_path = original_fcurve.data_path
        original_index = original_fcurve.array_index
        action.fcurves.remove(original_fcurve)
        
        try:
            d_fcurve = obj.driver_add(original_path, original_index)
        except TypeError:
            d_fcurve = obj.driver_add(original_path)
            
        drv = d_fcurve.driver
        drv.type = 'AVERAGE'
        
        var = drv.variables.new()
        var.name = "val"
        var.type = 'SINGLE_PROP'
        
        target = var.targets[0]
        target.id_type = 'OBJECT'
        target.id = obj
        target.data_path = new_path

        type_label = "Int" if is_integer else f"Float ({subtype})"
        self.report({'INFO'}, f"Renamed to '{prop_name}' [{type_label}]")
        context.area.tag_redraw()
        
        return {'FINISHED'}

def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(GRAPH_OT_rename_active_group.bl_idname, text="Rename Group", icon='OUTLINER_COLLECTION')
    self.layout.operator(GRAPH_OT_create_proxy_channel.bl_idname, text="Rename Channel (Proxy)", icon='DRIVER')

addon_keymaps = []

def register():
    bpy.utils.register_class(GRAPH_OT_rename_active_group)
    bpy.utils.register_class(GRAPH_OT_create_proxy_channel)
    
    menus_to_try = ["GRAPH_MT_context_menu", "GRAPH_MT_channel_context_menu"]
    for menu_name in menus_to_try:
        if hasattr(bpy.types, menu_name):
            getattr(bpy.types, menu_name).append(menu_func)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='Graph Editor', space_type='GRAPH_EDITOR')
        kmi = km.keymap_items.new(GRAPH_OT_rename_active_group.bl_idname, 'F2', 'PRESS')
        addon_keymaps.append((km, kmi))
        kmi2 = km.keymap_items.new(GRAPH_OT_create_proxy_channel.bl_idname, 'F2', 'PRESS', shift=True)
        addon_keymaps.append((km, kmi2))

def unregister():
    bpy.utils.unregister_class(GRAPH_OT_rename_active_group)
    bpy.utils.unregister_class(GRAPH_OT_create_proxy_channel)
    
    menus_to_try = ["GRAPH_MT_context_menu", "GRAPH_MT_channel_context_menu"]
    for menu_name in menus_to_try:
        if hasattr(bpy.types, menu_name):
            getattr(bpy.types, menu_name).remove(menu_func)

    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

if __name__ == "__main__":
    register()