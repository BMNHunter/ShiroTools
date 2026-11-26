"""
IvyGen v2 — procedural ivy that grows along mesh edges and crawls over any
surface shape. Written against the Blender 5.2 Python / Geometry Nodes API.

Builds (or rebuilds in place) a node group called "IvyGen". Modifiers already
using the group keep their settings across rebuilds. Shiro additions: a Growth
input (0-1, animate it to grow the ivy) and Miter Scale (Blender 5.3 Curve to
Mesh option that keeps the radius even around sharp bends).

Pipeline:
    edge source (menu) -> strands per metre -> trim -> resample
    -> per-point offset in the surface tangent plane
    -> surface walk (repeat zone): step, snap to surface, carry the offset
       direction over creases so it keeps going around the bend
    -> tube mesh (+UVs)  and  leaf instances -> join

Everything happens in the object's local space.
"""

import math

import bpy

GROUP_NAME = "IvyGen"
GROUP_VERSION = 3         # 2 = IvyGen 2.1.0, 3 = + Growth and Miter Scale
UV_NAME = "UVMap"         # UV attribute written on the bark tube
OFFSET_ATTR = "ivy_offset"  # scratch attribute used by the surface walk

TAU = math.pi * 2.0

# Sockets whose meaning changed in v2. Values stored by a v1 modifier are not
# carried over for these, the v2 defaults are used instead.
CHANGED_IN_V2 = {"Length"}


# ---------------------------------------------------------------------------
# helpers
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
        self.frames[key] = f

    def node(self, idname, x, y, frame=None, label=None, props=None, inputs=None):
        n = self.tree.nodes.new(idname)
        # enum properties first: several nodes rebuild their sockets on change
        for k, v in (props or {}).items():
            setattr(n, k, v)
        if frame is not None:
            n.parent = self.frames[frame]
        n.location = (x, y)
        if label:
            n.label = label
        for k, v in (inputs or {}).items():
            find_socket(n, k).default_value = v
        return n

    def link(self, out_node, out_key, in_node, in_key):
        self.tree.links.new(find_socket(out_node, out_key, output=True),
                            find_socket(in_node, in_key))

    def math(self, op, x, y, frame=None, a=None, b=None, c=None, label=None):
        n = self.node("ShaderNodeMath", x, y, frame, label, props={"operation": op})
        for idx, val in ((0, a), (1, b), (2, c)):
            if val is not None:
                n.inputs[idx].default_value = val
        return n

    def vmath(self, op, x, y, frame=None, label=None):
        return self.node("ShaderNodeVectorMath", x, y, frame, label,
                         props={"operation": op})

    @staticmethod
    def capture(node, kind, name):
        """Add a Capture Attribute item and return its socket name as a str.
        The item struct goes stale as soon as another item is added."""
        node.capture_items.new(kind, name)
        return str(node.capture_items[-1].name)


# ---------------------------------------------------------------------------
# interface
# ---------------------------------------------------------------------------

EDGE_SOURCES = ("Upward Rim", "Sharp Edges", "Open Borders", "Selection")


