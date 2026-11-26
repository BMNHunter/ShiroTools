"""
IvyGen — procedural ivy that grows along the edges of a mesh.
Written against the Blender 5.2 Python / Geometry Nodes API.

Run it from Blender's Text Editor (Alt+P) or:
    blender myfile.blend --python ivygen_nodes.py

It builds (or refreshes in place) a node group called "IvyGen" and, if
ATTACH_TO_ACTIVE is on, drops a Geometry Nodes modifier using it on the
active object.

Pipeline:
    edge selection -> strands -> trim -> resample -> wander -> cling to surface
                   -> tube mesh (+UVs)  and  leaf instances -> join

Author's note on polycount: everything is trimmed *before* it is resampled,
the tube profile is a user-set n-gon (3 is legal), leaves stay instances
unless you ask for them realized, and caps are never filled.
"""

import math

import bpy

GROUP_NAME = "IvyGen"
ATTACH_TO_ACTIVE = True   # add / refresh the modifier on the active object
UV_NAME = "UVMap"         # name of the UV attribute written on the bark tube

TAU = math.pi * 2.0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def find_socket(node, key, output=False):
    """Socket by identifier first, then by UI name (several nodes reuse names)."""
    coll = node.outputs if output else node.inputs
    if isinstance(key, int):
        return coll[key]
    for s in coll:
        if s.identifier == key:
            return s
    for s in coll:
        if s.name == key:
            return s
    raise KeyError("%s has no socket %r (%s)"
                   % (node.bl_idname, key, [(s.name, s.identifier) for s in coll]))


class Builder:
    """Thin wrapper so the graph below reads like a graph."""

    def __init__(self, tree):
        self.tree = tree
        self.frames = {}

    def frame(self, key, label):
        f = self.tree.nodes.new("NodeFrame")
        f.label = label
        f.location = (0.0, 0.0)
        f.use_custom_color = False
        self.frames[key] = f
        return f

    def node(self, idname, x, y, frame=None, label=None, props=None, inputs=None,
             width=None):
        n = self.tree.nodes.new(idname)
        # enum / bool properties must be set before sockets are touched:
        # several nodes rebuild their socket list when they change.
        for k, v in (props or {}).items():
            setattr(n, k, v)
        if frame is not None:
            n.parent = self.frames[frame]
        n.location = (x, y)
        if label:
            n.label = label
        if width:
            n.width = width
        for k, v in (inputs or {}).items():
            find_socket(n, k).default_value = v
        return n

    def link(self, out_node, out_key, in_node, in_key):
        self.tree.links.new(find_socket(out_node, out_key, output=True),
                            find_socket(in_node, in_key))

    # a few shorthands for the maths nodes, which make up most of the tree
    def math(self, op, x, y, frame=None, a=None, b=None, c=None, label=None,
             clamp=False):
        n = self.node("ShaderNodeMath", x, y, frame, label,
                      props={"operation": op, "use_clamp": clamp})
        for idx, val in ((0, a), (1, b), (2, c)):
            if val is not None:
                n.inputs[idx].default_value = val
        return n

    @staticmethod
    def capture(node, kind, name):
        """Add a Capture Attribute item and return its socket name as a plain str.

        The item struct itself goes stale as soon as another item is added,
        so nothing but the name is kept.
        """
        node.capture_items.new(kind, name)
        return str(node.capture_items[-1].name)

    def vmath(self, op, x, y, frame=None, label=None):
        return self.node("ShaderNodeVectorMath", x, y, frame, label,
                         props={"operation": op})


# ---------------------------------------------------------------------------
# interface
# ---------------------------------------------------------------------------

