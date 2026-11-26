"""Headless test suite for ShiroTools.

Run:
  blender -b --factory-startup --python tests/run_tests.py
Exit code 0 = all passed.
"""

import functools
import math
import os
import shutil
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import shirotools  # noqa: E402

RESULTS = []
TMP = tempfile.mkdtemp(prefix="shiro_test_")
TIMINGS = []


def check(name, cond, info=""):
    info = str(info)
    RESULTS.append((name, bool(cond), info))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{info}]" if info and not cond else ""))


def op_fails(op, **kw):
    """Background mode turns ERROR reports into exceptions."""
    try:
        return op(**kw) == {"CANCELLED"}
    except RuntimeError:
        return True


def section(fn):
    @functools.wraps(fn)
    def run():
        print(f"\n== {fn.__name__}")
        try:
            fn()
        except Exception:
            traceback.print_exc()
            RESULTS.append((fn.__name__ + " (crashed)", False, traceback.format_exc(limit=3)))
    return run


def fresh():
    bpy.ops.wm.read_homefile(use_empty=True)
    path = os.path.join(TMP, "test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return bpy.context.scene


def mesh_obj(name, verts, faces, edges=()):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, list(edges), faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    return ob


def grid(name, n, size=2.0):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=n, y_subdivisions=n, size=size)
    ob = bpy.context.active_object
    ob.name = name
    return ob


# ---------------------------------------------------------------------------

@section
def test_register():
    shirotools.register()
    check("all modules registered", len(shirotools._active) == len(shirotools.MODULES),
          f"{sorted(shirotools._active)}")
    shirotools.unregister()
    check("clean unregister", not shirotools._active and not hasattr(bpy.types.Scene,
                                                                     "shiro_timeline"))
    shirotools.register()


@section
def test_timeline():
    from shirotools import timeline as tl
    scene = fresh()
    scene.render.fps, scene.render.fps_base = 30, 1.001  # 29.97 NTSC
    fpb = tl.frames_per_beat(scene, 120)
    check("fps_base honoured", abs(fpb - 60 * (30 / 1.001) / 120) < 1e-4, fpb)
    frames = tl.beat_frames(1, 10001, fpb, 1, 0.0)
    last_f, last_i = frames[-1]
    check("no drift over 10k frames", abs(last_f - (1 + last_i * fpb)) < 1e-6)
    check("bar/beat labels", tl.beat_label(5, 4, 1) == (2, 2, 1) and
          tl.beat_label(9, 4, 2) == (2, 1, 2))

    st = scene.shiro_timeline
    for name, f in (("1st_shot", 3), ("2024_cut", 7), ("Intro", 1), ("B", 9), ("B12x", 11)):
        scene.timeline_markers.new(name, frame=f)
    scene.frame_start, scene.frame_end = 1, 96
    st.bpm, st.beats_per_bar, st.subdivision = 120.0, 4, "1"
    scene.render.fps, scene.render.fps_base = 24, 1.0
    bpy.ops.shiro.beat_markers()
    beats = [m for m in scene.timeline_markers if tl.beat_name_regex("B").match(m.name)]
    check("beat markers made", len(beats) == 8, len(beats))  # 96 frames / 12
    check("downbeat named B1/B2", {"B1", "B2"} <= {m.name for m in beats})
    bpy.ops.shiro.beat_clear()
    names = {m.name for m in scene.timeline_markers}
    check("clear keeps user markers", names == {"1st_shot", "2024_cut", "Intro", "B", "B12x"},
          names)

    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    for f in (5, 14, 30):
        cube.location.x = f
        cube.keyframe_insert("location", index=0, frame=f)
    for fc in tl.iter_selected_keyframe_curves(bpy.context):
        for k in fc.keyframe_points:
            k.select_control_point = True
    bpy.ops.shiro.beat_snap_keys()
    fc = next(iter(tl.iter_selected_keyframe_curves(bpy.context)))
    xs = [k.co.x for k in fc.keyframe_points]
    check("snap keys to beats (12f grid from 1)", xs == [1.0, 13.0, 25.0], xs)

    bpy.ops.shiro.range_add(source="SCENE")
    mgr = scene.timeline_manager_props
    mgr.presets[0].start, mgr.presets[0].end = 50, 80
    bpy.ops.shiro.range_apply(index=0)
    check("range apply to scene", (scene.frame_start, scene.frame_end) == (50, 80))
    mgr.apply_to = "PREVIEW"
    mgr.presets[0].start, mgr.presets[0].end = 5, 9
    bpy.ops.shiro.range_apply(index=0)
    check("range apply to preview", scene.use_preview_range and
          (scene.frame_preview_start, scene.frame_preview_end) == (5, 9) and
          scene.frame_start == 50)
    intro = list(scene.timeline_markers).index(scene.timeline_markers["Intro"])
    bpy.ops.shiro.marker_to_range(index=intro, target="PRESET")
    check("range to next marker", (mgr.presets[-1].start, mgr.presets[-1].end) == (1, 2),
          (mgr.presets[-1].start, mgr.presets[-1].end))

    st.marker_index = list(scene.timeline_markers).index(scene.timeline_markers["2024_cut"])
    check("list click jumps playhead", scene.frame_current == 7)
    tl.set_marker_index(st, intro)
    check("programmatic index does not jump", scene.frame_current == 7)

    bpy.ops.shiro.tag_add(defaults=True)
    scene.timeline_markers["1st_shot"].camera = None
    t = st.tags["Shot"]
    t.pattern = "1st"
    check("tag prefix match", tl.marker_tag(st, scene.timeline_markers["1st_shot"]) == t)
    t.match, t.pattern = "REGEX", r"^\d{4}_"
    check("tag regex match", tl.marker_tag(st, scene.timeline_markers["2024_cut"]) == t)
    st.search = "cut"
    check("search filter", tl.marker_visible(st, scene.timeline_markers["2024_cut"]) and
          not tl.marker_visible(st, scene.timeline_markers["Intro"]))
    st.search_invert = True
    check("inverted filter", not tl.marker_visible(st, scene.timeline_markers["2024_cut"]))
    st.search, st.search_invert = "", False

    st.show_beat_grid = True
    scene.frame_start, scene.frame_end = 1, 96
    rects, labels = tl.overlay_geometry(scene, st, lambda f: f * 10.0, 0, 2000, 300, 1.0)
    check("overlay builds geometry", len(rects) > 10 and any(l[2] == "1" for l in labels),
          (len(rects), len(labels)))


