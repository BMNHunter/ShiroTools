# OPTIMIZED ONE-CLICK BAKER
# Full optimized rewrite with restored auto-setup system.

bl_info = {
    "name": "One-Click Baker Optimized",
    "author": "Milan + ChatGPT",
    "version": (2, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Bake",
    "description": "Fast one-click baking with auto-setup",
    "category": "Render",
}

import bpy
import os

from bpy.props import (
    BoolProperty,
    EnumProperty,
    StringProperty,
    CollectionProperty,
    PointerProperty,
    IntProperty,
)

from bpy.types import (
    Panel,
    Operator,
    PropertyGroup,
    UIList,
)


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

BAKE_NODE_NAME = "OCB_BakeTarget"
THROWAWAY_IMAGE_NAME = "_OCB_throwaway"


RESOLUTION_ITEMS = [
    ('512', "512", "512 x 512"),
    ('1024', "1K", "1024 x 1024"),
    ('2048', "2K", "2048 x 2048"),
    ('4096', "4K", "4096 x 4096"),
    ('8192', "8K", "8192 x 8192"),
]


BAKE_TYPE_CONFIG = {
    'DIFFUSE': {
        'cycles': 'DIFFUSE',
        'color_space': 'sRGB',
        'float': False,
        'folder': 'Diffuse',
        'suffix': 'Diffuse',
    },

    'NORMAL': {
        'cycles': 'NORMAL',
        'color_space': 'Non-Color',
        'float': True,
        'folder': 'Normal',
        'suffix': 'Normal',
    },

    'ROUGHNESS': {
        'cycles': 'ROUGHNESS',
        'color_space': 'Non-Color',
        'float': False,
        'folder': 'Roughness',
        'suffix': 'Roughness',
    },
}


# -----------------------------------------------------------------------------
# PROPERTY GROUPS
# -----------------------------------------------------------------------------

class OCB_MaterialItem(PropertyGroup):
    name: StringProperty(name="Material Name")
    bake: BoolProperty(name="Bake", default=True)


class OCB_Settings(PropertyGroup):

    materials: CollectionProperty(type=OCB_MaterialItem)
    materials_index: IntProperty(default=0)

    bake_diffuse: BoolProperty(name="Diffuse", default=True)
    bake_normal: BoolProperty(name="Normal", default=False)
    bake_roughness: BoolProperty(name="Roughness", default=False)

    resolution: EnumProperty(
        name="Resolution",
        items=RESOLUTION_ITEMS,
        default='2048',
    )

    auto_setup: BoolProperty(
        name="Auto Setup",
        default=True,
    )


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------


def refresh_material_list(context):

    settings = context.scene.ocb_settings
    obj = context.active_object

    settings.materials.clear()

    if not obj or obj.type != 'MESH':
        return

    for slot in obj.material_slots:

        if slot.material is None:
            continue

        item = settings.materials.add()
        item.name = slot.material.name
        item.bake = True



def _sanitize(name):

    invalid = '<>:"/\|?*'
    return ''.join(c if c not in invalid else '_' for c in name)



def _textures_root():

    blend_dir = os.path.dirname(bpy.data.filepath)
    return os.path.join(blend_dir, 'Textures')



def _get_or_create_throwaway():

    img = bpy.data.images.get(THROWAWAY_IMAGE_NAME)

    if img is None:

        img = bpy.data.images.new(
            name=THROWAWAY_IMAGE_NAME,
            width=64,
            height=64,
            alpha=False,
        )

    return img



def _prepare_all_bake_nodes(obj):
    """Prepare bake nodes ONCE."""

    throwaway = _get_or_create_throwaway()

    for slot in obj.material_slots:

        mat = slot.material

        if mat is None:
            continue

        if not mat.use_nodes:
            continue

        nt = mat.node_tree

        if nt is None:
            continue

        bake_node = nt.nodes.get(BAKE_NODE_NAME)

        if bake_node is None or bake_node.type != 'TEX_IMAGE':

            if bake_node is not None:
                nt.nodes.remove(bake_node)

            bake_node = nt.nodes.new('ShaderNodeTexImage')
            bake_node.name = BAKE_NODE_NAME
            bake_node.label = 'OCB Bake Target'
            bake_node.location = (-600, -400)

        bake_node.image = throwaway



def _set_active_bake_image(material, image, color_space):

    if not material.use_nodes:
        return

    nt = material.node_tree

    if nt is None:
        return

    bake_node = nt.nodes.get(BAKE_NODE_NAME)

    if bake_node is None:
        return

    bake_node.image = image
    image.colorspace_settings.name = color_space

    for n in nt.nodes:
        n.select = False

    bake_node.select = True
    nt.nodes.active = bake_node



def _make_or_get_image(name, width, height, is_float=False):

    img = bpy.data.images.get(name)

    if img is not None:

        same_size = (
            img.size[0] == width and
            img.size[1] == height
        )

        if same_size and img.is_float == is_float:
            return img

        bpy.data.images.remove(img)

    return bpy.data.images.new(
        name=name,
        width=width,
        height=height,
        alpha=False,
        float_buffer=is_float,
    )



def _load_or_get_image(filepath, color_space):

    abspath = bpy.path.abspath(filepath)

    for img in bpy.data.images:

        if img.filepath and bpy.path.abspath(img.filepath) == abspath:

            try:
                img.reload()
            except RuntimeError:
                pass

            img.colorspace_settings.name = color_space
            return img

    img = bpy.data.images.load(filepath, check_existing=True)
    img.colorspace_settings.name = color_space

    return img


# -----------------------------------------------------------------------------
# AUTO SETUP
# -----------------------------------------------------------------------------


def _setup_material_tree(material, baked_paths):

    if not material.use_nodes:
        material.use_nodes = True

    nt = material.node_tree

    if nt is None:
        return

    nodes = nt.nodes
    links = nt.links

    # -----------------------------------------------------------------
    # FIND / CREATE OUTPUT
    # -----------------------------------------------------------------

    output = None

    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL':
            output = n
            break

    if output is None:
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (1200, 0)

    # -----------------------------------------------------------------
    # HELPERS
    # -----------------------------------------------------------------

    def get_or_create(name, node_type, location):

        node = nodes.get(name)

        if node is None or node.bl_idname != node_type:

            if node is not None:
                nodes.remove(node)

            node = nodes.new(node_type)
            node.name = name
            node.location = location

        return node

    def ensure_link(from_socket, to_socket):

        for l in links:
            if l.from_socket == from_socket and l.to_socket == to_socket:
                return

        links.new(from_socket, to_socket)

    # -----------------------------------------------------------------
    # CREATE NODES
    # -----------------------------------------------------------------

    texcoord = get_or_create(
        'OCB_TexCoord',
        'ShaderNodeTexCoord',
        (-1400, 0)
    )

    mapping = get_or_create(
        'OCB_Mapping',
        'ShaderNodeMapping',
        (-1150, 0)
    )

    diffuse_tex = get_or_create(
        'OCB_Img_Diffuse',
        'ShaderNodeTexImage',
        (-850, 300)
    )

    roughness_tex = get_or_create(
        'OCB_Img_Roughness',
        'ShaderNodeTexImage',
        (-850, 0)
    )

    normal_tex = get_or_create(
        'OCB_Img_Normal',
        'ShaderNodeTexImage',
        (-850, -300)
    )

    normal_map = get_or_create(
        'OCB_NormalMap',
        'ShaderNodeNormalMap',
        (-500, -300)
    )

    bsdf = get_or_create(
        'OCB_BSDF',
        'ShaderNodeBsdfPrincipled',
        (-150, 0)
    )

    # -----------------------------------------------------------------
    # LOAD IMAGES
    # -----------------------------------------------------------------

    if 'DIFFUSE' in baked_paths:

        img = _load_or_get_image(
            baked_paths['DIFFUSE'],
            'sRGB'
        )

        diffuse_tex.image = img

    if 'ROUGHNESS' in baked_paths:

        img = _load_or_get_image(
            baked_paths['ROUGHNESS'],
            'Non-Color'
        )

        roughness_tex.image = img

    if 'NORMAL' in baked_paths:

        img = _load_or_get_image(
            baked_paths['NORMAL'],
            'Non-Color'
        )

        normal_tex.image = img

    # -----------------------------------------------------------------
    # CREATE LINKS
    # -----------------------------------------------------------------

    ensure_link(
        texcoord.outputs['UV'],
        mapping.inputs['Vector']
    )

    # Only wire textures that were actually baked

    if 'DIFFUSE' in baked_paths:

        ensure_link(
            mapping.outputs['Vector'],
            diffuse_tex.inputs['Vector']
        )

        ensure_link(
            diffuse_tex.outputs['Color'],
            bsdf.inputs['Base Color']
        )

    if 'ROUGHNESS' in baked_paths:

        ensure_link(
            mapping.outputs['Vector'],
            roughness_tex.inputs['Vector']
        )

        ensure_link(
            roughness_tex.outputs['Color'],
            bsdf.inputs['Roughness']
        )

    if 'NORMAL' in baked_paths:

        ensure_link(
            mapping.outputs['Vector'],
            normal_tex.inputs['Vector']
        )

        ensure_link(
            normal_tex.outputs['Color'],
            normal_map.inputs['Color']
        )

        ensure_link(
            normal_map.outputs['Normal'],
            bsdf.inputs['Normal']
        )

    ensure_link(
        bsdf.outputs['BSDF'],
        output.inputs['Surface']
    )


# -----------------------------------------------------------------------------
# UI LIST
# -----------------------------------------------------------------------------

class OCB_UL_materials(UIList):

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):

        row = layout.row(align=True)
        row.prop(item, 'bake', text='')
        row.label(text=item.name, icon='MATERIAL')


