"""Per-object leaf and bark materials built from the images picked in the panel."""

import bpy

LEAF_FALLBACK = (0.09, 0.28, 0.05, 1.0)
BARK_FALLBACK = (0.18, 0.11, 0.06, 1.0)


def _non_color(img):
    if img is None:
        return
    try:
        img.colorspace_settings.name = "Non-Color"
    except TypeError:
        img.colorspace_settings.is_data = True


def _fresh_tree(mat):
    nt = mat.node_tree
    nt.nodes.clear()
    return nt, nt.nodes, nt.links


def _image_node(nodes, img, x, y, label):
    n = nodes.new("ShaderNodeTexImage")
    n.image = img
    n.label = label
    n.location = (x, y)
    return n


def _material(settings, attr, name):
    """The object's own material for this slot, created on first use."""
    mat = getattr(settings, attr)
    if mat is None:
        mat = bpy.data.materials.new(name)
        setattr(settings, attr, mat)
    return mat


def build_leaf(obj):
    s = obj.ivygen
    mat = _material(s, "leaf_material", "IvyGen Leaf · %s" % obj.name)
    mat.use_backface_culling = False
    nt, nodes, links = _fresh_tree(mat)

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 200)
    bsdf.inputs["Roughness"].default_value = 0.55
    trans = nodes.new("ShaderNodeBsdfTranslucent")
    trans.location = (200, -200)
    mix = nodes.new("ShaderNodeMixShader")
    mix.label = "translucency"
    mix.location = (480, 60)
    mix.inputs[0].default_value = s.leaf_translucency
    clear = nodes.new("ShaderNodeBsdfTransparent")
    clear.location = (480, -160)
    cutout = nodes.new("ShaderNodeMixShader")
    cutout.label = "alpha cut-out"
    cutout.location = (700, 0)
    cutout.inputs[0].default_value = 1.0

    links.new(bsdf.outputs["BSDF"], mix.inputs[1])
    links.new(trans.outputs["BSDF"], mix.inputs[2])
    # factor 0 = see-through, 1 = leaf, so alpha drives it directly
    links.new(clear.outputs["BSDF"], cutout.inputs[1])
    links.new(mix.outputs["Shader"], cutout.inputs[2])
    links.new(cutout.outputs["Shader"], out.inputs["Surface"])

    coord = nodes.new("ShaderNodeTexCoord")
    coord.location = (-700, 0)

    if s.leaf_color is not None:
        col = _image_node(nodes, s.leaf_color, -400, 200, "Leaf Color")
        links.new(coord.outputs["UV"], col.inputs["Vector"])
        links.new(col.outputs["Color"], bsdf.inputs["Base Color"])
        links.new(col.outputs["Color"], trans.inputs["Color"])
        links.new(col.outputs["Alpha"], cutout.inputs[0])
    else:
        bsdf.inputs["Base Color"].default_value = LEAF_FALLBACK
        trans.inputs["Color"].default_value = LEAF_FALLBACK

    if s.leaf_opacity is not None:
        _non_color(s.leaf_opacity)
        op = _image_node(nodes, s.leaf_opacity, -400, -100, "Leaf Opacity")
        links.new(coord.outputs["UV"], op.inputs["Vector"])
        links.new(op.outputs["Color"], cutout.inputs[0])

    if s.leaf_normal is not None:
        _non_color(s.leaf_normal)
        nm = _image_node(nodes, s.leaf_normal, -400, -400, "Leaf Normal")
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (-100, -400)
        links.new(coord.outputs["UV"], nm.inputs["Vector"])
        links.new(nm.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        links.new(nmap.outputs["Normal"], trans.inputs["Normal"])
    return mat


def build_bark(obj, uv_name):
    s = obj.ivygen
    mat = _material(s, "bark_material", "IvyGen Bark · %s" % obj.name)
    nt, nodes, links = _fresh_tree(mat)

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (500, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 0)
    bsdf.inputs["Roughness"].default_value = 0.8
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # the joined mesh can carry the surface's own UV maps too, so ask for
    # the tube's map by name
    uv = nodes.new("ShaderNodeUVMap")
    uv.uv_map = uv_name
    uv.location = (-700, 0)

    if s.bark_color is not None:
        col = _image_node(nodes, s.bark_color, -400, 200, "Bark Color")
        links.new(uv.outputs["UV"], col.inputs["Vector"])
        links.new(col.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = BARK_FALLBACK

    if s.bark_normal is not None:
        _non_color(s.bark_normal)
        nm = _image_node(nodes, s.bark_normal, -400, -200, "Bark Normal")
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.uv_map = uv_name
        nmap.location = (-100, -200)
        links.new(uv.outputs["UV"], nm.inputs["Vector"])
        links.new(nm.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return mat