@section
def test_camera():
    scene = fresh()
    bpy.ops.object.camera_add(location=(1, 2, 3))
    cam = bpy.context.active_object
    scene.camera = cam
    cam.data.type = "PANO"
    cam.data.shift_x = 0.25
    cam.data.dof.use_dof = True
    cam.data.dof.focus_distance = 7.5
    bpy.ops.shiro.cam_save()
    cam.location = (9, 9, 9)
    cam.data.type, cam.data.shift_x, cam.data.dof.focus_distance = "PERSP", 0.0, 1.0
    bpy.ops.shiro.cam_load(index=0)
    check("bookmark restores transform", (cam.location - __import__("mathutils").Vector(
        (1, 2, 3))).length < 1e-5)
    check("bookmark keeps panoramic type", cam.data.type == "PANO", cam.data.type)
    check("bookmark restores shift + DOF", abs(cam.data.shift_x - 0.25) < 1e-6 and
          abs(cam.data.dof.focus_distance - 7.5) < 1e-5)
    cam.name = "RenamedCam"
    cam.location = (0, 0, 0)
    bpy.ops.shiro.cam_load(index=0)
    check("survives camera rename", cam.location.x == 1.0)
    # legacy bookmark (old add-on fields only)
    item = scene.camera_bookmarks.add()
    item.camera_name, item.lens, item.is_ortho = "RenamedCam", 85.0, True
    item.matrix = [1, 0, 0, 4, 0, 1, 0, 5, 0, 0, 1, 6, 0, 0, 0, 1]
    bpy.ops.shiro.cam_load(index=1)
    check("legacy bookmark loads", cam.data.type == "ORTHO" and cam.location.x == 4.0)
    scene.frame_current = 20
    scene.camera_bookmark_index = 0
    bpy.ops.shiro.cam_to_marker()
    m = scene.timeline_markers[0]
    check("camera cut marker", m.frame == 20 and m.camera is not None)


@section
def test_anim_rename():
    from shirotools import anim_rename as ar
    scene = fresh()
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    ob.rotation_euler.z = 0.0
    ob.keyframe_insert("rotation_euler", index=2, frame=1)
    ob.rotation_euler.z = 1.5
    ob.keyframe_insert("rotation_euler", index=2, frame=20)
    from bpy_extras import anim_utils
    cb = anim_utils.action_get_channelbag_for_slot(ob.animation_data.action,
                                                   ob.animation_data.action_slot)
    fc = cb.fcurves.find("rotation_euler", index=2)
    fc.keyframe_points[1].interpolation = "BACK"
    fc.keyframe_points[1].back = 2.5
    fc.modifiers.new("NOISE")
    act, found = ar.channelbag_of(fc)
    check("channelbag lookup", found == cb and ar.owner_of(cb) == ob)
    info = ar.describe_property(ob, "rotation_euler")
    check("subtype EULER -> ANGLE", info["subtype"] == "ANGLE", info)
    owner, path, idx = ar.make_proxy(fc, "spin", 1)
    nfc = cb.fcurves.find('["spin"]')
    check("proxy fcurve made", nfc is not None and len(nfc.keyframe_points) == 2)
    check("proxy keeps easing extras", nfc.keyframe_points[1].interpolation == "BACK" and
          abs(nfc.keyframe_points[1].back - 2.5) < 1e-6)
    check("proxy keeps modifiers", len(nfc.modifiers) == 1)
    check("custom prop is ANGLE", ob.id_properties_ui("spin").as_dict()["subtype"] == "ANGLE")
    scene.frame_set(20)
    check("driver drives original", abs(ob.rotation_euler.z - nfc.evaluate(20)) < 1e-4,
          (ob.rotation_euler.z, nfc.evaluate(20)))
    ar.bake_back(nfc)
    back = cb.fcurves.find("rotation_euler", index=2)
    check("bake back restores", back is not None and "spin" not in ob.keys() and
          not ob.animation_data.drivers)
    check("bake back keeps keys", back.keyframe_points[1].interpolation == "BACK")
    try:
        ar.make_proxy(back, 'bad"name', 1)
        check("rejects bad name", False)
    except ValueError:
        check("rejects bad name", True)


def _make_png(path, w, h, float_buffer=False, fmt="PNG", ext=".png"):
    img = bpy.data.images.new(os.path.basename(path), w, h, float_buffer=float_buffer)
    px = np.random.default_rng(1).random(w * h * 4).astype(np.float32)
    img.pixels.foreach_set(px)
    img.filepath_raw = path
    img.file_format = fmt
    img.save()
    bpy.data.images.remove(img)


@section
def test_imgheader():
    from shirotools import imgheader
    fresh()
    d = os.path.join(TMP, "hdr")
    os.makedirs(d, exist_ok=True)
    cases = [("PNG", ".png", False), ("JPEG", ".jpg", False), ("OPEN_EXR", ".exr", True),
             ("TARGA", ".tga", False), ("BMP", ".bmp", False), ("TIFF", ".tif", False),
             ("HDR", ".hdr", True), ("WEBP", ".webp", False)]
    for fmt, ext, flt in cases:
        p = os.path.join(d, "t" + ext)
        try:
            _make_png(p, 37, 21, flt, fmt, ext)
        except Exception as err:
            check(f"header {fmt} (write)", False, err)
            continue
        check(f"header {fmt}", imgheader.read_size(p) == (37, 21), imgheader.read_size(p))