def build_interface(ng):
    """Create the group's sockets, grouped into panels. Returns a name->socket map."""
    iface = ng.interface
    socks = {}
    panels = {}

    def panel(name, closed=False):
        panels[name] = iface.new_panel(name, default_closed=closed)
        return panels[name]

    def add(name, stype, parent=None, default=None, mn=None, mx=None, desc="",
            single=True, subtype=None):
        s = iface.new_socket(name, in_out="INPUT", socket_type=stype,
                             parent=panels.get(parent))
        if desc:
            s.description = desc
        if subtype is not None:
            try:
                s.subtype = subtype
            except (TypeError, AttributeError):
                pass
        if mn is not None:
            s.min_value = mn
        if mx is not None:
            s.max_value = mx
        if default is not None:
            s.default_value = default
        if single:
            # plain value in the modifier, no attribute plug
            try:
                s.structure_type = "SINGLE"
            except (TypeError, AttributeError):
                pass
        socks[name] = s
        return s

    out = iface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    socks["#out"] = out
    add("Geometry", "NodeSocketGeometry", single=False,
        desc="Mesh to grow ivy on")

    # ---- edges ----------------------------------------------------------
    panel("Edges")
    add("Auto Top Edges", "NodeSocketBool", "Edges", default=True,
        desc="Use the border edges of upward facing faces (the top rim of a cube). "
             "Turn off to drive the growth from Edge Selection instead")
    add("Up Threshold", "NodeSocketFloat", "Edges", default=0.5, mn=-1.0, mx=1.0,
        desc="A face counts as upward facing when its normal Z is above this")
    sel = add("Edge Selection", "NodeSocketBool", "Edges", default=True, single=False,
              desc="Which edges to grow along when Auto Top Edges is off. "
                   "Plug a boolean edge attribute in the modifier")
    try:
        sel.structure_type = "DYNAMIC"
        sel.attribute_domain = "EDGE"
    except (TypeError, AttributeError):
        pass

    # ---- growth ---------------------------------------------------------
    panel("Growth")
    add("Strands", "NodeSocketInt", "Growth", default=4, mn=1, mx=64,
        desc="Vines per selected edge loop")
    add("Length", "NodeSocketFloat", "Growth", default=0.55, mn=0.0, mx=1.0,
        desc="How much of each edge loop one vine covers, as a fraction")
    add("Length Random", "NodeSocketFloat", "Growth", default=0.45, mn=0.0, mx=1.0,
        desc="Per strand variation of Length")
    add("Seed", "NodeSocketInt", "Growth", default=0, mn=-10000, mx=10000)

    # ---- shape ----------------------------------------------------------
    panel("Shape")
    add("Spread", "NodeSocketFloat", "Shape", default=0.25, mn=0.0, mx=100.0,
        desc="How far the vines wander sideways off the edge, in meters")
    add("Spread Scale", "NodeSocketFloat", "Shape", default=2.5, mn=0.0, mx=100.0,
        desc="Noise frequency of the wander. Low = long lazy curves, high = wiggly")
    add("Spread Start", "NodeSocketFloat", "Shape", default=0.15, mn=0.0, mx=1.0,
        desc="Fraction of the strand that stays glued to the edge before it starts "
             "wandering")
    add("Cling", "NodeSocketFloat", "Shape", default=0.9, mn=0.0, mx=1.0,
        desc="Pull the wandering vine back onto the surface. 1 = hugs the mesh, "
             "0 = free floating")
    add("Surface Offset", "NodeSocketFloat", "Shape", default=0.006, mn=-10.0, mx=10.0,
        desc="Lift off the surface, along the surface normal")
    add("Droop", "NodeSocketFloat", "Shape", default=0.05, mn=0.0, mx=100.0,
        desc="Gravity sag toward the tip of each strand")

    # ---- vine mesh ------------------------------------------------------
    panel("Vine")
    add("Build Vine Mesh", "NodeSocketBool", "Vine", default=True,
        desc="Off outputs the bare curves instead of a tube (cheap LOD, or render "
             "them as curves)")
    add("Segment Length", "NodeSocketFloat", "Vine", default=0.06, mn=0.001, mx=100.0,
        desc="Distance between vine control points. The main polycount dial")
    add("Profile Resolution", "NodeSocketInt", "Vine", default=4, mn=3, mx=32,
        desc="Sides of the tube. 3 or 4 is plenty for background ivy")
    add("Vine Radius", "NodeSocketFloat", "Vine", default=0.010, mn=0.0, mx=10.0)
    add("Tip Taper", "NodeSocketFloat", "Vine", default=0.25, mn=0.0, mx=4.0,
        desc="Radius multiplier at the tip of a strand")
    add("Bark UV Scale", "NodeSocketFloat", "Vine", default=1.0, mn=0.0, mx=1000.0,
        desc="UV tiles per meter along the vine. U always runs 0-1 around the tube")
    add("Bark Material", "NodeSocketMaterial", "Vine")

    # ---- leaves ---------------------------------------------------------
    panel("Leaves")
    add("Build Leaves", "NodeSocketBool", "Leaves", default=True)
    add("Leaf Object", "NodeSocketObject", "Leaves", single=False,
        desc="Your leaf mesh object. Leave it empty and a UV'd card is generated "
             "instead. Model it flat in XY, growing along +Y, stem at the origin")
    add("Leaf Collection", "NodeSocketCollection", "Leaves", single=False,
        desc="Several leaf variants. One is picked at random per leaf. Takes "
             "priority over Leaf Object")
    add("Leaf Density", "NodeSocketFloat", "Leaves", default=14.0, mn=0.0, mx=1000.0,
        desc="Leaves per meter of vine")
    add("Leaf Size", "NodeSocketFloat", "Leaves", default=0.07, mn=0.0, mx=100.0)
    add("Leaf Size Random", "NodeSocketFloat", "Leaves", default=0.4, mn=0.0, mx=1.0)
    add("Leaf Tip Scale", "NodeSocketFloat", "Leaves", default=0.45, mn=0.0, mx=4.0,
        desc="Leaf size multiplier at the tip of a strand. Young growth is smaller")
    add("Leaf Rotation Random", "NodeSocketFloat", "Leaves", default=0.55, mn=0.0, mx=1.0)
    add("Leaf Material", "NodeSocketMaterial", "Leaves")
    add("Override Leaf Material", "NodeSocketBool", "Leaves", default=True,
        desc="Off keeps the materials already on your leaf mesh")
    add("Realize Leaves", "NodeSocketBool", "Leaves", default=False,
        desc="Instances are far cheaper. Realize only if you need to edit or "
             "export them as real geometry")

    # ---- output ---------------------------------------------------------
    panel("Output")
    add("Keep Surface", "NodeSocketBool", "Output", default=True,
        desc="Pass the original mesh through")

    return socks


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

def _interface_inputs(ng):
    for item in ng.interface.items_tree:
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT":
            yield item


def _modifiers_using(ng):
    for ob in bpy.data.objects:
        for mod in ob.modifiers:
            if mod.type == "NODES" and mod.node_group is ng:
                yield mod


def snapshot_settings(ng):
    """Remember what every modifier has dialled in, keyed by socket name.

    Rebuilding the interface hands out fresh socket identifiers, which orphans
    the values already stored on the modifiers, so they are copied out here and
    written back afterwards.
    """
    saved = []
    for mod in _modifiers_using(ng):
        values = {}
        inputs = mod.properties.inputs
        for item in _interface_inputs(ng):
            sock = getattr(inputs, item.identifier, None)
            if sock is None:
                continue
            entry = {}
            for field in ("value", "type", "attribute_name"):
                if hasattr(sock, field):
                    try:
                        entry[field] = getattr(sock, field)
                    except (AttributeError, TypeError):
                        pass
            if entry:
                values[item.name] = entry
        saved.append((mod, values))
    return saved


