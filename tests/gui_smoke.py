"""GUI smoke test: opens Blender with every ShiroTools panel expanded, draws the
Timeline overlay and the sidebars, saves screenshots, then quits.

  blender --factory-startup --python tests/gui_smoke.py -- <out_dir>
Any Python error raised while drawing is printed as a traceback (grep it).
"""

import importlib
import math
import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else os.path.join(ROOT, "shots")
os.makedirs(OUT, exist_ok=True)

import shirotools  # noqa: E402

# open every panel so every draw() runs
for name, _l, _d in shirotools.MODULES:
    mod = importlib.import_module(f"shirotools.{name}")
    for cls in getattr(mod, "classes", ()):
        if issubclass(cls, bpy.types.Panel) and "DEFAULT_CLOSED" in getattr(cls, "bl_options",
                                                                            set()):
            cls.bl_options = set(cls.bl_options) - {"DEFAULT_CLOSED"}
shirotools.register()


def demo_scene():
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 240
    st = scene.shiro_timeline
    for name, f in (("Intro", 1), ("sh010_wide", 24), ("sh020_close", 72), ("key_jump", 110),
                    ("sh030_reverse", 150), ("Outro", 210)):
        scene.timeline_markers.new(name, frame=f)
    cam = scene.camera
    scene.timeline_markers["sh010_wide"].camera = cam
    bpy.ops.shiro.tag_add(defaults=True)
    for start, end, name in ((1, 71, "Shot A"), (72, 149, "Shot B"), (150, 240, "Shot C"),
                             (100, 130, "Jump")):
        bpy.ops.shiro.range_add(source="SCENE")
        p = scene.timeline_manager_props.presets[-1]
        p.name, p.start, p.end = name, start, end
    scene.timeline_manager_props.active_index = 1
    st.bpm = 128
    st.show_beat_grid = True
    bpy.ops.shiro.cam_save()
    cube = bpy.data.objects.get("Cube")
    if cube:
        bpy.context.view_layer.objects.active = cube
        cube.select_set(True)
        cube.vertex_groups.new(name="Arm.L")
        cube.vertex_groups.new(name="Arm.R")
        bpy.ops.shiro.cost_scan()
        bpy.ops.shiro.doctor_scan()
        bpy.ops.shiro.export_check()
        bpy.ops.shiro.tex_scan()
        bpy.ops.shiro.sd_check()
        bpy.ops.shiro.wt_check()
        bpy.ops.shiro.ivy_add()


def big_area(kind):
    areas = [a for a in bpy.context.window.screen.areas if a.type == kind]
    return max(areas, key=lambda a: a.width * a.height) if areas else None


def window_region(area, kind="WINDOW"):
    return next(r for r in area.regions if r.type == kind)


def shot(area, name):
    path = os.path.join(OUT, name + ".png")
    with bpy.context.temp_override(window=bpy.context.window, area=area,
                                   region=window_region(area)):
        bpy.ops.screen.screenshot_area(filepath=path)
    print("SHOT", path)


STATE = {"step": 0}


def set_tab(area, tab="Shiro"):
    region = window_region(area, "UI")
    try:
        region.active_panel_category = tab
    except (AttributeError, TypeError) as err:
        print("TAB", err)


def step():
    s = STATE["step"]
    STATE["step"] += 1
    win = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=win):
        main = big_area("VIEW_3D") or big_area("DOPESHEET_EDITOR")
        if s == 0:
            demo_scene()
            main.ui_type = "TIMELINE"
            main.spaces.active.show_region_ui = True
            return 1.0
        if s == 1:
            set_tab(main)
            with bpy.context.temp_override(window=win, area=main, region=window_region(main)):
                bpy.ops.action.view_all()
            return 1.0
        if s == 2:
            shot(main, "timeline")
            main.ui_type = "DOPESHEET"
            return 1.0
        if s == 3:
            set_tab(main)
            return 1.0
        if s == 4:
            shot(main, "dopesheet")
            main.ui_type = "VIEW_3D"
            main.spaces.active.show_region_ui = True
            return 1.0
        if s == 5:
            set_tab(main)
            return 1.0
        if 6 <= s <= 14:
            shot(main, f"view3d_{s - 6}")
            with bpy.context.temp_override(window=win, area=main, region=window_region(main, "UI")):
                for _ in range(12):
                    bpy.ops.view2d.scroll_down()
            return 0.6
        print("GUI_SMOKE_DONE")
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(step, first_interval=2.0)