def build_interface(ng):
    iface = ng.interface
    panels = {}

    def panel(name, closed=False):
        panels[name] = iface.new_panel(name, default_closed=closed)

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
            try:
                s.structure_type = "SINGLE"
            except (TypeError, AttributeError):
                pass
        return s

    iface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    add("Geometry", "NodeSocketGeometry", single=False, desc="Mesh to grow ivy on")

    panel("Edges")
    add("Edge Source", "NodeSocketMenu", "Edges",
        desc="Upward Rim: the border of upward facing regions. Sharp Edges: any "
             "crease steeper than Crease Angle. Open Borders: the mesh's open "
             "boundary. Selection: a boolean edge attribute")
    add("Up Threshold", "NodeSocketFloat", "Edges", default=0.5, mn=-1.0, mx=1.0,
        desc="Upward Rim: a face counts as upward when its normal Z is above this")
    add("Crease Angle", "NodeSocketFloat", "Edges", default=math.radians(30.0),
        mn=0.0, mx=math.pi, subtype="ANGLE",
        desc="Sharp Edges: minimum angle between the two faces of an edge")
    add("Convex Only", "NodeSocketBool", "Edges", default=True,
        desc="Sharp Edges: ridges only. Off also grows in grooves and inner corners")
    sel = add("Edge Selection", "NodeSocketBool", "Edges", default=True, single=False,
              desc="Selection: plug a boolean edge attribute in the modifier")
    try:
        sel.structure_type = "DYNAMIC"
        sel.attribute_domain = "EDGE"
    except (TypeError, AttributeError):
        pass

    panel("Growth")
    add("Strand Density", "NodeSocketFloat", "Growth", default=1.0, mn=0.0, mx=100.0,
        desc="Vines per metre of selected edge. Pieces too short to earn one "
             "strand are dropped, which keeps fragmented edges on dense meshes "
             "from turning into stubble")
    add("Length", "NodeSocketFloat", "Growth", default=1.5, mn=0.0, mx=1000.0,
        subtype="DISTANCE", desc="Length of one vine along its edge, in metres")
    add("Length Random", "NodeSocketFloat", "Growth", default=0.5, mn=0.0, mx=1.0,
        subtype="FACTOR")
    add("Seed", "NodeSocketInt", "Growth", default=0, mn=-10000, mx=10000)
    add("Growth", "NodeSocketFloat", "Growth", default=1.0, mn=0.0, mx=1.0,
        desc="How far along its length each vine has grown. Animate 0 -> 1 to grow it",
        subtype="FACTOR")

    panel("Shape")
    add("Spread", "NodeSocketFloat", "Shape", default=0.25, mn=0.0, mx=100.0,
        subtype="DISTANCE",
        desc="How far the vines crawl sideways off the edge, measured along the "
             "surface")
    add("Spread Scale", "NodeSocketFloat", "Shape", default=2.5, mn=0.0, mx=100.0,
        desc="Noise frequency of the wander. Low = lazy curves, high = wiggly")
    add("Spread Start", "NodeSocketFloat", "Shape", default=0.15, mn=0.0, mx=1.0,
        subtype="FACTOR", desc="Part of each strand that stays on the edge")
    add("Droop", "NodeSocketFloat", "Shape", default=0.1, mn=0.0, mx=100.0,
        subtype="DISTANCE",
        desc="Gravity pull toward the tip, walked along the surface, so vines "
             "drape down over rims and slopes")
    add("Conform Steps", "NodeSocketInt", "Shape", default=4, mn=1, mx=32,
        desc="Surface walk iterations. More steps follow tight curvature and "
             "small details more faithfully. 3-6 covers most cases")
    add("Surface Offset", "NodeSocketFloat", "Shape", default=0.006, mn=-10.0,
        mx=10.0, subtype="DISTANCE", desc="Lift off the surface along its normal")

    panel("Vine")
    add("Build Vine Mesh", "NodeSocketBool", "Vine", default=True,
        desc="Off outputs the bare curves (cheap LOD, or render them as curves)")
    add("Segment Length", "NodeSocketFloat", "Vine", default=0.06, mn=0.001,
        mx=100.0, subtype="DISTANCE",
        desc="Spacing of vine points. The main polycount dial")
    add("Profile Resolution", "NodeSocketInt", "Vine", default=4, mn=3, mx=32,
        desc="Sides of the tube")
    add("Vine Radius", "NodeSocketFloat", "Vine", default=0.010, mn=0.0, mx=10.0,
        subtype="DISTANCE")
    add("Miter Scale", "NodeSocketBool", "Vine", default=False,
        desc="Keep the tube radius even around sharp bends (Curve to Mesh, Blender 5.3)")
    add("Tip Taper", "NodeSocketFloat", "Vine", default=0.25, mn=0.0, mx=4.0,
        desc="Radius multiplier at the tip of a strand")
    add("Bark UV Scale", "NodeSocketFloat", "Vine", default=1.0, mn=0.0, mx=1000.0,
        desc="UV tiles per metre along the vine. U runs 0-1 around the tube")
    add("Bark Material", "NodeSocketMaterial", "Vine", single=False)

    panel("Leaves")
    add("Build Leaves", "NodeSocketBool", "Leaves", default=True)
    add("Leaf Object", "NodeSocketObject", "Leaves", single=False,
        desc="Your leaf mesh, flat in XY, growing along +Y, stem at the origin. "
             "Empty = a generated UV'd card")
    add("Leaf Collection", "NodeSocketCollection", "Leaves", single=False,
        desc="Leaf variants, one picked at random per leaf. Wins over Leaf Object")
    add("Leaf Density", "NodeSocketFloat", "Leaves", default=14.0, mn=0.0, mx=1000.0,
        desc="Leaves per metre of vine")
    add("Leaf Size", "NodeSocketFloat", "Leaves", default=0.07, mn=0.0, mx=100.0,
        subtype="DISTANCE")
    add("Leaf Size Random", "NodeSocketFloat", "Leaves", default=0.4, mn=0.0, mx=1.0,
        subtype="FACTOR")
    add("Leaf Tip Scale", "NodeSocketFloat", "Leaves", default=0.45, mn=0.0, mx=4.0,
        desc="Leaf size multiplier at the tip of a strand")
    add("Leaf Rotation Random", "NodeSocketFloat", "Leaves", default=0.55, mn=0.0,
        mx=1.0, subtype="FACTOR")
    add("Leaf Material", "NodeSocketMaterial", "Leaves", single=False)
    add("Override Leaf Material", "NodeSocketBool", "Leaves", default=True,
        desc="Off keeps the materials already on your leaf mesh")
    add("Realize Leaves", "NodeSocketBool", "Leaves", default=False,
        desc="Instances are far cheaper. Realize only to edit or export them")

    panel("Output")
    add("Keep Surface", "NodeSocketBool", "Output", default=True)