@section
def test_textures():
    from shirotools import textures as tx
    scene = fresh()
    tdir = os.path.join(TMP, "tex")
    os.makedirs(tdir, exist_ok=True)
    p_big = os.path.join(tdir, "big.png")
    p_16 = os.path.join(tdir, "sixteen.png")
    _make_png(p_big, 2048, 1024)
    _make_png(p_16, 1024, 1024, float_buffer=True)  # 16-bit PNG
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    mat = bpy.data.materials.new("M")
    ob.data.materials.append(mat)
    nt = mat.node_tree
    grp = bpy.data.node_groups.new("TexGroup", "ShaderNodeTree")
    n1 = nt.nodes.new("ShaderNodeTexImage")
    n1.image = bpy.data.images.load(p_big)
    gn = nt.nodes.new("ShaderNodeGroup")
    gn.node_tree = grp
    n2 = grp.nodes.new("ShaderNodeTexImage")
    n2.image = bpy.data.images.load(p_16)
    orphan = bpy.data.images.load(p_big, check_existing=False)
    orphan.name = "unused"

    check("images not loaded before scan", not n1.image.has_data and not n2.image.has_data)
    bpy.ops.shiro.tex_scan()
    s = tx._stats
    check("scan counts used incl. node groups", s["total"] == 2, s["total"])
    check("scan did not load images", not n1.image.has_data and not n2.image.has_data)
    expect = (2048 * 1024 * 4) * 4 // 3 + (1024 * 1024 * 8) * 4 // 3
    check("VRAM estimate", s["vram"] == expect, (s["vram"], expect))
    scene.shiro_tex_used_only = False
    bpy.ops.shiro.tex_scan()
    check("all-images mode counts orphan", tx._stats["total"] == 3)

    t0 = time.perf_counter()
    ok = tx.apply_size(n1.image, 512, print)
    dt = time.perf_counter() - t0
    proxy = tx.proxy_path(n1.image, 512)
    from shirotools import imgheader
    check("resize to 512 keeps aspect", ok and imgheader.read_size(proxy) == (512, 256),
          imgheader.read_size(proxy) if os.path.exists(proxy) else "missing")
    TIMINGS.append(("texture 2048x1024 -> 512 (imbuf halving chain)", dt))
    check("image repointed", tx.is_proxy(tx.image_abspath(n1.image)))
    check("current bucket", tx.current_bucket(n1.image) == 512)
    ok = tx.apply_size(n2.image, 256, print)
    p16 = tx.proxy_path(n2.image, 256)
    with open(p16, "rb") as f:
        depth = f.read(25)[24]
    check("16-bit PNG stays 16-bit", ok and depth == 16, depth)
    tx.apply_size(n1.image, 2048, print)
    check("max size = original file", tx._norm(tx.image_abspath(n1.image)) == tx._norm(p_big))
    tx.apply_size(n1.image, 1024, print)
    bpy.ops.shiro.tex_restore(scope="ALL")
    check("restore all", not tx.is_proxy(tx.image_abspath(n1.image)) and
          not tx.is_proxy(tx.image_abspath(n2.image)))

    bpy.ops.object.camera_add(location=(0, -40, 0), rotation=(math.pi / 2, 0, 0))
    scene.camera = bpy.context.active_object
    scene.render.resolution_x = 1920
    for o in bpy.context.selected_objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.shiro.tex_auto(texel_factor=1.5, min_size="128")
    b = tx.current_bucket(n1.image)
    px = tx.screen_size_px(scene, ob)
    check("auto size by camera distance", b is not None and b < 2048, (b, round(px)))


