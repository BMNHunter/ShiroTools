"""Batch Render Queue.

Queue animation ranges and stills (from Timeline range presets, Camera
Bookmarks or marker camera cuts) and render them one after another. In the UI
it runs non-blocking: each job is started with the normal render window and
the next one begins when ``render_complete`` fires. Esc / cancel stops the queue.
Scene settings (range, frame, camera, output path) are restored afterwards.

Output tokens: {name} {camera} {scene} {blend}
"""

import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .common import TAB

STATUS_ICON = {"": "BLANK1", "QUEUED": "TIME", "RENDERING": "RENDER_ANIMATION",
               "DONE": "CHECKMARK", "FAILED": "CANCEL", "CANCELLED": "X"}


class SHIRO_PG_rq_item(PropertyGroup):
    name: StringProperty(name="Name", default="Job")
    enabled: BoolProperty(name="Render", default=True)
    kind: EnumProperty(name="Type", items=[("ANIM", "Animation", "", "RENDER_ANIMATION", 0),
                                           ("STILL", "Still", "", "RENDER_STILL", 1)],
                       default="ANIM")
    start: IntProperty(name="Start", default=1)
    end: IntProperty(name="End", default=250)
    frame: IntProperty(name="Frame", default=1)
    camera: PointerProperty(name="Camera", type=bpy.types.Object,
                            poll=lambda s, o: o.type == "CAMERA")
    bookmark: StringProperty(name="Bookmark",
                             description="Camera bookmark applied before rendering")
    output: StringProperty(name="Output", subtype="FILE_PATH",
                           default="//render/{name}/{name}_")
    status: StringProperty(default="")


def expand_path(item, scene):
    blend = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "untitled"
    cam = item.camera.name if item.camera else (scene.camera.name if scene.camera else "camera")
    out = item.output
    for key, val in (("{name}", item.name), ("{camera}", cam), ("{scene}", scene.name),
                     ("{blend}", blend)):
        out = out.replace(key, bpy.path.clean_name(val))
    return out


class _Runner:
    active = False
    queue = []
    current = None
    scene_name = ""
    saved = {}

    @classmethod
    def scene(cls):
        return bpy.data.scenes.get(cls.scene_name)

    @classmethod
    def start(cls, scene, indices):
        cls.scene_name = scene.name
        r = scene.render
        cls.saved = dict(start=scene.frame_start, end=scene.frame_end,
                         frame=scene.frame_current, camera=scene.camera, path=r.filepath)
        cls.queue = list(indices)
        for i in indices:
            scene.shiro_render_queue[i].status = "QUEUED"
        cls.active = True

    @classmethod
    def apply(cls, scene, item):
        if item.bookmark:
            bm = scene.camera_bookmarks.get(item.bookmark) if hasattr(scene,
                                                                      "camera_bookmarks") else None
            if bm is not None:
                from .camera import apply_camera, resolve_camera
                cam = item.camera or resolve_camera(bpy.context, bm)
                if cam is not None:
                    apply_camera(bm, cam)
        if item.camera is not None:
            scene.camera = item.camera
        scene.render.filepath = expand_path(item, scene)
        if item.kind == "ANIM":
            scene.frame_start, scene.frame_end = item.start, max(item.start, item.end)
        else:
            scene.frame_set(item.frame)

    @classmethod
    def finish(cls):
        scene = cls.scene()
        if scene is not None and cls.saved:
            s = cls.saved
            scene.frame_start, scene.frame_end = s["start"], s["end"]
            scene.camera = s["camera"]
            scene.render.filepath = s["path"]
            scene.frame_set(s["frame"])
            for item in scene.shiro_render_queue:
                if item.status in {"QUEUED", "RENDERING"}:
                    item.status = "CANCELLED"
        cls.active, cls.queue, cls.current, cls.saved = False, [], None, {}
        _remove_handlers()


def _item(index):
    scene = _Runner.scene()
    if scene is None or not (0 <= index < len(scene.shiro_render_queue)):
        return None, None
    return scene, scene.shiro_render_queue[index]


def run_blocking():
    """Render everything now (background mode / scripts)."""
    while _Runner.queue:
        index = _Runner.queue.pop(0)
        scene, item = _item(index)
        if item is None:
            continue
        _Runner.apply(scene, item)
        item.status = "RENDERING"
        try:
            with bpy.context.temp_override(scene=scene):
                bpy.ops.render.render(animation=item.kind == "ANIM",
                                      write_still=item.kind == "STILL", scene=scene.name)
            item.status = "DONE"
        except RuntimeError as err:
            print("[ShiroTools] render queue:", err)
            item.status = "FAILED"
    _Runner.finish()