def sync_modifiers(ng, saved=()):
    """Write values back onto every modifier, falling back to group defaults."""
    by_mod = dict(saved)
    for mod in _modifiers_using(ng):
        values = by_mod.get(mod, {})
        inputs = mod.properties.inputs
        for item in _interface_inputs(ng):
            sock = getattr(inputs, item.identifier, None)
            if sock is None or not hasattr(sock, "value"):
                continue
            entry = values.get(item.name)
            if entry is None:
                entry = {"value": getattr(item, "default_value", None)}
            for field in ("type", "value", "attribute_name"):
                if field not in entry or entry[field] is None:
                    continue
                try:
                    setattr(sock, field, entry[field])
                except (AttributeError, TypeError, ValueError):
                    pass
        mod.node_group = ng


def build_group():
    ng = bpy.data.node_groups.get(GROUP_NAME)
    saved = ()
    if ng is None:
        ng = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    else:
        saved = snapshot_settings(ng)
        ng.nodes.clear()
        ng.interface.clear()

    build_interface(ng)
    b = Builder(ng)
    for key, label in (
        ("edges", "1 · Edge selection"),
        ("strands", "2 · Strands"),
        ("wander", "3 · Wander + cling to surface"),
        ("vine", "4 · Vine tube + UVs"),
        ("leaves", "5 · Leaves"),
        ("out", "6 · Output"),
    ):
        b.frame(key, label)

    gi = b.node("NodeGroupInput", -1400, 0, "edges", width=220)
    gi2 = b.node("NodeGroupInput", -1400, -1500, "leaves", width=220)
    go = b.node("NodeGroupOutput", 3400, 0, "out")

    # =====================================================================
    # 1 - edge selection
    # =====================================================================
    normal = b.node("GeometryNodeInputNormal", -1150, 320, "edges")
    nz = b.node("ShaderNodeSeparateXYZ", -980, 320, "edges")
    b.link(normal, "Normal", nz, "Vector")

    up_cmp = b.node("FunctionNodeCompare", -800, 320, "edges", "face points up",
                    props={"data_type": "FLOAT", "operation": "GREATER_THAN"})
    b.link(nz, "Z", up_cmp, "A")
    b.link(gi, "Up Threshold", up_cmp, "B")

    up_faces = b.node("GeometryNodeSeparateGeometry", -600, 260, "edges",
                      "upward facing faces", props={"domain": "FACE"})
    b.link(gi, "Geometry", up_faces, "Geometry")
    b.link(up_cmp, "Result", up_faces, "Selection")

    neigh = b.node("GeometryNodeInputMeshEdgeNeighbors", -600, 60, "edges")
    border = b.node("FunctionNodeCompare", -420, 60, "edges", "border edge",
                    props={"data_type": "INT", "operation": "EQUAL"},
                    inputs={"B": 1})
    b.link(neigh, "Face Count", border, "A")

    curve_auto = b.node("GeometryNodeMeshToCurve", -230, 240, "edges",
                        props={"mode": "EDGES"})
    b.link(up_faces, "Selection", curve_auto, "Mesh")
    b.link(border, "Result", curve_auto, "Selection")

    curve_manual = b.node("GeometryNodeMeshToCurve", -230, 60, "edges",
                          props={"mode": "EDGES"})
    b.link(gi, "Geometry", curve_manual, "Mesh")
    b.link(gi, "Edge Selection", curve_manual, "Selection")

    edge_curve = b.node("GeometryNodeSwitch", -40, 200, "edges", "edge source",
                        props={"input_type": "GEOMETRY"})
    b.link(gi, "Auto Top Edges", edge_curve, "Switch")
    b.link(curve_manual, "Curve", edge_curve, "False")
    b.link(curve_auto, "Curve", edge_curve, "True")

    # =====================================================================
    # 2 - strands: duplicate each edge loop, then trim each copy
    # =====================================================================
    dup = b.node("GeometryNodeDuplicateElements", 180, 200, "strands",
                 "one copy per strand", props={"domain": "SPLINE"})
    b.link(edge_curve, "Output", dup, "Geometry")
    b.link(gi, "Strands", dup, "Amount")

    idx = b.node("GeometryNodeInputIndex", 180, -60, "strands")
    cap_id = b.node("GeometryNodeCaptureAttribute", 380, 200, "strands",
                    "freeze strand id", props={"domain": "CURVE"})
    sid = b.capture(cap_id, "INT", "Strand")
    b.link(dup, "Geometry", cap_id, "Geometry")
    b.link(idx, "Index", cap_id, sid)

    def strand_random(x, y, seed_offset, lo=0.0, hi=1.0, label=None,
                      frame="strands"):
        """One random value per strand, stable under everything but Seed."""
        seed = b.math("ADD", x - 190, y - 200, frame, b=float(seed_offset))
        b.link(gi, "Seed", seed, 0)
        rnd = b.node("FunctionNodeRandomValue", x, y, frame, label,
                     props={"data_type": "FLOAT"},
                     inputs={"Min": lo, "Max": hi})
        b.link(cap_id, sid, rnd, "ID")
        b.link(seed, "Value", rnd, "Seed")
        return rnd

    r_len = strand_random(620, 320, 0, label="length jitter")
    r_start = strand_random(620, 100, 1, label="start along edge")
    r_noise = strand_random(620, -140, 2, 0.0, 97.0, label="noise offset")

    # length = Length * (1 - Length Random * r_len)
    lr = b.math("MULTIPLY", 820, 380, "strands")
    b.link(gi, "Length Random", lr, 0)
    b.link(r_len, "Value", lr, 1)
    one_minus = b.math("SUBTRACT", 990, 380, "strands", a=1.0)
    b.link(lr, "Value", one_minus, 1)
    seg_len = b.math("MULTIPLY", 1160, 380, "strands", label="strand length", clamp=True)
    b.link(gi, "Length", seg_len, 0)
    b.link(one_minus, "Value", seg_len, 1)

    room = b.math("SUBTRACT", 990, 160, "strands", a=1.0)
    b.link(seg_len, "Value", room, 1)
    t_start = b.math("MULTIPLY", 1160, 160, "strands", label="trim start")
    b.link(r_start, "Value", t_start, 0)
    b.link(room, "Value", t_start, 1)
    t_end = b.math("ADD", 1330, 260, "strands", label="trim end")
    b.link(t_start, "Value", t_end, 0)
    b.link(seg_len, "Value", t_end, 1)

    trim = b.node("GeometryNodeTrimCurve", 1520, 200, "strands",
                  props={"mode": "FACTOR"})
    b.link(cap_id, "Geometry", trim, "Curve")
    b.link(t_start, "Value", trim, "Start")
    b.link(t_end, "Value", trim, "End")

    resample = b.node("GeometryNodeResampleCurve", 1700, 200, "strands",
                      "polycount dial")
    resample.inputs["Mode"].default_value = "Length"
    b.link(trim, "Curve", resample, "Curve")
    b.link(gi, "Segment Length", resample, "Length")

    # =====================================================================
    # 3 - wander, then cling back onto the surface
    # =====================================================================
    param = b.node("GeometryNodeSplineParameter", 1700, -320, "wander")
    pos = b.node("GeometryNodeInputPosition", 1700, -480, "wander")
    tangent = b.node("GeometryNodeInputTangent", 1700, -600, "wander")

    surf_n = b.node("GeometryNodeSampleNearestSurface", 1900, -700, "wander",
                    "surface normal", props={"data_type": "FLOAT_VECTOR"})
    surf_normal_src = b.node("GeometryNodeInputNormal", 1700, -760, "wander")
    b.link(gi, "Geometry", surf_n, "Mesh")
    b.link(surf_normal_src, "Normal", surf_n, "Value")
    b.link(pos, "Position", surf_n, "Sample Position")

    side = b.vmath("CROSS_PRODUCT", 2100, -620, "wander", "sideways")
    b.link(tangent, "Tangent", side, 0)
    b.link(surf_n, "Value", side, 1)
    side_n = b.vmath("NORMALIZE", 2260, -620, "wander")
    b.link(side, "Vector", side_n, 0)

    noise = b.node("ShaderNodeTexNoise", 1900, -330, "wander",
                   props={"noise_dimensions": "4D"},
                   inputs={"Detail": 2.0, "Roughness": 0.5})
    b.link(pos, "Position", noise, "Vector")
    b.link(r_noise, "Value", noise, "W")
    b.link(gi, "Spread Scale", noise, "Scale")
    nsep = b.node("ShaderNodeSeparateXYZ", 2100, -290, "wander")
    b.link(noise, "Color", nsep, "Vector")

    def signed(out_node, out_key, x, y, label=None):
        """Noise sits in a narrow band around 0.5, so stretch it to -1..1."""
        m = b.node("ShaderNodeMapRange", x, y, "wander", label,
                   props={"clamp": True},
                   inputs={"From Min": 0.25, "From Max": 0.75,
                           "To Min": -1.0, "To Max": 1.0})
        b.link(out_node, out_key, m, "Value")
        return m

    n_side = signed(nsep, "X", 2260, -180, "-1..1")
    n_norm = signed(nsep, "Y", 2260, -400)

    # ramp: nothing moves until Spread Start, then ease in
    ramp = b.node("ShaderNodeMapRange", 1900, -140, "wander", "hold the base",
                  props={"interpolation_type": "SMOOTHSTEP"},
                  inputs={"To Min": 0.0, "To Max": 1.0})
    b.link(param, "Factor", ramp, "Value")
    b.link(gi, "Spread Start", ramp, "From Min")
    ramp.inputs["From Max"].default_value = 1.0

    amp0 = b.math("MULTIPLY", 2600, -140, "wander", label="spread * ramp")
    b.link(gi, "Spread", amp0, 0)
    b.link(ramp, "Result", amp0, 1)
    # not every strand wanders as far as the next one
    r_amp = strand_random(2260, -560, 8, 0.35, 1.0, label="strand amplitude",
                          frame="wander")
    amp = b.math("MULTIPLY", 2770, -60, "wander", label="per strand")
    b.link(amp0, "Value", amp, 0)
    b.link(r_amp, "Value", amp, 1)

    off_side = b.math("MULTIPLY", 2940, -180, "wander")
    b.link(n_side, "Result", off_side, 0)
    b.link(amp, "Value", off_side, 1)
    off_norm_a = b.math("MULTIPLY", 2940, -400, "wander", b=0.35)
    b.link(n_norm, "Result", off_norm_a, 0)
    off_norm = b.math("MULTIPLY", 3110, -400, "wander")
    b.link(off_norm_a, "Value", off_norm, 0)
    b.link(amp, "Value", off_norm, 1)

    v_side = b.vmath("SCALE", 3290, -180, "wander")
    b.link(side_n, "Vector", v_side, 0)
    b.link(off_side, "Value", v_side, "Scale")
    v_norm = b.vmath("SCALE", 3290, -400, "wander")
    b.link(surf_n, "Value", v_norm, 0)
    b.link(off_norm, "Value", v_norm, "Scale")

    # droop grows with t^2 so the base stays put
    t2 = b.math("MULTIPLY", 2600, -520, "wander", label="t squared")
    b.link(param, "Factor", t2, 0)
    b.link(param, "Factor", t2, 1)
    droop = b.math("MULTIPLY", 2770, -520, "wander")
    b.link(t2, "Value", droop, 0)
    b.link(gi, "Droop", droop, 1)
    droop_neg = b.math("MULTIPLY", 2940, -520, "wander", b=-1.0)
    b.link(droop, "Value", droop_neg, 0)
    v_droop = b.node("ShaderNodeCombineXYZ", 3110, -520, "wander")
    b.link(droop_neg, "Value", v_droop, "Z")

    sum_a = b.vmath("ADD", 3460, -240, "wander")
    b.link(v_side, "Vector", sum_a, 0)
    b.link(v_norm, "Vector", sum_a, 1)
    sum_b = b.vmath("ADD", 3620, -300, "wander", "total offset")
    b.link(sum_a, "Vector", sum_b, 0)
    b.link(v_droop, "Vector", sum_b, 1)

    wander = b.node("GeometryNodeSetPosition", 3800, 200, "wander", "wander")
    b.link(resample, "Curve", wander, "Geometry")
    b.link(sum_b, "Vector", wander, "Offset")

    # --- cling ----------------------------------------------------------
    # Projecting straight onto the nearest surface point collapses everything
    # back onto a convex edge: the edge *is* the nearest point for anything
    # hovering over it. So the probe point is pushed a little way into the
    # mesh first, which breaks the tie and lets a strand settle onto one face
    # or the other and wrap over the rim.
    pos2 = b.node("GeometryNodeInputPosition", 3800, -60, "wander")
    surf_n0 = b.node("GeometryNodeSampleNearestSurface", 3830, -560, "wander",
                     "normal at the point", props={"data_type": "FLOAT_VECTOR"})
    normal_src0 = b.node("GeometryNodeInputNormal", 3640, -620, "wander")
    b.link(gi, "Geometry", surf_n0, "Mesh")
    b.link(normal_src0, "Normal", surf_n0, "Value")
    b.link(pos2, "Position", surf_n0, "Sample Position")

    bias_amt = b.math("MULTIPLY", 3830, -720, "wander", b=-0.5, label="sink the probe")
    b.link(gi, "Spread", bias_amt, 0)
    bias_vec = b.vmath("SCALE", 4020, -640, "wander")
    b.link(surf_n0, "Value", bias_vec, 0)
    b.link(bias_amt, "Value", bias_vec, "Scale")
    probe = b.vmath("ADD", 4190, -560, "wander", "probe point")
    b.link(pos2, "Position", probe, 0)
    b.link(bias_vec, "Vector", probe, 1)

    prox = b.node("GeometryNodeProximity", 4360, -240, "wander",
                  "nearest surface point", props={"target_element": "FACES"})
    b.link(gi, "Geometry", prox, "Target")
    b.link(probe, "Vector", prox, "Source Position")

    surf_n2 = b.node("GeometryNodeSampleNearestSurface", 4360, -420, "wander",
                     "normal there", props={"data_type": "FLOAT_VECTOR"})
    normal_src2 = b.node("GeometryNodeInputNormal", 4190, -760, "wander")
    b.link(gi, "Geometry", surf_n2, "Mesh")
    b.link(normal_src2, "Normal", surf_n2, "Value")
    b.link(probe, "Vector", surf_n2, "Sample Position")

    lift = b.vmath("SCALE", 4540, -420, "wander")
    b.link(surf_n2, "Value", lift, 0)
    b.link(gi, "Surface Offset", lift, "Scale")
    on_surf = b.vmath("ADD", 4720, -280, "wander", "point on surface")
    b.link(prox, "Position", on_surf, 0)
    b.link(lift, "Vector", on_surf, 1)

    mix_pos = b.node("ShaderNodeMix", 4900, -60, "wander", "cling",
                     props={"data_type": "VECTOR"})
    b.link(gi, "Cling", mix_pos, "Factor_Float")
    b.link(pos2, "Position", mix_pos, "A_Vector")
    b.link(on_surf, "Vector", mix_pos, "B_Vector")

    cling = b.node("GeometryNodeSetPosition", 5090, 200, "wander", "cling")
    b.link(wander, "Geometry", cling, "Geometry")
    b.link(mix_pos, "Result_Vector", cling, "Position")

    # --- radius taper ----------------------------------------------------
    taper = b.node("ShaderNodeMapRange", 5090, -60, "wander", "taper",
                   inputs={"From Min": 0.0, "From Max": 1.0, "To Min": 1.0})
    b.link(param, "Factor", taper, "Value")
    b.link(gi, "Tip Taper", taper, "To Max")
    rad = b.math("MULTIPLY", 5280, -60, "wander", label="radius")
    b.link(gi, "Vine Radius", rad, 0)
    b.link(taper, "Result", rad, 1)

    set_rad = b.node("GeometryNodeSetCurveRadius", 5280, 200, "wander")
    b.link(cling, "Geometry", set_rad, "Curve")
    b.link(rad, "Value", set_rad, "Radius")

    # freeze what the leaves need, so it survives Curve to Points
    # the leaves want the normal at the vine itself, not at the sunken probe
    # point the clinging step uses, or leaves near a rim end up standing on
    # the neighbouring face
    leaf_pos = b.node("GeometryNodeInputPosition", 5280, -320, "wander")
    leaf_n = b.node("GeometryNodeSampleNearestSurface", 5470, -320, "wander",
                    "normal for the leaves", props={"data_type": "FLOAT_VECTOR"})
    leaf_n_src = b.node("GeometryNodeInputNormal", 5280, -440, "wander")
    b.link(gi, "Geometry", leaf_n, "Mesh")
    b.link(leaf_n_src, "Normal", leaf_n, "Value")
    b.link(leaf_pos, "Position", leaf_n, "Sample Position")

    cap_pt = b.node("GeometryNodeCaptureAttribute", 5660, 200, "wander",
                    "freeze for leaves", props={"domain": "POINT"})
    it_t = b.capture(cap_pt, "FLOAT", "T")
    it_n = b.capture(cap_pt, "VECTOR", "SurfNormal")
    b.link(set_rad, "Curve", cap_pt, "Geometry")
    b.link(param, "Factor", cap_pt, it_t)
    b.link(leaf_n, "Value", cap_pt, it_n)

    vines = cap_pt   # curves, finished

    # =====================================================================
    # 4 - tube mesh + UVs
    # =====================================================================
    # V: distance along the strand, U: 0..1 around the tube
    len_along = b.math("MULTIPLY", 5150, 40, "vine", label="V")
    b.link(param, "Length", len_along, 0)
    b.link(gi, "Bark UV Scale", len_along, 1)
    cap_v = b.node("GeometryNodeCaptureAttribute", 5340, 200, "vine",
                   "V along vine", props={"domain": "POINT"})
    it_v = b.capture(cap_v, "FLOAT", "V")
    b.link(vines, "Geometry", cap_v, "Geometry")
    b.link(len_along, "Value", cap_v, it_v)

    # profile: an arc swept a full turn is an open ring, so the seam gets its
    # own vertex column and U does not wrap back on itself
    prof_res = b.math("ADD", 5150, -320, "vine", b=1.0, label="res + 1")
    b.link(gi, "Profile Resolution", prof_res, 0)
    profile = b.node("GeometryNodeCurveArc", 5340, -320, "vine", "tube profile",
                     props={"mode": "RADIUS"},
                     inputs={"Radius": 1.0, "Sweep Angle": TAU,
                             "Connect Center": False, "Invert Arc": False})
    b.link(prof_res, "Value", profile, "Resolution")

    p_idx = b.node("GeometryNodeInputIndex", 5340, -560, "vine")
    p_span = b.math("SUBTRACT", 5340, -660, "vine", b=1.0)
    b.link(prof_res, "Value", p_span, 0)
    p_u = b.math("DIVIDE", 5520, -600, "vine", label="U around tube")
    b.link(p_idx, "Index", p_u, 0)
    b.link(p_span, "Value", p_u, 1)
    cap_u = b.node("GeometryNodeCaptureAttribute", 5700, -320, "vine",
                   "U around tube", props={"domain": "POINT"})
    it_u = b.capture(cap_u, "FLOAT", "U")
    b.link(profile, "Curve", cap_u, "Geometry")
    b.link(p_u, "Value", cap_u, it_u)

    tube = b.node("GeometryNodeCurveToMesh", 5900, 200, "vine",
                  inputs={"Fill Caps": False})
    tube_radius = b.node("GeometryNodeInputRadius", 5700, 40, "vine")
    b.link(cap_v, "Geometry", tube, "Curve")
    b.link(cap_u, "Geometry", tube, "Profile Curve")
    # 5.x scales the profile through this socket, not through the radius
    # attribute, so hand it the radius explicitly
    b.link(tube_radius, "Radius", tube, "Scale")

    uv = b.node("ShaderNodeCombineXYZ", 5900, -60, "vine", "UV")
    b.link(cap_u, it_u, uv, "X")
    b.link(cap_v, it_v, uv, "Y")
    store_uv = b.node("GeometryNodeStoreNamedAttribute", 6090, 200, "vine",
                      props={"data_type": "FLOAT2", "domain": "CORNER"},
                      inputs={"Name": UV_NAME})
    b.link(tube, "Mesh", store_uv, "Geometry")
    b.link(uv, "Vector", store_uv, "Value")

    smooth = b.node("GeometryNodeSetShadeSmooth", 6280, 200, "vine",
                    props={"domain": "FACE"}, inputs={"Shade Smooth": True})
    b.link(store_uv, "Geometry", smooth, "Geometry")

    bark_mat = b.node("GeometryNodeSetMaterial", 6470, 200, "vine")
    b.link(smooth, "Geometry", bark_mat, "Geometry")
    b.link(gi, "Bark Material", bark_mat, "Material")

    vine_out = b.node("GeometryNodeSwitch", 6660, 200, "vine", "tube or bare curves",
                      props={"input_type": "GEOMETRY"})
    b.link(gi, "Build Vine Mesh", vine_out, "Switch")
    b.link(vines, "Geometry", vine_out, "False")
    b.link(bark_mat, "Geometry", vine_out, "True")

    # =====================================================================
    # 5 - leaves
    # =====================================================================
    spacing = b.math("DIVIDE", 5150, -1150, "leaves", a=1.0, label="1 / density")
    b.link(gi2, "Leaf Density", spacing, 1)
    lpts = b.node("GeometryNodeCurveToPoints", 5340, -1000, "leaves",
                  "leaf positions", props={"mode": "LENGTH"})
    b.link(vines, "Geometry", lpts, "Curve")
    b.link(spacing, "Value", lpts, "Length")

    # leaf frame: flat on the surface, growing along the vine
    rot = b.node("FunctionNodeAxesToRotation", 5560, -1120, "leaves", "lie flat",
                 props={"primary_axis": "Z", "secondary_axis": "Y"})
    b.link(cap_pt, it_n, rot, "Primary Axis")
    b.link(lpts, "Tangent", rot, "Secondary Axis")

    def leaf_random(x, y, seed_offset, lo, hi, label=None):
        seed = b.math("ADD", x - 190, y - 130, "leaves", b=float(seed_offset))
        b.link(gi2, "Seed", seed, 0)
        rnd = b.node("FunctionNodeRandomValue", x, y, "leaves", label,
                     props={"data_type": "FLOAT"}, inputs={"Min": lo, "Max": hi})
        b.link(seed, "Value", rnd, "Seed")
        return rnd

    yaw_r = leaf_random(5560, -1400, 3, -math.pi, math.pi, "yaw")
    tilt_r = leaf_random(5560, -1650, 4, -0.7, 0.7, "tilt")
    side_r = leaf_random(5560, -1900, 5, -1.0, 1.0, "sideways")
    size_r = leaf_random(5560, -2150, 6, 0.0, 1.0, "size")

    yaw = b.math("MULTIPLY", 5760, -1400, "leaves")
    b.link(yaw_r, "Value", yaw, 0)
    b.link(gi2, "Leaf Rotation Random", yaw, 1)
    tilt = b.math("MULTIPLY", 5760, -1650, "leaves")
    b.link(tilt_r, "Value", tilt, 0)
    b.link(gi2, "Leaf Rotation Random", tilt, 1)
    euler = b.node("ShaderNodeCombineXYZ", 5940, -1500, "leaves")
    b.link(tilt, "Value", euler, "X")
    b.link(yaw, "Value", euler, "Z")
    euler_rot = b.node("FunctionNodeEulerToRotation", 6120, -1500, "leaves")
    b.link(euler, "Vector", euler_rot, "Euler")
    rot2 = b.node("FunctionNodeRotateRotation", 6300, -1200, "leaves", "jitter",
                  props={"rotation_space": "LOCAL"})
    b.link(rot, "Rotation", rot2, "Rotation")
    b.link(euler_rot, "Rotation", rot2, "Rotate By")

    # nudge leaves off the vine axis so they do not all sit on the center line
    lside = b.vmath("CROSS_PRODUCT", 5760, -1900, "leaves")
    b.link(lpts, "Tangent", lside, 0)
    b.link(cap_pt, it_n, lside, 1)
    lside_n = b.vmath("NORMALIZE", 5940, -1900, "leaves")
    b.link(lside, "Vector", lside_n, 0)
    nudge_amt_a = b.math("MULTIPLY", 5940, -2050, "leaves", b=0.3)
    b.link(side_r, "Value", nudge_amt_a, 0)
    nudge_amt = b.math("MULTIPLY", 6120, -2050, "leaves")
    b.link(nudge_amt_a, "Value", nudge_amt, 0)
    b.link(gi2, "Leaf Size", nudge_amt, 1)
    nudge_side = b.vmath("SCALE", 6300, -1900, "leaves")
    b.link(lside_n, "Vector", nudge_side, 0)
    b.link(nudge_amt, "Value", nudge_side, "Scale")
    lift_leaf = b.vmath("SCALE", 6300, -2100, "leaves", "clear the bark")
    b.link(cap_pt, it_n, lift_leaf, 0)
    b.link(gi2, "Vine Radius", lift_leaf, "Scale")
    nudge = b.vmath("ADD", 6480, -1950, "leaves")
    b.link(nudge_side, "Vector", nudge, 0)
    b.link(lift_leaf, "Vector", nudge, 1)

    lpts_moved = b.node("GeometryNodeSetPosition", 6480, -1000, "leaves")
    b.link(lpts, "Points", lpts_moved, "Geometry")
    b.link(nudge, "Vector", lpts_moved, "Offset")

    # size: base size * random * taper toward the tip
    size_span = b.math("MULTIPLY", 5760, -2150, "leaves")
    b.link(size_r, "Value", size_span, 0)
    b.link(gi2, "Leaf Size Random", size_span, 1)
    size_fac = b.math("SUBTRACT", 5940, -2250, "leaves", a=1.0)
    b.link(size_span, "Value", size_fac, 1)
    leaf_taper = b.node("ShaderNodeMapRange", 5940, -2450, "leaves", "smaller at the tip",
                        inputs={"From Min": 0.0, "From Max": 1.0, "To Min": 1.0})
    b.link(cap_pt, it_t, leaf_taper, "Value")
    b.link(gi2, "Leaf Tip Scale", leaf_taper, "To Max")
    size_a = b.math("MULTIPLY", 6120, -2300, "leaves")
    b.link(gi2, "Leaf Size", size_a, 0)
    b.link(size_fac, "Value", size_a, 1)
    size = b.math("MULTIPLY", 6300, -2350, "leaves", label="leaf scale")
    b.link(size_a, "Value", size, 0)
    b.link(leaf_taper, "Result", size, 1)

    # leaf source: the plugged mesh, or a generated card when it is empty
    card = b.node("GeometryNodeMeshGrid", 5340, -2700, "leaves", "fallback leaf card",
                  inputs={"Size X": 1.0, "Size Y": 1.0, "Vertices X": 2, "Vertices Y": 2})
    card_uv = b.node("GeometryNodeStoreNamedAttribute", 5560, -2700, "leaves",
                     props={"data_type": "FLOAT2", "domain": "CORNER"},
                     inputs={"Name": UV_NAME})
    b.link(card, "Mesh", card_uv, "Geometry")
    b.link(card, "UV Map", card_uv, "Value")
    card_pivot = b.node("GeometryNodeTransform", 5760, -2700, "leaves",
                        "stem at the origin")
    card_pivot.inputs["Translation"].default_value = (0.0, 0.5, 0.0)
    b.link(card_uv, "Geometry", card_pivot, "Geometry")

    leaf_obj = b.node("GeometryNodeObjectInfo", 5340, -2950, "leaves",
                      props={"transform_space": "ORIGINAL"},
                      inputs={"As Instance": False})
    b.link(gi2, "Leaf Object", leaf_obj, "Object")
    dsize = b.node("GeometryNodeAttributeDomainSize", 5560, -3050, "leaves",
                   props={"component": "MESH"})
    b.link(leaf_obj, "Geometry", dsize, "Geometry")
    empty = b.node("FunctionNodeCompare", 5740, -3050, "leaves", "no leaf object",
                   props={"data_type": "INT", "operation": "LESS_THAN"},
                   inputs={"B": 1})
    b.link(dsize, "Point Count", empty, "A")
    leaf_src = b.node("GeometryNodeSwitch", 5940, -2800, "leaves", "object or card",
                      props={"input_type": "GEOMETRY"})
    b.link(empty, "Result", leaf_src, "Switch")
    b.link(leaf_obj, "Geometry", leaf_src, "False")
    b.link(card_pivot, "Geometry", leaf_src, "True")

    leaf_mat = b.node("GeometryNodeSetMaterial", 6120, -2800, "leaves")
    b.link(leaf_src, "Output", leaf_mat, "Geometry")
    b.link(gi2, "Leaf Material", leaf_mat, "Material")
    single_leaf = b.node("GeometryNodeSwitch", 6300, -2800, "leaves",
                         "material override", props={"input_type": "GEOMETRY"})
    b.link(gi2, "Override Leaf Material", single_leaf, "Switch")
    b.link(leaf_src, "Output", single_leaf, "False")
    b.link(leaf_mat, "Geometry", single_leaf, "True")

    # a collection of leaf variants wins over the single leaf, and each
    # instance picks one of its children at random
    coll = b.node("GeometryNodeCollectionInfo", 5340, -3300, "leaves",
                  "leaf variants", props={"transform_space": "ORIGINAL"},
                  inputs={"Separate Children": True, "Reset Children": True})
    b.link(gi2, "Leaf Collection", coll, "Collection")
    coll_size = b.node("GeometryNodeAttributeDomainSize", 5560, -3400, "leaves",
                       props={"component": "INSTANCES"})
    b.link(coll, "Instances", coll_size, "Geometry")
    use_coll = b.node("FunctionNodeCompare", 5740, -3400, "leaves", "collection given",
                      props={"data_type": "INT", "operation": "GREATER_THAN"},
                      inputs={"B": 0})
    b.link(coll_size, "Instance Count", use_coll, "A")
    leaf_final = b.node("GeometryNodeSwitch", 6480, -2900, "leaves", "leaf source",
                        props={"input_type": "GEOMETRY"})
    b.link(use_coll, "Result", leaf_final, "Switch")
    b.link(single_leaf, "Output", leaf_final, "False")
    b.link(coll, "Instances", leaf_final, "True")

    variant_max = b.math("SUBTRACT", 5940, -3400, "leaves", b=1.0)
    b.link(coll_size, "Instance Count", variant_max, 0)
    variant_seed = b.math("ADD", 5940, -3560, "leaves", b=7.0)
    b.link(gi2, "Seed", variant_seed, 0)
    variant = b.node("FunctionNodeRandomValue", 6120, -3400, "leaves", "pick a variant",
                     props={"data_type": "INT"}, inputs={"Min": 0})
    b.link(variant_max, "Value", variant, "Max")
    b.link(variant_seed, "Value", variant, "Seed")

    inst = b.node("GeometryNodeInstanceOnPoints", 6700, -1000, "leaves")
    b.link(lpts_moved, "Geometry", inst, "Points")
    b.link(leaf_final, "Output", inst, "Instance")
    b.link(use_coll, "Result", inst, "Pick Instance")
    b.link(variant, "Value", inst, "Instance Index")
    b.link(rot2, "Rotation", inst, "Rotation")
    b.link(size, "Value", inst, "Scale")

    realized = b.node("GeometryNodeRealizeInstances", 6900, -1150, "leaves")
    b.link(inst, "Instances", realized, "Geometry")
    leaf_geo = b.node("GeometryNodeSwitch", 7080, -1000, "leaves", "realize?",
                      props={"input_type": "GEOMETRY"})
    b.link(gi2, "Realize Leaves", leaf_geo, "Switch")
    b.link(inst, "Instances", leaf_geo, "False")
    b.link(realized, "Geometry", leaf_geo, "True")

    leaves_on = b.node("GeometryNodeSwitch", 7260, -1000, "leaves", "leaves on/off",
                       props={"input_type": "GEOMETRY"})
    b.link(gi2, "Build Leaves", leaves_on, "Switch")
    b.link(leaf_geo, "Output", leaves_on, "True")

    # =====================================================================
    # 6 - output
    # =====================================================================
    keep = b.node("GeometryNodeSwitch", 7260, 300, "out", "keep the surface",
                  props={"input_type": "GEOMETRY"})
    b.link(gi, "Keep Surface", keep, "Switch")
    b.link(gi, "Geometry", keep, "True")

    join = b.node("GeometryNodeJoinGeometry", 7500, 100, "out")
    b.link(keep, "Output", join, "Geometry")
    b.link(vine_out, "Output", join, "Geometry")
    b.link(leaves_on, "Output", join, "Geometry")

    go.location = (7700, 100)
    go.parent = b.frames["out"]
    b.link(join, "Geometry", go, "Geometry")

    # keep any modifier that already points at this group working
    sync_modifiers(ng, saved)
    return ng


# ---------------------------------------------------------------------------
# hook it up
# ---------------------------------------------------------------------------

def attach_to(obj, ng):
    mod = None
    for m in obj.modifiers:
        if m.type == "NODES" and m.node_group == ng:
            mod = m
            break
    if mod is None:
        mod = obj.modifiers.new(name="IvyGen", type="NODES")
        mod.node_group = ng
    return mod


def main():
    ng = build_group()
    print("[IvyGen] built %r with %d nodes" % (ng.name, len(ng.nodes)))
    obj = bpy.context.active_object if bpy.context else None
    if ATTACH_TO_ACTIVE and obj is not None and obj.type == "MESH":
        attach_to(obj, ng)
        print("[IvyGen] modifier added to %r" % obj.name)
    return ng


if __name__ == "__main__":
    main()