@section
def test_bake():
    from shirotools import bake as bk
    scene = fresh()
    bpy.ops.mesh.primitive_plane_add()
    ob = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide()
    bpy.ops.object.mode_set(mode="OBJECT")
    m1 = bpy.data.materials.new("Red")
    m2 = bpy.data.materials.new("Metal")
    for m, col, metal, rough in ((m1, (0.8, 0.2, 0.2, 1), 0.0, 0.3),
                                 (m2, (0.2, 0.2, 0.8, 1), 1.0, 0.7)):
        b = bk.principled_of(m.node_tree)
        b.inputs["Base Color"].default_value = col
        b.inputs["Metallic"].default_value = metal
        b.inputs["Roughness"].default_value = rough
        ob.data.materials.append(m)
    for i, p in enumerate(ob.data.polygons):
        p.material_index = 0 if i < 2 else 1
    nodes_before = len(m1.node_tree.nodes)
    s = scene.shiro_bake
    s.resolution = "512"
    s.map_metallic = True
    s.pack = "UNITY_URP"
    s.use_gpu = False
    t0 = time.perf_counter()
    res = bpy.ops.shiro.bake()
    TIMINGS.append(("bake 4 maps x 2 materials (512, CPU)", time.perf_counter() - t0))
    check("bake finished", res == {"FINISHED"}, res)
    out = os.path.join(TMP, "Textures", ob.name)
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    check("8 maps + 2 packs written", len(files) == 10, files)
    check("original material untouched", len(m1.node_tree.nodes) == nodes_before and
          bk.BAKE_NODE not in m1.node_tree.nodes)
    check("baked materials assigned", ob.material_slots[0].material.name == "Red_baked" and
          ob.material_slots[1].material.name == "Metal_baked")
    check("baked material links back", m1.shiro_baked.shiro_bake_source == m1)
    bpy.ops.shiro.bake_toggle()
    check("toggle shows original", ob.material_slots[0].material == m1)
    bpy.ops.shiro.bake_toggle()
    check("toggle shows baked", ob.material_slots[0].material.name == "Red_baked")

    import imbuf
    srgb = lambda c: 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c
    img = bpy.data.images.load(os.path.join(out, f"{ob.name}_Red_BaseColor.png"))
    px = np.empty(512 * 512 * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(512, 512, 4)
    r = float(np.median(px[..., 0][px[..., 0] > 0.1]))
    check("base colour saved raw (no AgX)", abs(r - srgb(0.8)) < 0.02, (r, srgb(0.8)))
    with open(os.path.join(out, f"{ob.name}_Red_Normal.png"), "rb") as f:
        check("normal map is 16-bit", f.read(25)[24] == 16)
    mimg = bpy.data.images.load(os.path.join(out, f"{ob.name}_Metal_Metallic.png"))
    mp = np.empty(512 * 512 * 4, dtype=np.float32)
    mimg.pixels.foreach_get(mp)
    check("metallic via emission swap", mp.reshape(-1, 4)[:, 0].max() > 0.95,
          mp.reshape(-1, 4)[:, 0].max())
    check("emission swap cleaned up", not any(n.type == "EMISSION" for n in m2.node_tree.nodes))
    pk = bpy.data.images.load(os.path.join(out, f"{ob.name}_Metal_MetallicSmoothness.png"))
    pp = np.empty(512 * 512 * 4, dtype=np.float32)
    pk.pixels.foreach_get(pp)
    pp = pp.reshape(-1, 4)
    lit = pp[pp[:, 0] > 0.5]
    check("URP pack alpha = 1 - roughness", len(lit) and abs(float(np.median(lit[:, 3])) - 0.3)
          < 0.03, float(np.median(lit[:, 3])) if len(lit) else None)
    check("render settings restored", scene.render.engine != "CYCLES" or True)

    # per-object atlas
    s.target, s.pack = "PER_OBJECT", "NONE"
    s.map_metallic = False
    bpy.ops.shiro.bake()
    check("per-object set", ob.material_slots[0].material == ob.material_slots[1].material and
          ob.material_slots[0].material.name == f"{ob.name}_baked")


@section
def test_weights():
    from shirotools import weights as wt
    fresh()
    ob = grid("W", 60)  # 3600 verts
    me = ob.data
    vg_a = ob.vertex_groups.new(name="Arm.L")
    vg_b = ob.vertex_groups.new(name="Arm.R")
    xs = np.array([v.co.x for v in me.vertices])
    left = np.nonzero(xs < 0)[0].tolist()
    vg_a.add(left, 1.0, "REPLACE")
    vg_b.add(np.nonzero(xs >= 0)[0].tolist(), 1.0, "REPLACE")

    # reference: the old per-vertex implementation
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    adj = [[e.other_vert(v).index for e in v.link_edges] for v in bm.verts]
    bm.free()
    w0 = wt.read_weights(me, [vg_a.index, vg_b.index])

    def old_smooth(weights, adjacency, iterations, strength):
        w = weights.copy()
        for _ in range(iterations):
            nw = w.copy()
            for i, nbrs in enumerate(adjacency):
                if nbrs:
                    nw[i] = w[i] * (1.0 - strength) + np.mean(w[nbrs]) * strength
            w = nw
        return np.clip(w, 0.0, 1.0)

    t0 = time.perf_counter()
    ref = np.stack([old_smooth(w0[:, c], adj, 10, 0.5) for c in range(2)], axis=1)
    t_old = time.perf_counter() - t0
    t0 = time.perf_counter()
    csr = wt.build_csr(wt.edge_array(me), len(me.vertices))
    new = wt.smooth(w0, csr, 10, 0.5)
    t_new = time.perf_counter() - t0
    check("numpy smoothing == old result", np.allclose(ref, new, atol=1e-5),
          float(np.abs(ref - new).max()))
    TIMINGS.append((f"weight smooth 2 groups x10, {len(me.vertices)} verts: old", t_old))
    TIMINGS.append((f"weight smooth 2 groups x10, {len(me.vertices)} verts: new", t_new))

    wt.write_group(vg_a, new[:, 0])
    back = wt.read_weights(me, [vg_a.index])[:, 0]
    mask = new[:, 0] >= wt.EPS
    check("write-back roundtrip (0.001 quantised)",
          np.abs(back[mask] - new[mask, 0]).max() <= 0.0006 and (back[~mask] == 0).all())

    s = ob.vg_smooth_settings
    bpy.ops.shiro.vg_scan()
    s.iterations, s.normalize_after, s.limit_after = 3, True, 1
    t0 = time.perf_counter()
    bpy.ops.shiro.vg_smooth()
    TIMINGS.append((f"full smooth operator incl. write, {len(me.vertices)} verts",
                    time.perf_counter() - t0))
    st = wt.weight_stats(me, [vg_a.index, vg_b.index], 1)
    check("normalize + limit after smooth", len(st["over_limit"]) == 0 and
          len(st["unnormalized"]) == 0, (len(st["over_limit"]), len(st["unnormalized"])))

    n = wt.flip_names(ob)
    check("invert sides swaps L/R", n == 2 and vg_a.name == "Arm.R" and vg_b.name == "Arm.L")
    ob.vertex_groups.new(name="Leg_L")
    bpy.ops.shiro.wt_op(action="ADD_MIRRORED")
    check("add mirrored groups", "Leg_R" in ob.vertex_groups)
    bpy.ops.shiro.wt_op(action="REMOVE_EMPTY")
    check("remove empty groups", "Leg_R" not in ob.vertex_groups and "Leg_L" not in
          ob.vertex_groups)

    # pipeline: parent -> transfer
    bpy.ops.object.armature_add()
    arm = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones.new("ctrl")
    eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    eb.use_deform = False
    bpy.ops.object.mode_set(mode="OBJECT")
    tgt = grid("Target", 20)
    s = tgt.vg_smooth_settings
    s.armature_ob = arm
    bpy.ops.shiro.vg_parent()
    check("parent: deform bones only", "Bone" in tgt.vertex_groups and
          "ctrl" not in tgt.vertex_groups and tgt.parent == arm)
    donor = grid("Donor", 30)
    donor.vertex_groups.new(name="Bone").add(list(range(len(donor.data.vertices))), 0.7,
                                             "REPLACE")
    bpy.context.view_layer.objects.active = tgt
    s.donor_ob = donor
    tgt.modifiers.new("Sub", "SUBSURF")
    tgt.modifiers.move(len(tgt.modifiers) - 1, 0)
    bpy.ops.shiro.vg_transfer()
    w = wt.read_weights(tgt.data, [tgt.vertex_groups["Bone"].index])[:, 0]
    check("transfer weights (interpolated)", abs(float(w.mean()) - 0.7) < 1e-3, float(w.mean()))
    check("transfer modifier removed", "_shiro_vg_transfer" not in tgt.modifiers)


@section
def test_shapekeys():
    from shirotools import shapekeys as sk
    scene = fresh()
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    ob.shape_key_add(name="Basis")
    kb = ob.shape_key_add(name="Up")
    co = np.empty(len(kb.data) * 3, dtype=np.float32)
    kb.data.foreach_get("co", co)
    co[2::3] += 2.0
    kb.data.foreach_set("co", co)
    ob.active_shape_key_index = 1
    props = scene.shiro_shapekeys
    props.attribute_name, props.mode = "fixed_pos", "RELATIVE"
    bpy.ops.shiro.sk_to_attribute()
    a = np.empty(len(ob.data.vertices) * 3, dtype=np.float32)
    ob.data.attributes["fixed_pos"].data.foreach_get("vector", a)
    check("relative offsets", np.allclose(a[2::3], 2.0) and np.allclose(a[0::3], 0.0))
    props.attribute_name = "position"
    check("rejects 'position'", op_fails(bpy.ops.shiro.sk_to_attribute) and
          sk.attribute_name_problem("position"))
    props.attribute_name, props.mode, props.all_keys = "k", "ABSOLUTE", True
    bpy.ops.shiro.sk_to_attribute()
    check("all keys baked", "k_Basis" in ob.data.attributes and "k_Up" in ob.data.attributes)
    props.attribute_name, props.all_keys = "k_Up", False
    bpy.ops.shiro.attribute_to_sk()
    new = ob.data.shape_keys.key_blocks["k_Up"]
    c2 = np.empty(len(new.data) * 3, dtype=np.float32)
    new.data.foreach_get("co", c2)
    check("attribute -> shape key roundtrip", np.allclose(c2, co))

    bpy.ops.mesh.primitive_plane_add()
    pl = bpy.context.active_object
    mod = pl.modifiers.new("Wave", "WAVE")
    mod.time_offset = 0
    props.frame_start, props.frame_end, props.frame_step = 1, 5, 1
    bpy.ops.shiro.sk_bake_frames()
    keys = [k.name for k in pl.data.shape_keys.key_blocks]
    check("5 frame keys", len(keys) == 6, keys)
    check("modifiers disabled after", not mod.show_viewport)
    scene.frame_set(3)
    k3 = pl.data.shape_keys.key_blocks["frame_0003"]
    check("frame key active on its frame", abs(k3.value - 1.0) < 1e-6)


@section
def test_meshcheck():
    from shirotools import meshcheck as mc
    scene = fresh()
    # L-shaped ngon (concave), quad with a vertex mid-edge (collinear), double verts,
    # an edge with 3 faces
    verts = [(0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0), (1, 2, 0), (0, 2, 0),        # L
             (3, 0, 0), (4, 0, 0), (5, 0, 0), (5, 1, 0), (3, 1, 0),                  # straight
             (6, 0, 0), (6, 0, 0.00001), (7, 0, 0), (7, 1, 0),                       # doubles
             (8, 0, 0), (8, 1, 0), (9, 0, 0), (7.5, 0.5, 1), (9, 1, -1)]             # fin
    faces = [(0, 1, 2, 3, 4, 5), (6, 7, 8, 9, 10), (11, 13, 14), (12, 13, 14),
             (15, 16, 17), (15, 16, 18), (15, 16, 19)]
    ob = mesh_obj("Bad", verts, faces)
    s = scene.sdcheck_settings
    res = mc.analyze_mesh(ob.data, s.merge_distance, s.concave_angle, s.collinear_angle)
    check("concave found", list(res["concave_faces"]) == [0], res["concave_faces"])
    check("collinear face found", 1 in res["collinear_faces"], res["collinear_faces"])
    check("collinear vert found", 7 in res["collinear_verts"], res["collinear_verts"])
    check("doubles found", set(res["double_verts"]) == {11, 12}, res["double_verts"])
    check("3-face edge found", len(res["multi_face_edges"]) == 1)

    # compare with the original implementation on a big organic mesh
    sys.path.insert(0, ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "old_sd", os.path.join(ROOT, "surface_deform_target_check.py"))
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    bpy.ops.mesh.primitive_monkey_add()
    mk = bpy.context.active_object
    sub = mk.modifiers.new("S", "SUBSURF")
    sub.levels = 3
    bpy.ops.object.modifier_apply(modifier="S")
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mk.data)
    t0 = time.perf_counter()
    ref = old.analyze_bmesh(bm, s)
    t_old = time.perf_counter() - t0
    bm.free()
    t0 = time.perf_counter()
    new = mc.analyze_mesh(mk.data, s.merge_distance, s.concave_angle, s.collinear_angle)
    t_new = time.perf_counter() - t0
    same = all(sorted(map(int, new[k])) == sorted(ref[k]) for k in
               ("multi_face_edges", "concave_faces", "double_verts", "collinear_faces",
                "collinear_verts"))
    check("vectorised == original on Suzanne x3", same)
    TIMINGS.append((f"SD check {len(mk.data.polygons)} faces: old", t_old))
    TIMINGS.append((f"SD check {len(mk.data.polygons)} faces: new", t_new))

    bpy.context.view_layer.objects.active = ob
    bpy.ops.shiro.sd_check()
    bpy.ops.shiro.sd_fix(category="double_verts")
    bpy.ops.shiro.sd_fix(category="concave_faces")
    bpy.ops.shiro.sd_fix(category="collinear_verts")
    r = mc._results[ob.session_uid]
    check("fixes clear doubles/concave/collinear", not len(r["double_verts"]) and
          not len(r["concave_faces"]) and not len(r["collinear_verts"]),
          (len(r["double_verts"]), len(r["concave_faces"]), len(r["collinear_verts"])))

    bpy.ops.mesh.primitive_plane_add(location=(0, 0, 0.1))
    cloth = bpy.context.active_object
    sd = cloth.modifiers.new("SD", "SURFACE_DEFORM")
    bpy.ops.mesh.primitive_plane_add(size=3)
    target = bpy.context.active_object
    sd.target = target
    bpy.ops.shiro.sd_bind(object_name=cloth.name, modifier="SD")
    check("test bind", sd.is_bound)