def start_next():
    if not _Runner.queue:
        _Runner.finish()
        return None
    index = _Runner.queue.pop(0)
    scene, item = _item(index)
    if item is None:
        return 0.1
    _Runner.current = index
    _Runner.apply(scene, item)
    item.status = "RENDERING"
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm.windows else None
    try:
        with bpy.context.temp_override(window=window, scene=scene):
            res = bpy.ops.render.render("INVOKE_DEFAULT", animation=item.kind == "ANIM",
                                        write_still=item.kind == "STILL")
        if "CANCELLED" in res:
            item.status = "FAILED"
            return 0.1
    except RuntimeError as err:
        print("[ShiroTools] render queue:", err)
        item.status = "FAILED"
        return 0.1
    return None


def _on_complete(*_args):
    scene, item = _item(_Runner.current if _Runner.current is not None else -1)
    if item is not None:
        item.status = "DONE"
    _Runner.current = None
    bpy.app.timers.register(start_next, first_interval=0.5)


def _on_cancel(*_args):
    scene, item = _item(_Runner.current if _Runner.current is not None else -1)
    if item is not None:
        item.status = "CANCELLED"
    _Runner.queue = []
    bpy.app.timers.register(lambda: _Runner.finish(), first_interval=0.2)


def _add_handlers():
    _remove_handlers()
    bpy.app.handlers.render_complete.append(_on_complete)
    bpy.app.handlers.render_cancel.append(_on_cancel)


def _remove_handlers():
    for lst, fn in ((bpy.app.handlers.render_complete, _on_complete),
                    (bpy.app.handlers.render_cancel, _on_cancel)):
        while fn in lst:
            lst.remove(fn)


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

def _new(scene, **kw):
    item = scene.shiro_render_queue.add()
    for k, v in kw.items():
        setattr(item, k, v)
    scene.shiro_render_queue_index = len(scene.shiro_render_queue) - 1
    return item


class SHIRO_OT_rq_add(Operator):
    bl_idname = "shiro.rq_add"
    bl_label = "Add Jobs"
    bl_description = "Add render jobs"
    bl_options = {"REGISTER", "UNDO"}

    source: EnumProperty(items=[
        ("CURRENT", "Current Range", "One animation job with the scene range and camera"),
        ("STILL", "Current Frame", "One still of the current frame"),
        ("RANGES", "Timeline Range Presets", "One job per Shiro Timeline range preset"),
        ("BOOKMARKS", "Camera Bookmarks", "One still per camera bookmark"),
        ("MARKERS", "Marker Camera Cuts", "One job per marker with a bound camera, up to the "
                                          "next marker"),
    ])

    def execute(self, context):
        scene = context.scene
        n0 = len(scene.shiro_render_queue)
        if self.source == "CURRENT":
            _new(scene, name=scene.name, start=scene.frame_start, end=scene.frame_end,
                 camera=scene.camera)
        elif self.source == "STILL":
            _new(scene, name=f"{scene.name}_{scene.frame_current:04d}", kind="STILL",
                 frame=scene.frame_current, camera=scene.camera, output="//render/{name}")
        elif self.source == "RANGES":
            mgr = getattr(scene, "timeline_manager_props", None)
            for p in mgr.presets if mgr is not None else ():
                _new(scene, name=p.name, start=p.start, end=p.end,
                     camera=p.camera or scene.camera)
        elif self.source == "BOOKMARKS":
            for b in getattr(scene, "camera_bookmarks", ()):
                if b.kind == "CAMERA":
                    _new(scene, name=b.name, kind="STILL", frame=scene.frame_current,
                         camera=b.camera, bookmark=b.name, output="//render/{name}")
        else:
            cuts = sorted((m for m in scene.timeline_markers if m.camera), key=lambda m: m.frame)
            frames = sorted(m.frame for m in scene.timeline_markers)
            for m in cuts:
                later = [f for f in frames if f > m.frame]
                end = (later[0] - 1) if later else scene.frame_end
                _new(scene, name=m.name, start=m.frame, end=end, camera=m.camera)
        added = len(scene.shiro_render_queue) - n0
        if not added:
            self.report({"WARNING"}, "Nothing to add")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Added {added} job(s)")
        return {"FINISHED"}


class SHIRO_OT_rq_remove(Operator):
    bl_idname = "shiro.rq_remove"
    bl_label = "Remove Job"
    bl_options = {"REGISTER", "UNDO"}

    clear_all: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        scene = context.scene
        q = scene.shiro_render_queue
        if self.clear_all:
            q.clear()
        elif 0 <= scene.shiro_render_queue_index < len(q):
            q.remove(scene.shiro_render_queue_index)
        scene.shiro_render_queue_index = min(scene.shiro_render_queue_index, len(q) - 1)
        return {"FINISHED"}