# ---------------------------------------------------------------------------
# keeping existing modifiers alive across reruns
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
    """Copy every modifier's values out, keyed by socket name. Rebuilding the
    interface hands out fresh identifiers, which would orphan them."""
    old_version = ng.get("ivygen_version", 1)
    saved = []
    for mod in _modifiers_using(ng):
        values = {}
        inputs = mod.properties.inputs
        for item in _interface_inputs(ng):
            if old_version < 2 and item.name in CHANGED_IN_V2:
                continue
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
                if entry.get(field) is None:
                    continue
                try:
                    setattr(sock, field, entry[field])
                except (AttributeError, TypeError, ValueError):
                    pass
        mod.node_group = ng


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

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
    for key, label in (("edges", "1 · Edge source"),
                       ("strands", "2 · Strands"),
                       ("offset", "3 · Offset in the tangent plane"),
                       ("walk", "4 · Surface walk"),
                       ("vine", "5 · Vine tube + UVs"),
                       ("leaves", "6 · Leaves"),
                       ("out", "7 · Output")):
        b.frame(key, label)

    gi = b.node("NodeGroupInput", -1500, 0, "edges")
    gi2 = b.node("NodeGroupInput", 5600, -1600, "leaves")

    # =====================================================================
    # 1 - edge source: four boolean edge fields, one menu
    # =====================================================================
    normal = b.node("GeometryNodeInputNormal", -1250, 420, "edges")
    nz = b.node("ShaderNodeSeparateXYZ", -1080, 420, "edges")
    b.link(normal, "Normal", nz, "Vector")
    up = b.node("FunctionNodeCompare", -900, 420, "edges", "face points up",
                props={"data_type": "FLOAT", "operation": "GREATER_THAN"})
    b.link(nz, "Z", up, "A")
    b.link(gi, "Up Threshold", up, "B")
    # read on edges, a face field becomes the mean over the edge's faces
    up_faces = b.node("GeometryNodeFieldOnDomain", -720, 420, "edges",
                      "up, per face", props={"domain": "FACE", "data_type": "FLOAT"})
    b.link(up, "Result", up_faces, "Value")
    neigh = b.node("GeometryNodeInputMeshEdgeNeighbors", -720, 220, "edges")
    up_count = b.math("MULTIPLY", -540, 380, "edges", label="up faces on edge")
    b.link(up_faces, "Value", up_count, 0)
    b.link(neigh, "Face Count", up_count, 1)
    rim = b.node("FunctionNodeCompare", -360, 380, "edges", "exactly one up face",
                 props={"data_type": "FLOAT", "operation": "EQUAL"},
                 inputs={"B": 1.0, "Epsilon": 0.01})
    b.link(up_count, "Value", rim, "A")

    angle = b.node("GeometryNodeInputMeshEdgeAngle", -720, 60, "edges")
    ang = b.node("GeometryNodeSwitch", -540, 60, "edges", "convex only?",
                 props={"input_type": "FLOAT"})
    b.link(gi, "Convex Only", ang, "Switch")
    b.link(angle, "Unsigned Angle", ang, "False")
    b.link(angle, "Signed Angle", ang, "True")
    sharp = b.node("FunctionNodeCompare", -360, 60, "edges", "crease",
                   props={"data_type": "FLOAT", "operation": "GREATER_THAN"})
    b.link(ang, "Output", sharp, "A")
    b.link(gi, "Crease Angle", sharp, "B")

    border = b.node("FunctionNodeCompare", -360, 220, "edges", "open border",
                    props={"data_type": "INT", "operation": "EQUAL"}, inputs={"B": 1})
    b.link(neigh, "Face Count", border, "A")

    menu = b.node("GeometryNodeMenuSwitch", -150, 200, "edges", "edge source",
                  props={"data_type": "BOOLEAN"})
    while len(menu.enum_items) < len(EDGE_SOURCES):
        menu.enum_items.new("item")
    for i, name in enumerate(EDGE_SOURCES):
        menu.enum_items[i].name = name
    b.link(gi, "Edge Source", menu, "Menu")
    b.link(rim, "Result", menu, "Upward Rim")
    b.link(sharp, "Result", menu, "Sharp Edges")
    b.link(border, "Result", menu, "Open Borders")
    b.link(gi, "Edge Selection", menu, "Selection")
    for item in ng.interface.items_tree:
        if getattr(item, "name", "") == "Edge Source":
            item.default_value = EDGE_SOURCES[0]

    edge_curve = b.node("GeometryNodeMeshToCurve", 50, 200, "edges",
                        props={"mode": "EDGES"})
    b.link(gi, "Geometry", edge_curve, "Mesh")
    b.link(menu, "Output", edge_curve, "Selection")

    # =====================================================================
    # 2 - strands: N per metre, each trimmed to its own random stretch
    # =====================================================================
    slen = b.node("GeometryNodeSplineLength", 250, -40, "strands")
    per_m = b.math("MULTIPLY", 430, -40, "strands", label="strands on this piece")
    b.link(slen, "Length", per_m, 0)
    b.link(gi, "Strand Density", per_m, 1)
    count = b.math("ROUND", 600, -40, "strands")
    b.link(per_m, "Value", count, 0)
    dup = b.node("GeometryNodeDuplicateElements", 780, 200, "strands",
                 props={"domain": "SPLINE"})
    b.link(edge_curve, "Curve", dup, "Geometry")
    b.link(count, "Value", dup, "Amount")

    idx = b.node("GeometryNodeInputIndex", 780, -60, "strands")
    cap_id = b.node("GeometryNodeCaptureAttribute", 960, 200, "strands",
                    "strand id", props={"domain": "CURVE"})
    sid = b.capture(cap_id, "INT", "Strand")
    b.link(dup, "Geometry", cap_id, "Geometry")
    b.link(idx, "Index", cap_id, sid)

    # x: length jitter, y: start position, z: wander amplitude
    rnd = b.node("FunctionNodeRandomValue", 960, -120, "strands", "per strand",
                 props={"data_type": "FLOAT_VECTOR"},
                 inputs={"Min": (0.0, 0.0, 0.35), "Max": (1.0, 1.0, 1.0)})
    b.link(cap_id, sid, rnd, "ID")
    b.link(gi, "Seed", rnd, "Seed")
    rsep = b.node("ShaderNodeSeparateXYZ", 1140, -120, "strands")
    b.link(rnd, "Value", rsep, "Vector")

    jit = b.math("MULTIPLY", 1320, 60, "strands")
    b.link(gi, "Length Random", jit, 0)
    b.link(rsep, "X", jit, 1)
    keep = b.math("SUBTRACT", 1490, 60, "strands", a=1.0)
    b.link(jit, "Value", keep, 1)
    s_len = b.math("MULTIPLY", 1660, 60, "strands", label="strand length")
    b.link(gi, "Length", s_len, 0)
    b.link(keep, "Value", s_len, 1)
    room = b.math("SUBTRACT", 1660, -100, "strands")
    b.link(slen, "Length", room, 0)
    b.link(s_len, "Value", room, 1)
    room0 = b.math("MAXIMUM", 1830, -100, "strands", b=0.0)
    b.link(room, "Value", room0, 0)
    t_start = b.math("MULTIPLY", 2000, -100, "strands", label="start")
    b.link(rsep, "Y", t_start, 0)
    b.link(room0, "Value", t_start, 1)
    grown = b.math("MULTIPLY", 1830, 220, "strands", label="grown length")
    b.link(s_len, "Value", grown, 0)
    b.link(gi, "Growth", grown, 1)
    t_end = b.math("ADD", 2000, 60, "strands", label="end")
    b.link(t_start, "Value", t_end, 0)
    b.link(grown, "Value", t_end, 1)

    trim = b.node("GeometryNodeTrimCurve", 2180, 200, "strands", props={"mode": "LENGTH"})
    b.link(cap_id, "Geometry", trim, "Curve")
    b.link(t_start, "Value", trim, "Start_001")
    b.link(t_end, "Value", trim, "End_001")
    resample = b.node("GeometryNodeResampleCurve", 2360, 200, "strands", "polycount dial")
    resample.inputs["Mode"].default_value = "Length"
    b.link(trim, "Curve", resample, "Curve")
    b.link(gi, "Segment Length", resample, "Length")

    # =====================================================================
    # 3 - per point offset, flattened into the surface's tangent plane
    # =====================================================================
    param = b.node("GeometryNodeSplineParameter", 2360, -300, "offset")
    pos = b.node("GeometryNodeInputPosition", 2360, -460, "offset")
    tangent = b.node("GeometryNodeInputTangent", 2360, -560, "offset")
    nrm_src = b.node("GeometryNodeInputNormal", 2360, -660, "offset")
    n0 = b.node("GeometryNodeSampleNearestSurface", 2540, -600, "offset",
                "surface normal", props={"data_type": "FLOAT_VECTOR"})
    b.link(gi, "Geometry", n0, "Mesh")
    b.link(nrm_src, "Normal", n0, "Value")
    b.link(pos, "Position", n0, "Sample Position")
    side = b.vmath("CROSS_PRODUCT", 2720, -560, "offset", "sideways")
    b.link(tangent, "Tangent", side, 0)
    b.link(n0, "Value", side, 1)
    side_n = b.vmath("NORMALIZE", 2880, -560, "offset")
    b.link(side, "Vector", side_n, 0)

    w = b.math("MULTIPLY_ADD", 2540, -300, "offset", b=100.0, label="noise W")
    b.link(gi, "Seed", w, 0)
    b.link(cap_id, sid, w, 2)
    noise = b.node("ShaderNodeTexNoise", 2720, -260, "offset",
                   props={"noise_dimensions": "4D"},
                   inputs={"Detail": 2.0, "Roughness": 0.5})
    b.link(pos, "Position", noise, "Vector")
    b.link(w, "Value", noise, "W")
    b.link(gi, "Spread Scale", noise, "Scale")
    signed = b.node("ShaderNodeMapRange", 2880, -260, "offset", "-1..1",
                    props={"clamp": True},
                    inputs={"From Min": 0.25, "From Max": 0.75,
                            "To Min": -1.0, "To Max": 1.0})
    b.link(noise, "Factor", signed, "Value")

    ramp = b.node("ShaderNodeMapRange", 2720, -40, "offset", "hold the base",
                  props={"interpolation_type": "SMOOTHSTEP"},
                  inputs={"From Max": 1.0, "To Min": 0.0, "To Max": 1.0})
    b.link(param, "Factor", ramp, "Value")
    b.link(gi, "Spread Start", ramp, "From Min")
    amp = b.math("MULTIPLY", 2880, -40, "offset")
    b.link(gi, "Spread", amp, 0)
    b.link(ramp, "Result", amp, 1)
    amp2 = b.math("MULTIPLY", 3040, -40, "offset", label="per strand")
    b.link(amp, "Value", amp2, 0)
    b.link(rsep, "Z", amp2, 1)
    amp3 = b.math("MULTIPLY", 3040, -220, "offset")
    b.link(amp2, "Value", amp3, 0)
    b.link(signed, "Result", amp3, 1)
    v_side = b.vmath("SCALE", 3200, -400, "offset")
    b.link(side_n, "Vector", v_side, 0)
    b.link(amp3, "Value", v_side, "Scale")

    tdroop = b.math("MULTIPLY", 2880, -760, "offset")
    b.link(param, "Factor", tdroop, 0)
    b.link(gi, "Droop", tdroop, 1)
    tdroop2 = b.math("MULTIPLY", 3040, -760, "offset", label="droop * t^2")
    b.link(tdroop, "Value", tdroop2, 0)
    b.link(param, "Factor", tdroop2, 1)
    v_droop = b.vmath("SCALE", 3200, -700, "offset", "down")
    v_droop.inputs[0].default_value = (0.0, 0.0, -1.0)
    b.link(tdroop2, "Value", v_droop, "Scale")

    off = b.vmath("ADD", 3360, -500, "offset")
    b.link(v_side, "Vector", off, 0)
    b.link(v_droop, "Vector", off, 1)
    off_n = b.vmath("PROJECT", 3520, -620, "offset")
    b.link(off, "Vector", off_n, 0)
    b.link(n0, "Value", off_n, 1)
    off_t = b.vmath("SUBTRACT", 3680, -500, "offset", "tangential")
    b.link(off, "Vector", off_t, 0)
    b.link(off_n, "Vector", off_t, 1)
    inv_steps = b.math("DIVIDE", 3520, -360, "offset", a=1.0)
    b.link(gi, "Conform Steps", inv_steps, 1)
    off_step = b.vmath("SCALE", 3840, -440, "offset", "one step")
    b.link(off_t, "Vector", off_step, 0)
    b.link(inv_steps, "Value", off_step, "Scale")

    store_off = b.node("GeometryNodeStoreNamedAttribute", 4000, 200, "offset",
                       props={"data_type": "FLOAT_VECTOR", "domain": "POINT"},
                       inputs={"Name": OFFSET_ATTR})
    b.link(resample, "Curve", store_off, "Geometry")
    b.link(off_step, "Vector", store_off, "Value")

    # =====================================================================
    # 4 - surface walk
    #   per step: move by the offset, snap to the nearest surface point,
    #   then turn the offset by the rotation that takes the old normal a to
    #   the new normal b:   o' = o - (a + b) * (b.o) / (1 + a.b)
    #   On a flat patch nothing changes. Over a rim the offset tips down the
    #   far side, in a groove it climbs the wall, so vines keep going round.
    # =====================================================================
    rep_in = b.node("GeometryNodeRepeatInput", 4200, 200, "walk")
    rep_out = b.node("GeometryNodeRepeatOutput", 6000, 200, "walk")
    rep_in.pair_with_output(rep_out)
    b.link(store_off, "Geometry", rep_in, "Geometry")
    b.link(gi, "Conform Steps", rep_in, "Iterations")

    o = b.node("GeometryNodeInputNamedAttribute", 4200, -100, "walk",
               props={"data_type": "FLOAT_VECTOR"}, inputs={"Name": OFFSET_ATTR})
    wpos = b.node("GeometryNodeInputPosition", 4200, -240, "walk")
    wnrm = b.node("GeometryNodeInputNormal", 4200, -340, "walk")
    na = b.node("GeometryNodeSampleNearestSurface", 4400, -340, "walk",
                "normal here (a)", props={"data_type": "FLOAT_VECTOR"})
    b.link(gi, "Geometry", na, "Mesh")
    b.link(wnrm, "Normal", na, "Value")
    b.link(wpos, "Position", na, "Sample Position")

    stepped = b.vmath("ADD", 4400, -100, "walk")
    b.link(wpos, "Position", stepped, 0)
    b.link(o, "Attribute", stepped, 1)
    snap = b.node("GeometryNodeProximity", 4580, -100, "walk", "snap",
                  props={"target_element": "FACES"})
    b.link(gi, "Geometry", snap, "Target")
    b.link(stepped, "Vector", snap, "Source Position")
    nb = b.node("GeometryNodeSampleNearestSurface", 4760, -340, "walk",
                "normal there (b)", props={"data_type": "FLOAT_VECTOR"})
    b.link(gi, "Geometry", nb, "Mesh")
    b.link(wnrm, "Normal", nb, "Value")
    b.link(snap, "Position", nb, "Sample Position")

    bo = b.vmath("DOT_PRODUCT", 4940, -200, "walk", "b.o")
    b.link(nb, "Value", bo, 0)
    b.link(o, "Attribute", bo, 1)
    ab = b.vmath("DOT_PRODUCT", 4940, -360, "walk", "a.b")
    b.link(na, "Value", ab, 0)
    b.link(nb, "Value", ab, 1)
    den = b.math("ADD", 5100, -360, "walk", b=1.0)
    b.link(ab, "Value", den, 0)
    den_safe = b.math("MAXIMUM", 5260, -360, "walk", b=0.05,
                      label="guard: normal flipped")
    b.link(den, "Value", den_safe, 0)
    k = b.math("DIVIDE", 5420, -260, "walk")
    b.link(bo, "Value", k, 0)
    b.link(den_safe, "Value", k, 1)
    apb = b.vmath("ADD", 5100, -500, "walk", "a + b")
    b.link(na, "Value", apb, 0)
    b.link(nb, "Value", apb, 1)
    turn = b.vmath("SCALE", 5580, -420, "walk")
    b.link(apb, "Vector", turn, 0)
    b.link(k, "Value", turn, "Scale")
    o_new = b.vmath("SUBTRACT", 5740, -260, "walk", "carried offset")
    b.link(o, "Attribute", o_new, 0)
    b.link(turn, "Vector", o_new, 1)

    lift = b.vmath("SCALE", 4940, -40, "walk")
    b.link(nb, "Value", lift, 0)
    b.link(gi, "Surface Offset", lift, "Scale")
    target = b.vmath("ADD", 5100, -40, "walk", "on the surface")
    b.link(snap, "Position", target, 0)
    b.link(lift, "Vector", target, 1)

    # both results are read off the same (unmoved) geometry, then applied
    cap_step = b.node("GeometryNodeCaptureAttribute", 5420, 200, "walk",
                      props={"domain": "POINT"})
    c_pos = b.capture(cap_step, "VECTOR", "Target")
    c_off = b.capture(cap_step, "VECTOR", "Offset")
    b.link(rep_in, "Geometry", cap_step, "Geometry")
    b.link(target, "Vector", cap_step, c_pos)
    b.link(o_new, "Vector", cap_step, c_off)
    move = b.node("GeometryNodeSetPosition", 5600, 200, "walk")
    b.link(cap_step, "Geometry", move, "Geometry")
    b.link(cap_step, c_pos, move, "Position")
    keep_off = b.node("GeometryNodeStoreNamedAttribute", 5800, 200, "walk",
                      props={"data_type": "FLOAT_VECTOR", "domain": "POINT"},
                      inputs={"Name": OFFSET_ATTR})
    b.link(move, "Geometry", keep_off, "Geometry")
    b.link(cap_step, c_off, keep_off, "Value")
    b.link(keep_off, "Geometry", rep_out, "Geometry")

    drop_attr = b.node("GeometryNodeRemoveAttribute", 6200, 200, "walk",
                       inputs={"Name": OFFSET_ATTR})
    drop_attr.inputs["Pattern Mode"].default_value = "Exact"
    b.link(rep_out, "Geometry", drop_attr, "Geometry")

    # radius taper, and what the leaves need frozen before Curve to Points
    taper = b.node("ShaderNodeMapRange", 6200, -60, "walk", "taper",
                   inputs={"From Min": 0.0, "From Max": 1.0, "To Min": 1.0})
    b.link(param, "Factor", taper, "Value")
    b.link(gi, "Tip Taper", taper, "To Max")
    rad = b.math("MULTIPLY", 6380, -60, "walk", label="radius")
    b.link(gi, "Vine Radius", rad, 0)
    b.link(taper, "Result", rad, 1)
    set_rad = b.node("GeometryNodeSetCurveRadius", 6380, 200, "walk")
    b.link(drop_attr, "Geometry", set_rad, "Curve")
    b.link(rad, "Value", set_rad, "Radius")

    leaf_n = b.node("GeometryNodeSampleNearestSurface", 6560, -100, "walk",
                    "normal for leaves", props={"data_type": "FLOAT_VECTOR"})
    b.link(gi, "Geometry", leaf_n, "Mesh")
    b.link(nrm_src, "Normal", leaf_n, "Value")
    b.link(pos, "Position", leaf_n, "Sample Position")
    cap_pt = b.node("GeometryNodeCaptureAttribute", 6740, 200, "walk",
                    "freeze for leaves", props={"domain": "POINT"})
    it_t = b.capture(cap_pt, "FLOAT", "T")
    it_n = b.capture(cap_pt, "VECTOR", "SurfNormal")
    b.link(set_rad, "Curve", cap_pt, "Geometry")
    b.link(param, "Factor", cap_pt, it_t)
    b.link(leaf_n, "Value", cap_pt, it_n)
    vines = cap_pt

    # =====================================================================
    # 5 - tube + UVs (U around, V along in metres)
    # =====================================================================
    v_len = b.math("MULTIPLY", 6940, 0, "vine", label="V")
    b.link(param, "Length", v_len, 0)
    b.link(gi, "Bark UV Scale", v_len, 1)
    cap_v = b.node("GeometryNodeCaptureAttribute", 7120, 200, "vine",
                   props={"domain": "POINT"})
    it_v = b.capture(cap_v, "FLOAT", "V")
    b.link(vines, "Geometry", cap_v, "Geometry")
    b.link(v_len, "Value", cap_v, it_v)

    # a full-turn arc is an open ring: the seam gets its own vertex column
    res1 = b.math("ADD", 6940, -320, "vine", b=1.0, label="res + 1")
    b.link(gi, "Profile Resolution", res1, 0)
    profile = b.node("GeometryNodeCurveArc", 7120, -320, "vine", "tube profile",
                     props={"mode": "RADIUS"},
                     inputs={"Radius": 1.0, "Sweep Angle": TAU,
                             "Connect Center": False, "Invert Arc": False})
    b.link(res1, "Value", profile, "Resolution")
    p_idx = b.node("GeometryNodeInputIndex", 7120, -560, "vine")
    p_span = b.math("SUBTRACT", 7120, -640, "vine", b=1.0)
    b.link(res1, "Value", p_span, 0)
    p_u = b.math("DIVIDE", 7300, -580, "vine", label="U")
    b.link(p_idx, "Index", p_u, 0)
    b.link(p_span, "Value", p_u, 1)
    cap_u = b.node("GeometryNodeCaptureAttribute", 7480, -320, "vine",
                   props={"domain": "POINT"})
    it_u = b.capture(cap_u, "FLOAT", "U")
    b.link(profile, "Curve", cap_u, "Geometry")
    b.link(p_u, "Value", cap_u, it_u)

    tube = b.node("GeometryNodeCurveToMesh", 7680, 200, "vine",
                  inputs={"Fill Caps": False})
    tube_r = b.node("GeometryNodeInputRadius", 7480, 40, "vine")
    b.link(cap_v, "Geometry", tube, "Curve")
    b.link(cap_u, "Geometry", tube, "Profile Curve")
    b.link(tube_r, "Radius", tube, "Scale")   # 5.x: radius goes through Scale
    if any(sk.name == "Miter Scale" for sk in tube.inputs):   # 5.3+
        b.link(gi, "Miter Scale", tube, "Miter Scale")

    uv = b.node("ShaderNodeCombineXYZ", 7680, -60, "vine", "UV")
    b.link(cap_u, it_u, uv, "X")
    b.link(cap_v, it_v, uv, "Y")
    store_uv = b.node("GeometryNodeStoreNamedAttribute", 7860, 200, "vine",
                      props={"data_type": "FLOAT2", "domain": "CORNER"},
                      inputs={"Name": UV_NAME})
    b.link(tube, "Mesh", store_uv, "Geometry")
    b.link(uv, "Vector", store_uv, "Value")
    smooth = b.node("GeometryNodeSetShadeSmooth", 8040, 200, "vine",
                    props={"domain": "FACE"}, inputs={"Shade Smooth": True})
    b.link(store_uv, "Geometry", smooth, "Geometry")
    bark = b.node("GeometryNodeSetMaterial", 8220, 200, "vine")
    b.link(smooth, "Geometry", bark, "Geometry")
    b.link(gi, "Bark Material", bark, "Material")
    vine_out = b.node("GeometryNodeSwitch", 8400, 200, "vine", "tube or curves",
                      props={"input_type": "GEOMETRY"})
    b.link(gi, "Build Vine Mesh", vine_out, "Switch")
    b.link(vines, "Geometry", vine_out, "False")
    b.link(bark, "Geometry", vine_out, "True")

    # =====================================================================
    # 6 - leaves
    # =====================================================================
    spacing = b.math("DIVIDE", 5800, -1200, "leaves", a=1.0, label="1 / density")
    b.link(gi2, "Leaf Density", spacing, 1)
    lpts = b.node("GeometryNodeCurveToPoints", 6940, -1000, "leaves",
                  props={"mode": "LENGTH"})
    b.link(vines, "Geometry", lpts, "Curve")
    b.link(spacing, "Value", lpts, "Length")

    rot = b.node("FunctionNodeAxesToRotation", 7120, -1150, "leaves", "lie flat",
                 props={"primary_axis": "Z", "secondary_axis": "Y"})
    b.link(cap_pt, it_n, rot, "Primary Axis")
    b.link(lpts, "Tangent", rot, "Secondary Axis")

    # x: tilt, y: sideways nudge, z: yaw
    lrnd = b.node("FunctionNodeRandomValue", 6000, -1400, "leaves", "per leaf",
                  props={"data_type": "FLOAT_VECTOR"},
                  inputs={"Min": (-0.6, -0.3, -math.pi), "Max": (0.6, 0.3, math.pi)})
    b.link(gi2, "Seed", lrnd, "Seed")
    rr = b.node("ShaderNodeCombineXYZ", 6000, -1640, "leaves")
    b.link(gi2, "Leaf Rotation Random", rr, "X")
    b.link(gi2, "Leaf Rotation Random", rr, "Z")
    euler = b.vmath("MULTIPLY", 6180, -1500, "leaves", "tilt / yaw")
    b.link(lrnd, "Value", euler, 0)
    b.link(rr, "Vector", euler, 1)
    e_rot = b.node("FunctionNodeEulerToRotation", 6360, -1500, "leaves")
    b.link(euler, "Vector", e_rot, "Euler")
    rot2 = b.node("FunctionNodeRotateRotation", 7300, -1250, "leaves", "jitter",
                  props={"rotation_space": "LOCAL"})
    b.link(rot, "Rotation", rot2, "Rotation")
    b.link(e_rot, "Rotation", rot2, "Rotate By")

    lsep = b.node("ShaderNodeSeparateXYZ", 6180, -1700, "leaves")
    b.link(lrnd, "Value", lsep, "Vector")
    lside = b.vmath("CROSS_PRODUCT", 7120, -1700, "leaves")
    b.link(lpts, "Tangent", lside, 0)
    b.link(cap_pt, it_n, lside, 1)
    lside_n = b.vmath("NORMALIZE", 7300, -1700, "leaves")
    b.link(lside, "Vector", lside_n, 0)
    nudge_amt = b.math("MULTIPLY", 7300, -1860, "leaves")
    b.link(lsep, "Y", nudge_amt, 0)
    b.link(gi2, "Leaf Size", nudge_amt, 1)
    nudge = b.vmath("SCALE", 7480, -1700, "leaves", "off the axis")
    b.link(lside_n, "Vector", nudge, 0)
    b.link(nudge_amt, "Value", nudge, "Scale")
    lift_leaf = b.vmath("SCALE", 7480, -1880, "leaves", "clear the bark")
    b.link(cap_pt, it_n, lift_leaf, 0)
    b.link(gi2, "Vine Radius", lift_leaf, "Scale")
    nudge2 = b.vmath("ADD", 7660, -1780, "leaves")
    b.link(nudge, "Vector", nudge2, 0)
    b.link(lift_leaf, "Vector", nudge2, 1)
    lmoved = b.node("GeometryNodeSetPosition", 7660, -1000, "leaves")
    b.link(lpts, "Points", lmoved, "Geometry")
    b.link(nudge2, "Vector", lmoved, "Offset")

    size_min = b.math("SUBTRACT", 6000, -2000, "leaves", a=1.0)
    b.link(gi2, "Leaf Size Random", size_min, 1)
    size_seed = b.math("ADD", 6000, -2160, "leaves", b=1.0)
    b.link(gi2, "Seed", size_seed, 0)
    size_r = b.node("FunctionNodeRandomValue", 6180, -2000, "leaves", "size",
                    props={"data_type": "FLOAT"}, inputs={"Max": 1.0})
    b.link(size_min, "Value", size_r, "Min")
    b.link(size_seed, "Value", size_r, "Seed")
    leaf_taper = b.node("ShaderNodeMapRange", 6360, -2160, "leaves", "smaller at tip",
                        inputs={"From Min": 0.0, "From Max": 1.0, "To Min": 1.0})
    b.link(cap_pt, it_t, leaf_taper, "Value")
    b.link(gi2, "Leaf Tip Scale", leaf_taper, "To Max")
    size_a = b.math("MULTIPLY", 6540, -2000, "leaves")
    b.link(gi2, "Leaf Size", size_a, 0)
    b.link(size_r, "Value", size_a, 1)
    size = b.math("MULTIPLY", 6720, -2060, "leaves", label="leaf scale")
    b.link(size_a, "Value", size, 0)
    b.link(leaf_taper, "Result", size, 1)

    # leaf source: collection > object > generated card
    card = b.node("GeometryNodeMeshGrid", 6000, -2500, "leaves", "fallback card",
                  inputs={"Size X": 1.0, "Size Y": 1.0, "Vertices X": 2, "Vertices Y": 2})
    card_uv = b.node("GeometryNodeStoreNamedAttribute", 6180, -2500, "leaves",
                     props={"data_type": "FLOAT2", "domain": "CORNER"},
                     inputs={"Name": UV_NAME})
    b.link(card, "Mesh", card_uv, "Geometry")
    b.link(card, "UV Map", card_uv, "Value")
    card_xf = b.node("GeometryNodeTransform", 6360, -2500, "leaves", "stem at origin")
    card_xf.inputs["Translation"].default_value = (0.0, 0.5, 0.0)
    b.link(card_uv, "Geometry", card_xf, "Geometry")

    leaf_obj = b.node("GeometryNodeObjectInfo", 6000, -2760, "leaves",
                      props={"transform_space": "ORIGINAL"}, inputs={"As Instance": False})
    b.link(gi2, "Leaf Object", leaf_obj, "Object")
    obj_size = b.node("GeometryNodeAttributeDomainSize", 6180, -2760, "leaves",
                      props={"component": "MESH"})
    b.link(leaf_obj, "Geometry", obj_size, "Geometry")
    no_obj = b.node("FunctionNodeCompare", 6360, -2760, "leaves", "no leaf object",
                    props={"data_type": "INT", "operation": "EQUAL"}, inputs={"B": 0})
    b.link(obj_size, "Point Count", no_obj, "A")
    single = b.node("GeometryNodeSwitch", 6540, -2600, "leaves", "object or card",
                    props={"input_type": "GEOMETRY"})
    b.link(no_obj, "Result", single, "Switch")
    b.link(leaf_obj, "Geometry", single, "False")
    b.link(card_xf, "Geometry", single, "True")
    leaf_mat = b.node("GeometryNodeSetMaterial", 6720, -2600, "leaves")
    b.link(single, "Output", leaf_mat, "Geometry")
    b.link(gi2, "Leaf Material", leaf_mat, "Material")
    single_m = b.node("GeometryNodeSwitch", 6900, -2600, "leaves", "material override",
                      props={"input_type": "GEOMETRY"})
    b.link(gi2, "Override Leaf Material", single_m, "Switch")
    b.link(single, "Output", single_m, "False")
    b.link(leaf_mat, "Geometry", single_m, "True")

    coll = b.node("GeometryNodeCollectionInfo", 6000, -3020, "leaves", "leaf variants",
                  props={"transform_space": "ORIGINAL"},
                  inputs={"Separate Children": True, "Reset Children": True})
    b.link(gi2, "Leaf Collection", coll, "Collection")
    coll_size = b.node("GeometryNodeAttributeDomainSize", 6180, -3020, "leaves",
                       props={"component": "INSTANCES"})
    b.link(coll, "Instances", coll_size, "Geometry")
    use_coll = b.node("FunctionNodeCompare", 6360, -3020, "leaves", "collection given",
                      props={"data_type": "INT", "operation": "GREATER_THAN"},
                      inputs={"B": 0})
    b.link(coll_size, "Instance Count", use_coll, "A")
    leaf_src = b.node("GeometryNodeSwitch", 7080, -2800, "leaves", "leaf source",
                      props={"input_type": "GEOMETRY"})
    b.link(use_coll, "Result", leaf_src, "Switch")
    b.link(single_m, "Output", leaf_src, "False")
    b.link(coll, "Instances", leaf_src, "True")
    v_max = b.math("SUBTRACT", 6540, -3020, "leaves", b=1.0)
    b.link(coll_size, "Instance Count", v_max, 0)
    variant = b.node("FunctionNodeRandomValue", 6720, -3020, "leaves", "pick variant",
                     props={"data_type": "INT"}, inputs={"Min": 0})
    b.link(v_max, "Value", variant, "Max")
    b.link(gi2, "Seed", variant, "Seed")

    inst = b.node("GeometryNodeInstanceOnPoints", 7880, -1000, "leaves")
    b.link(lmoved, "Geometry", inst, "Points")
    b.link(leaf_src, "Output", inst, "Instance")
    b.link(use_coll, "Result", inst, "Pick Instance")
    b.link(variant, "Value", inst, "Instance Index")
    b.link(rot2, "Rotation", inst, "Rotation")
    b.link(size, "Value", inst, "Scale")
    realized = b.node("GeometryNodeRealizeInstances", 8060, -1150, "leaves")
    b.link(inst, "Instances", realized, "Geometry")
    leaf_geo = b.node("GeometryNodeSwitch", 8240, -1000, "leaves", "realize?",
                      props={"input_type": "GEOMETRY"})
    b.link(gi2, "Realize Leaves", leaf_geo, "Switch")
    b.link(inst, "Instances", leaf_geo, "False")
    b.link(realized, "Geometry", leaf_geo, "True")
    leaves_on = b.node("GeometryNodeSwitch", 8420, -1000, "leaves", "leaves on/off",
                       props={"input_type": "GEOMETRY"})
    b.link(gi2, "Build Leaves", leaves_on, "Switch")
    b.link(leaf_geo, "Output", leaves_on, "True")

    # =====================================================================
    # 7 - output
    # =====================================================================
    keep_s = b.node("GeometryNodeSwitch", 8640, 400, "out", "keep surface",
                    props={"input_type": "GEOMETRY"})
    b.link(gi, "Keep Surface", keep_s, "Switch")
    b.link(gi, "Geometry", keep_s, "True")
    join = b.node("GeometryNodeJoinGeometry", 8840, 100, "out")
    b.link(keep_s, "Output", join, "Geometry")
    b.link(vine_out, "Output", join, "Geometry")
    b.link(leaves_on, "Output", join, "Geometry")
    go = b.node("NodeGroupOutput", 9040, 100, "out")
    b.link(join, "Geometry", go, "Geometry")

    ng["ivygen_version"] = GROUP_VERSION
    sync_modifiers(ng, saved)
    return ng


# ---------------------------------------------------------------------------
# hook it up
# ---------------------------------------------------------------------------

def attach_to(obj, ng):
    for m in obj.modifiers:
        if m.type == "NODES" and m.node_group == ng:
            return m
    mod = obj.modifiers.new(name="IvyGen", type="NODES")
    mod.node_group = ng
    return mod