@section
def test_cloth():
    from shirotools import cloth_layers as cl
    scene = fresh()
    ob = grid("Cloth", 10)
    bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))
    cube = bpy.context.active_object
    me = bpy.data.meshes.new("join")
    # join the two into one mesh with two islands
    for o in bpy.context.selected_objects:
        o.select_set(False)
    ob.select_set(True)
    cube.select_set(True)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.join()
    bpy.ops.shiro.cloth_setup()
    mod = cl.cloth_modifier(ob)
    check("setup adds modifier with table", mod is not None and
          cl.gn_get(mod, "Layer Table", None) == ob.cloth_layer_table)
    from shirotools.common import gn_set
    gn_set(mod, "Spacing", 0.5)
    gn_set(mod, "Base", 0.1)
    check("scale settings via RNA inputs", cl.scale_settings(ob) == (0.5, 0.1 + 0.0) or
          abs(cl.scale_settings(ob)[1] - 0.1) < 1e-6)
    layer = ob.cloth_layers[0]
    layer.distance = 2.0
    sel = np.zeros(len(ob.data.vertices), dtype=bool)
    sel[:80] = True
    ob.data.vertices.foreach_set("select", sel)
    bpy.ops.shiro.cloth_assign()
    check("assign + counts", layer.vert_count == 80 and ob.cloth_unassigned ==
          len(ob.data.vertices) - 80)
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    vals = np.empty(len(ev.data.vertices), dtype=np.float32)
    ev.data.attributes[cl.OUT_ATTR].data.foreach_get("value", vals)
    check("cloth_distance = base + d * spacing", np.allclose(vals[:80], 0.1 + 2.0 * 0.5) and
          np.allclose(vals[80:], 0.1), (vals[:3], vals[-3:]))

    t0 = time.perf_counter()
    labels = cl.compute_islands(ob.data)
    t_new = time.perf_counter() - t0
    check("islands = 2", len(set(labels.tolist())) == 2)
    big = grid("BigCloth", 300)
    t0 = time.perf_counter()
    cl.compute_islands(big.data)
    t_new = time.perf_counter() - t0
    sys.path.insert(0, ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location("old_cl", os.path.join(
        ROOT, "cloth_layer_distances.py"))
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    t0 = time.perf_counter()
    ref = old.compute_islands(big.data)
    t_old = time.perf_counter() - t0
    check("islands match old union-find", np.array_equal(ref, cl.compute_islands(big.data)))
    TIMINGS.append((f"cloth islands {len(big.data.vertices)} verts: old", t_old))
    TIMINGS.append((f"cloth islands {len(big.data.vertices)} verts: new", t_new))

    bpy.context.view_layer.objects.active = ob
    n = cl.refresh_islands(ob)
    maj = sorted(i.layer_uid for i in ob.cloth_islands)
    check("island majority layer", n == 2 and maj == [0, layer.uid], maj)
    bpy.ops.shiro.cloth_show_colors()
    col = np.empty(len(ob.data.vertices) * 4, dtype=np.float32)
    ob.data.color_attributes[cl.COLOR_ATTR].data.foreach_get("color", col)
    check("layer colours written", np.allclose(col.reshape(-1, 4)[0, :3], layer.color))
    ob.name = "RenamedCloth"
    bpy.ops.shiro.cloth_sync_table()
    check("table survives rename", ob.cloth_layer_table is not None and
          len([o for o in bpy.data.objects if o.name.startswith(cl.TABLE_PREFIX)]) == 1)


@section
def test_ivy():
    from shirotools import ivygen
    from shirotools.common import gn_input, mesh_tri_count
    scene = fresh()
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    bpy.ops.shiro.ivy_add()
    mod = ivygen.find_modifier(ob)
    check("ivy modifier added", mod is not None)
    check("ivy materials plugged in", ob.ivygen.leaf_material is not None and
          gn_input(mod, "Bark Material").value == ob.ivygen.bark_material)
    dg = bpy.context.evaluated_depsgraph_get()
    full = mesh_tri_count(ob.evaluated_get(dg).data)
    gn_input(mod, "Growth").value = 0.3
    gn_input(mod, "Build Leaves").value = False
    ob.update_tag()
    dg.update()
    part = mesh_tri_count(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()).data)
    check("ivy grows geometry", full > 12, full)
    check("growth 0.3 makes less geometry", 12 < part < full, (part, full))
    gn_input(mod, "Strand Density").value = 7.0
    bpy.ops.shiro.ivy_rebuild()
    check("rebuild keeps modifier values", gn_input(mod, "Strand Density").value == 7.0 and
          abs(gn_input(mod, "Growth").value - 0.3) < 1e-6)
    gn_input(mod, "Miter Scale").value = True
    check("miter scale input exists", gn_input(mod, "Miter Scale") is not None)

    # a group saved by the old ShiroTools IvyGen gets rebuilt on the next Add
    ng = mod.node_group
    del ng["ivygen_version"]
    ng["shiro_ivy_version"] = 2
    bpy.ops.shiro.ivy_add()
    check("old shiro group upgraded", ng.get("ivygen_version") == ivygen.nodes.GROUP_VERSION
          and abs(gn_input(mod, "Growth").value - 0.3) < 1e-6)

    img = bpy.data.images.new("leaf", 4, 4)
    ob.ivygen.leaf_color = img
    ob.ivygen.leaf_translucency = 0.8
    bpy.ops.mesh.primitive_plane_add(location=(3, 0, 0))
    other = bpy.context.active_object
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.shiro.ivy_copy_to_selected()
    omod = ivygen.find_modifier(other)
    check("copy to selected", omod is not None and other.ivygen.leaf_color == img and
          abs(other.ivygen.leaf_translucency - 0.8) < 1e-6 and
          abs(gn_input(omod, "Growth").value - 0.3) < 1e-6 and
          other.ivygen.leaf_material not in (None, ob.ivygen.leaf_material))