class SHIRO_OT_rq_move(Operator):
    bl_idname = "shiro.rq_move"
    bl_label = "Move Job"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        scene = context.scene
        i = scene.shiro_render_queue_index
        j = i - 1 if self.direction == "UP" else i + 1
        if not (0 <= j < len(scene.shiro_render_queue)):
            return {"CANCELLED"}
        scene.shiro_render_queue.move(i, j)
        scene.shiro_render_queue_index = j
        return {"FINISHED"}


class SHIRO_OT_rq_run(Operator):
    bl_idname = "shiro.rq_run"
    bl_label = "Render Queue"
    bl_description = "Render every enabled job in order"

    only_failed: BoolProperty(default=False, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return not _Runner.active and any(i.enabled for i in context.scene.shiro_render_queue)

    def execute(self, context):
        scene = context.scene
        if not bpy.data.filepath and any(i.output.startswith("//") for i in
                                         scene.shiro_render_queue if i.enabled):
            self.report({"ERROR"}, "Save the .blend file first (outputs are relative)")
            return {"CANCELLED"}
        todo = [i for i, it in enumerate(scene.shiro_render_queue) if it.enabled and
                (not self.only_failed or it.status in {"FAILED", "CANCELLED"})]
        if not todo:
            return {"CANCELLED"}
        _Runner.start(scene, todo)
        if bpy.app.background:
            run_blocking()
        else:
            _add_handlers()
            bpy.app.timers.register(start_next, first_interval=0.1)
        return {"FINISHED"}


class SHIRO_OT_rq_stop(Operator):
    bl_idname = "shiro.rq_stop"
    bl_label = "Stop After Current"
    bl_description = "Let the current job finish, then stop the queue"

    def execute(self, context):
        _Runner.queue = []
        return {"FINISHED"}


class SHIRO_UL_rq(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        st = row.row(align=True)
        st.alert = item.status in {"FAILED", "CANCELLED"}
        st.label(text="", icon=STATUS_ICON.get(item.status, "BLANK1"))
        row.prop(item, "name", text="", emboss=False,
                 icon="RENDER_ANIMATION" if item.kind == "ANIM" else "RENDER_STILL")
        info = row.row()
        info.alignment = "RIGHT"
        info.active = False
        info.label(text=f"{item.start}–{item.end}" if item.kind == "ANIM" else f"f{item.frame}")
        if item.camera:
            row.label(text="", icon="VIEW_CAMERA")


class SHIRO_PT_render_queue(Panel):
    bl_idname = "SHIRO_PT_render_queue"
    bl_label = "Render Queue"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row()
        row.template_list("SHIRO_UL_rq", "", scene, "shiro_render_queue",
                          scene, "shiro_render_queue_index", rows=4)
        col = row.column(align=True)
        col.operator_menu_enum("shiro.rq_add", "source", text="", icon="ADD")
        col.operator("shiro.rq_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("shiro.rq_move", text="", icon="TRIA_UP").direction = "UP"
        col.operator("shiro.rq_move", text="", icon="TRIA_DOWN").direction = "DOWN"
        col.separator()
        col.operator("shiro.rq_remove", text="", icon="TRASH").clear_all = True

        q = scene.shiro_render_queue
        i = scene.shiro_render_queue_index
        if 0 <= i < len(q):
            item = q[i]
            box = layout.box()
            box.row().prop(item, "kind", expand=True)
            if item.kind == "ANIM":
                r = box.row(align=True)
                r.prop(item, "start")
                r.prop(item, "end")
            else:
                box.prop(item, "frame")
            box.prop(item, "camera")
            if item.bookmark:
                box.prop_search(item, "bookmark", scene, "camera_bookmarks", icon="BOOKMARKS")
            box.prop(item, "output", text="")
            sub = box.row()
            sub.active = False
            sub.label(text=expand_path(item, scene))

        row = layout.row(align=True)
        row.scale_y = 1.5
        if _Runner.active:
            row.alert = True
            row.operator("shiro.rq_stop", icon="PAUSE")
        else:
            n = sum(1 for it in q if it.enabled)
            row.operator("shiro.rq_run", text=f"Render {n} Job(s)", icon="RENDER_ANIMATION")
            if any(it.status in {"FAILED", "CANCELLED"} for it in q):
                row.operator("shiro.rq_run", text="", icon="FILE_REFRESH").only_failed = True


classes = (SHIRO_PG_rq_item, SHIRO_OT_rq_add, SHIRO_OT_rq_remove, SHIRO_OT_rq_move,
           SHIRO_OT_rq_run, SHIRO_OT_rq_stop, SHIRO_UL_rq, SHIRO_PT_render_queue)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_render_queue = CollectionProperty(type=SHIRO_PG_rq_item)
    bpy.types.Scene.shiro_render_queue_index = IntProperty(default=-1)


def unregister():
    _remove_handlers()
    _Runner.active = False
    del bpy.types.Scene.shiro_render_queue_index
    del bpy.types.Scene.shiro_render_queue
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
