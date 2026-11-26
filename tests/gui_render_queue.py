"""GUI test of the non-blocking Render Queue path (render window + handlers).

  blender --factory-startup --python tests/gui_render_queue.py -- <out_dir>
Prints RQ_RESULT ok/fail and quits.
"""

import math
import os
import sys
import time

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = sys.argv[sys.argv.index("--") + 1]
import shirotools  # noqa: E402

shirotools.register()
START = time.time()


def setup():
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "rq.blend"))
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.device = "CPU"
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.frame_start, scene.frame_end = 1, 30
    for name, s, e in (("jobA", 1, 2), ("jobB", 5, 6)):
        bpy.ops.shiro.range_add(source="SCENE")
        p = scene.timeline_manager_props.presets[-1]
        p.name, p.start, p.end = name, s, e
    bpy.ops.shiro.rq_add(source="RANGES")
    bpy.ops.shiro.rq_add(source="STILL")
    win = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=win):
        bpy.ops.shiro.rq_run()
    bpy.app.timers.register(poll, first_interval=1.0)
    return None


def poll():
    from shirotools.render_queue import _Runner
    scene = bpy.data.scenes[0]
    statuses = [i.status for i in scene.shiro_render_queue]
    if _Runner.active and time.time() - START < 120:
        return 0.5
    files = []
    for root, _d, fs in os.walk(os.path.join(OUT, "render")):
        files += fs
    ok = statuses == ["DONE", "DONE", "DONE"] and len(files) == 5 and \
        (scene.frame_start, scene.frame_end) == (1, 30)
    print("RQ_RESULT", "ok" if ok else "fail", statuses, sorted(files),
          (scene.frame_start, scene.frame_end))
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(setup, first_interval=1.5)