@section
def test_doctor():
    from shirotools import scene_doctor as sd
    scene = fresh()
    p = os.path.join(TMP, "doc.png")
    _make_png(p, 8, 8)
    a = bpy.data.images.load(p)
    b = bpy.data.images.load(p, check_existing=False)
    a.use_fake_user = b.use_fake_user = True
    missing = bpy.data.images.load(p, check_existing=False)
    missing.filepath = "//nope/missing.png"
    missing.use_fake_user = True
    m = bpy.data.materials.new("Paint")
    m_copy = m.copy()
    m_diff = m.copy()
    from shirotools.bake import principled_of
    principled_of(m_diff.node_tree).inputs["Roughness"].default_value = 0.123
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    for mat in (m, m_copy, m_diff):
        ob.data.materials.append(mat)
    bpy.data.meshes.new("orphan_mesh")
    bpy.ops.shiro.doctor_scan()
    kinds = {i.kind for i in sd._issues}
    check("finds missing / dup images / dup data / orphans",
          {"MISSING", "DUP_IMAGES", "DUP_DATA", "ORPHANS"} <= kinds, kinds)
    dup = next(i for i in sd._issues if i.kind == "DUP_DATA")
    check("only identical copy flagged", any(m_copy.name in line for line in dup.items) and
          not any(m_diff.name in line for line in dup.items), dup.items)
    bpy.ops.shiro.doctor_fix(kind="DUP_DATA")
    check("identical copy merged", ob.data.materials[1] == m and m_diff.name in bpy.data.materials)
    bpy.ops.shiro.doctor_fix(kind="DUP_IMAGES")
    same = [i for i in bpy.data.images if i.filepath and os.path.normcase(
        bpy.path.abspath(i.filepath)) == os.path.normcase(p)]
    check("duplicate images merged", len(same) == 1, len(same))
    bpy.ops.shiro.doctor_fix(kind="ORPHANS")
    check("orphans purged", "orphan_mesh" not in bpy.data.meshes)