# -----------------------------------------------------------------------------
# OPERATORS
# -----------------------------------------------------------------------------

class OCB_OT_refresh_materials(Operator):

    bl_idname = "ocb.refresh_materials"
    bl_label = "Refresh"

    def execute(self, context):

        refresh_material_list(context)
        return {'FINISHED'}


class OCB_OT_bake(Operator):

    bl_idname = "ocb.bake"
    bl_label = "Bake"

    @classmethod
    def poll(cls, context):

        obj = context.active_object
        return obj and obj.type == 'MESH'

    def _bake_one(self,
                  context,
                  obj,
                  material,
                  bake_type,
                  resolution,
                  out_dir):

        scene = context.scene
        cfg = BAKE_TYPE_CONFIG[bake_type]

        image_name = (
            f"{_sanitize(obj.name)}_"
            f"{_sanitize(material.name)}_"
            f"{cfg['suffix']}"
        )

        image = _make_or_get_image(
            image_name,
            resolution,
            resolution,
            cfg['float']
        )

        _set_active_bake_image(
            material,
            image,
            cfg['color_space']
        )

        if bake_type == 'DIFFUSE':

            scene.render.bake.use_pass_direct = False
            scene.render.bake.use_pass_indirect = False
            scene.render.bake.use_pass_color = True

        bpy.ops.object.bake(type=cfg['cycles'])

        filepath = os.path.join(
            out_dir,
            image_name + '.png'
        )

        image.save_render(filepath=filepath, scene=scene)

        image.filepath = filepath
        image.source = 'FILE'

        return filepath

    def execute(self, context):

        scene = context.scene
        settings = scene.ocb_settings
        obj = context.active_object

        if not bpy.data.filepath:

            self.report({'ERROR'}, 'Save blend file first')
            return {'CANCELLED'}

        resolution = int(settings.resolution)

        bake_types = []

        if settings.bake_diffuse:
            bake_types.append('DIFFUSE')

        if settings.bake_normal:
            bake_types.append('NORMAL')

        if settings.bake_roughness:
            bake_types.append('ROUGHNESS')

        target_materials = []

        for item in settings.materials:

            if not item.bake:
                continue

            mat = bpy.data.materials.get(item.name)

            if mat and mat.use_nodes:
                target_materials.append(mat)

        if not target_materials:

            self.report({'ERROR'}, 'No materials selected')
            return {'CANCELLED'}

        prev_engine = scene.render.engine
        scene.render.engine = 'CYCLES'

        _prepare_all_bake_nodes(obj)

        context.view_layer.update()

        baked_results = {}
        baked_count = 0

        try:

            for bake_type in bake_types:

                cfg = BAKE_TYPE_CONFIG[bake_type]

                out_dir = os.path.join(
                    _textures_root(),
                    cfg['folder']
                )

                os.makedirs(out_dir, exist_ok=True)

                for mat in target_materials:

                    try:

                        filepath = self._bake_one(
                            context,
                            obj,
                            mat,
                            bake_type,
                            resolution,
                            out_dir
                        )

                        baked_count += 1

                        if mat.name not in baked_results:
                            baked_results[mat.name] = {}

                        baked_results[mat.name][bake_type] = filepath

                        print(
                            f"[OCB OPTIMIZED] "
                            f"{mat.name} {bake_type} baked"
                        )

                    except RuntimeError as e:

                        self.report(
                            {'WARNING'},
                            f"Bake failed: {mat.name} ({e})"
                        )

            if settings.auto_setup:

                for mat_name, paths in baked_results.items():

                    mat = bpy.data.materials.get(mat_name)

                    if mat is None:
                        continue

                    try:
                        _setup_material_tree(mat, paths)

                    except Exception as e:
                        self.report(
                            {'WARNING'},
                            f'Auto setup failed for {mat.name}: {e}'
                        )

        finally:
            scene.render.engine = prev_engine

        self.report({'INFO'}, f'Baked {baked_count} textures')

        return {'FINISHED'}