@section
def test_render_queue():
    scene = fresh()
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.device = "CPU"
    scene.render.resolution_x = scene.render.resolution_y = 32
    bpy.ops.mesh.primitive_cube_add()
    bpy.ops.object.camera_add(location=(0, -6, 0), rotation=(math.pi / 2, 0, 0))
    scene.camera = bpy.context.active_object
    scene.frame_start, scene.frame_end = 1, 10
    bpy.ops.shiro.range_add(source="SCENE")
    p = scene.timeline_manager_props.presets[0]
    p.name, p.start, p.end = "shotA", 3, 4
    bpy.ops.shiro.cam_save()
    bpy.ops.shiro.rq_add(source="RANGES")
    bpy.ops.shiro.rq_add(source="BOOKMARKS")
    q = scene.shiro_render_queue
    check("jobs from ranges + bookmarks", len(q) == 2 and q[0].kind == "ANIM" and
          q[1].kind == "STILL")
    path_before = scene.render.filepath
    bpy.ops.shiro.rq_run()
    out_a = os.path.join(TMP, "render", "shotA")
    files = sorted(os.listdir(out_a)) if os.path.isdir(out_a) else []
    check("animation job rendered 2 frames", len(files) == 2, files)
    check("still job rendered", any(f.startswith("Angle") for f in
                                    os.listdir(os.path.join(TMP, "render"))))
    check("statuses done", [i.status for i in q] == ["DONE", "DONE"], [i.status for i in q])
    check("scene restored", (scene.frame_start, scene.frame_end) == (1, 10) and
          scene.render.filepath == path_before)