# -----------------------------------------------------------------------------
# PANEL
# -----------------------------------------------------------------------------

class OCB_PT_panel(Panel):

    bl_label = 'One-Click Baker'
    bl_idname = 'OCB_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Bake'

    def draw(self, context):

        layout = self.layout
        settings = context.scene.ocb_settings

        layout.operator('ocb.refresh_materials')

        layout.template_list(
            'OCB_UL_materials',
            '',
            settings,
            'materials',
            settings,
            'materials_index',
            rows=8,
        )

        if settings.materials:

            active_index = min(
                settings.materials_index,
                len(settings.materials) - 1
            )

            active_item = settings.materials[active_index]

            box = layout.box()
            box.label(
                text=f"Active Material: {active_item.name}",
                icon='MATERIAL'
            )

        layout.prop(settings, 'bake_diffuse')
        layout.prop(settings, 'bake_normal')
        layout.prop(settings, 'bake_roughness')
        layout.prop(settings, 'resolution')
        layout.prop(settings, 'auto_setup')

        row = layout.row()
        row.scale_y = 2
        row.operator('ocb.bake', icon='RENDER_STILL')


# -----------------------------------------------------------------------------
# REGISTRATION
# -----------------------------------------------------------------------------

classes = (
    OCB_MaterialItem,
    OCB_Settings,
    OCB_UL_materials,
    OCB_OT_refresh_materials,
    OCB_OT_bake,
    OCB_PT_panel,
)



def register():

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.ocb_settings = PointerProperty(
        type=OCB_Settings
    )



def unregister():

    del bpy.types.Scene.ocb_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