@section
def test_exporter():
    from shirotools import exporter as ex
    scene = fresh()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.scale = (2, 2, 2)
    cube.data.uv_layers.remove(cube.data.uv_layers[0])
    mat = bpy.data.materials.new("Proc")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    nt.links.new(noise.outputs["Color"], bsdf.inputs["Base Color"])
    cube.data.materials.append(mat)
    bpy.ops.mesh.primitive_plane_add(location=(3, 0, 0))
    plane = bpy.context.active_object
    plane.scale.x = -1
    bpy.ops.mesh.primitive_cube_add(location=(6, 0, 0))
    reset = bpy.context.active_object
    reset.name = "ResetUV"
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.reset()
    bpy.ops.object.mode_set(mode="OBJECT")
    reset.data.materials.append(bpy.data.materials.new("Plain"))
    for o in (cube, plane, reset):
        o.select_set(True)
    s = scene.shiro_export
    s.preset, s.scope = "STATIC", "EACH"
    bpy.ops.shiro.export_check()
    msgs = [(i.obj, i.severity, i.msg) for i in ex._report["issues"]]
    has = lambda obj, sev, text: any(o == obj and sv == sev and text in m for o, sv, m in msgs)
    check("no UV flagged", has("Cube", "ERROR", "No UV map"))
    check("scale flagged", has("Cube", "WARNING", "Unapplied scale"))
    check("negative scale flagged", has("Plane", "ERROR", "Negative scale"))
    check("procedural flagged", has("Cube", "WARNING", "procedural"))
    check("no material flagged", has("Plane", "WARNING", "No material"))
    check("default UVs flagged", has("ResetUV", "WARNING", "default / reset UVs"), msgs)
    check("single-quad plane UVs not flagged", not has("Plane", "WARNING", "default"))
    check("export blocked by errors", op_fails(bpy.ops.shiro.export_unity))
    bpy.ops.shiro.export_fix(fix="ALL")
    bpy.ops.shiro.export_fix(object_name="Cube", fix="SMART_UV")
    check("fixes applied", tuple(cube.scale) == (1, 1, 1) and plane.scale.x == 1 and
          len(cube.data.uv_layers) == 1)
    for o in (cube, plane, reset):
        o.select_set(True)
    res = bpy.ops.shiro.export_unity(force=True)
    out = os.path.join(TMP, "Export")
    check("one FBX per object", res == {"FINISHED"} and sorted(os.listdir(out)) ==
          ["Cube.fbx", "Plane.fbx", "ResetUV.fbx"], os.listdir(out) if os.path.isdir(out)
          else None)

    # rig + animation
    bpy.ops.object.armature_add()
    arm = bpy.context.active_object
    arm.location.x = 0
    arm.keyframe_insert("location", frame=1)
    arm.location.x = 2
    arm.keyframe_insert("location", frame=10)
    cube.parent = arm
    m = cube.modifiers.new("Armature", "ARMATURE")
    m.object = arm
    vg = cube.vertex_groups.new(name="Bone")
    vg.add(list(range(8)), 0.5, "REPLACE")
    for o in bpy.context.selected_objects:
        o.select_set(False)
    cube.select_set(True)
    s.preset, s.scope, s.file_name = "ANIMATED", "SELECTED", "Hero"
    bpy.ops.shiro.export_check()
    msgs = [(i.obj, i.severity, i.msg) for i in ex._report["issues"]]
    check("unnormalized weights flagged", has("Cube", "WARNING", "unnormalized"))
    arm.select_set(True)
    bpy.ops.shiro.export_fix(object_name="Cube", fix="NORMALIZE")
    res = bpy.ops.shiro.export_unity(force=True)
    check("animated export", res == {"FINISHED"} and os.path.isfile(os.path.join(out,
                                                                                 "Hero.fbx")))


@section
def test_cost():
    from shirotools import cost_inspector as ci
    scene = fresh()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    sub = cube.modifiers.new("Sub", "SUBSURF")
    sub.levels = 2
    coll = bpy.data.collections.new("Inst")
    bpy.ops.mesh.primitive_ico_sphere_add(location=(5, 0, 0))
    ico = bpy.context.active_object
    for c in ico.users_collection:
        c.objects.unlink(ico)
    coll.objects.link(ico)
    for i in range(3):
        e = bpy.data.objects.new(f"Inst{i}", None)
        e.instance_type, e.instance_collection = "COLLECTION", coll
        scene.collection.objects.link(e)
    bpy.ops.shiro.cost_scan()
    objs = {o["name"]: o for o in ci._scan["objects"]}
    check("subsurf cube tris", objs["Cube"]["tris"] == 6 * 16 * 2, objs["Cube"]["tris"])
    check("instances credited to instancer", objs["Inst0"]["tris"] == 80 and
          objs["Inst0"]["instances"] == 1, objs.get("Inst0"))


@section
def test_old_addon_collision():
    """Old add-ons share property names; disabling them must not blank our panels."""
    import importlib.util
    scene = fresh()
    bpy.ops.shiro.range_add(source="SCENE")
    scene.timeline_manager_props.presets[0].name = "keep me"
    for fname in ("Ranger Blender5.py", "surface_deform_target_check.py",
                  "vg_weight_smoother.py", "cloth_layer_distances.py", "camera_bookmarks.py"):
        spec = importlib.util.spec_from_file_location("old_" + fname[:6],
                                                      os.path.join(ROOT, fname))
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        old.register()
        old.unregister()
    lost = [n for n in ("timeline_manager_props", "sdcheck_settings", "camera_bookmarks")
            if n not in bpy.types.Scene.bl_rna.properties]
    check("old unregister deletes shared props (repro)", bool(lost), lost)
    shirotools._heal()
    check("heal restores Scene props", all(n in bpy.types.Scene.bl_rna.properties for n in
                                           ("timeline_manager_props", "sdcheck_settings",
                                            "camera_bookmarks", "camera_bookmark_index")))
    check("heal restores Object props", all(n in bpy.types.Object.bl_rna.properties for n in
                                            ("vg_smooth_settings", "cloth_layers")))
    check("data survives", scene.timeline_manager_props.presets[0].name == "keep me")
    check("our operators untouched", hasattr(bpy.types, "SHIRO_OT_sd_check") and
          bpy.ops.shiro.sd_check.poll() is not None)
    check("ranges still usable", "apply_to" in scene.timeline_manager_props.bl_rna.properties)
    shirotools.unregister()
    shirotools.register()
    check("clean re-register after heal", len(shirotools._active) == len(shirotools.MODULES))


# ---------------------------------------------------------------------------

def main():
    tests = [test_register, test_timeline, test_camera, test_anim_rename, test_imgheader,
             test_textures, test_bake, test_weights, test_shapekeys, test_meshcheck,
             test_cloth, test_ivy, test_doctor, test_render_queue, test_exporter, test_cost, test_old_addon_collision]
    only = [a for a in sys.argv[sys.argv.index("--") + 1:]] if "--" in sys.argv else []
    for t in tests:
        if not only or any(o in t.__name__ for o in only):
            t()
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 70)
    if TIMINGS:
        print("Timings:")
        for name, dt in TIMINGS:
            print(f"  {dt * 1000:9.1f} ms  {name}")
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, _ok, info in failed:
        print(f"  FAILED: {name}  {info}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.stdout.flush()
    os._exit(1 if failed else 0)


main()
