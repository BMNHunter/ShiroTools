"""Timeline: marker browser, frame-range presets, BPM beats and colour tags.

Replaces Marker Jump, Ranger (Timeline Manager) and BPM marker.

Data compatibility: ranges still live in ``Scene.timeline_manager_props.presets``
(name/start/end) so presets saved with Ranger load unchanged.

A GPU overlay in the Dope Sheet / Timeline draws:
  * a coloured band per range preset (active one also tints the background)
  * tag-coloured lines on markers
  * an optional beat grid (bars / beats / subdivisions) without any markers
"""

import re
import time

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    FloatVectorProperty, IntProperty, PointerProperty, StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .common import TAB, redraw

PALETTE = (
    (0.95, 0.45, 0.30), (0.30, 0.70, 0.95), (0.55, 0.85, 0.35), (0.95, 0.80, 0.25),
    (0.75, 0.45, 0.95), (0.30, 0.90, 0.80), (0.95, 0.40, 0.65), (0.65, 0.65, 0.70),
)


def palette(i):
    return PALETTE[i % len(PALETTE)]


# ---------------------------------------------------------------------------
# beat math (pure, tested)
# ---------------------------------------------------------------------------

def frames_per_beat(scene, bpm):
    fps = scene.render.fps / scene.render.fps_base
    return 60.0 * fps / max(bpm, 1e-6)


def beat_frames(start, end, fpb, subdivision=1, offset=0.0):
    """[(frame, bar, beat, sub)] for every grid step from start..end inclusive.

    Frames are computed from the step index, never accumulated, so there is
    no drift over long ranges.
    """
    step = fpb / subdivision
    out = []
    i = 0
    while True:
        f = start + offset + i * step
        if f > end + 1e-6:
            break
        out.append((f, i))
        i += 1
    return out


def beat_label(i, beats_per_bar, subdivision):
    beat_index, sub = divmod(i, subdivision)
    bar, beat = divmod(beat_index, beats_per_bar)
    return bar + 1, beat + 1, sub + 1


def beat_name(prefix, bar, beat, sub, subdivision):
    if beat == 1 and sub == 1:
        return f"{prefix}{bar}"
    if subdivision > 1 and sub > 1:
        return f"{prefix}{bar}.{beat}.{sub}"
    return f"{prefix}{bar}.{beat}"


def beat_name_regex(prefix):
    return re.compile(r"^" + re.escape(prefix) + r"\d+(\.\d+){0,2}$")


def snap_to_grid(frame, start, step, offset=0.0):
    base = start + offset
    return base + round((frame - base) / step) * step


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------

_regex_cache = {}


def tag_matches(tag, marker):
    mode = tag.match
    if mode == "CAMERA":
        return marker.camera is not None
    pattern = tag.pattern
    if not pattern:
        return False
    name = marker.name
    if not tag.case_sensitive and mode != "REGEX":
        name, pattern = name.lower(), pattern.lower()
    if mode == "PREFIX":
        return name.startswith(pattern)
    if mode == "SUFFIX":
        return name.endswith(pattern)
    if mode == "CONTAINS":
        return pattern in name
    key = (pattern, tag.case_sensitive)
    rx = _regex_cache.get(key)
    if rx is None:
        try:
            rx = re.compile(pattern, 0 if tag.case_sensitive else re.IGNORECASE)
        except re.error:
            rx = False
        _regex_cache[key] = rx
    return bool(rx and rx.search(marker.name))


def marker_tag(settings, marker):
    for tag in settings.tags:
        if tag_matches(tag, marker):
            return tag
    return None


def marker_visible(settings, marker):
    flt = settings.search
    if flt:
        name = marker.name if settings.search_case else marker.name.lower()
        needle = flt if settings.search_case else flt.lower()
        hit = needle in name
        if hit == settings.search_invert:
            return False
    isolated = [t for t in settings.tags if t.isolate]
    if isolated:
        return any(tag_matches(t, marker) for t in isolated)
    return True


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

def _tag_redraw(self, context):
    _regex_cache.clear()
    redraw(context, {"DOPESHEET_EDITOR"})


class SHIRO_PG_range(PropertyGroup):
    name: StringProperty(name="Name", default="Range")
    start: IntProperty(name="Start", default=1)
    end: IntProperty(name="End", default=250)
    color: FloatVectorProperty(name="Color", subtype="COLOR_GAMMA", size=3,
                               min=0.0, max=1.0, default=PALETTE[0], update=_tag_redraw)
    show: BoolProperty(name="Show in Overlay", default=True, update=_tag_redraw)
    camera: PointerProperty(name="Camera", type=bpy.types.Object,
                            poll=lambda self, o: o.type == "CAMERA",
                            description="Optional camera, used by Apply and the Render Queue")


class SHIRO_PG_ranges(PropertyGroup):
    # field names kept from Ranger so old files load
    presets: CollectionProperty(type=SHIRO_PG_range)
    active_index: IntProperty(default=-1, update=_tag_redraw)
    apply_to: EnumProperty(
        name="Apply To",
        items=[("SCENE", "Scene Range", "Set the scene start/end"),
               ("PREVIEW", "Preview Range", "Set the preview range, scene range untouched")],
        default="SCENE",
    )


class SHIRO_PG_tag(PropertyGroup):
    name: StringProperty(name="Name", default="Tag", update=_tag_redraw)
    match: EnumProperty(
        name="Match",
        items=[("PREFIX", "Prefix", ""), ("CONTAINS", "Contains", ""),
               ("SUFFIX", "Suffix", ""), ("REGEX", "Regex", ""),
               ("CAMERA", "Has Camera", "Markers with a bound camera")],
        default="PREFIX", update=_tag_redraw,
    )
    pattern: StringProperty(name="Pattern", update=_tag_redraw)
    case_sensitive: BoolProperty(name="Case Sensitive", default=False, update=_tag_redraw)
    color: FloatVectorProperty(name="Color", subtype="COLOR_GAMMA", size=3,
                               min=0.0, max=1.0, default=PALETTE[1], update=_tag_redraw)
    show: BoolProperty(name="Show in Overlay", default=True, update=_tag_redraw)
    isolate: BoolProperty(name="Isolate", description="Only list markers with this tag",
                          default=False, update=_tag_redraw)


_suppress_jump = False


def set_marker_index(st, index):
    """Change the list highlight without moving the playhead."""
    global _suppress_jump
    _suppress_jump = True
    try:
        st.marker_index = index
    finally:
        _suppress_jump = False


def _marker_index_changed(self, context):
    if _suppress_jump:
        return
    markers = context.scene.timeline_markers
    i = self.marker_index
    if 0 <= i < len(markers):
        m = markers[i]
        for other in markers:
            other.select = False
        m.select = True
        context.scene.frame_current = m.frame


class SHIRO_PG_timeline(PropertyGroup):
    tags: CollectionProperty(type=SHIRO_PG_tag)
    tag_index: IntProperty()

    search: StringProperty(name="Search", description="Filter markers by name",
                           options={"TEXTEDIT_UPDATE"})
    search_invert: BoolProperty(name="Invert", description="Show markers NOT matching")
    search_case: BoolProperty(name="Case Sensitive")
    marker_index: IntProperty(default=-1, update=_marker_index_changed)
    marker_sort: EnumProperty(name="Sort", items=[("FRAME", "Frame", ""), ("NAME", "Name", "")],
                              default="FRAME")

    overlay: BoolProperty(name="Overlay", default=True, update=_tag_redraw,
                          description="Draw ranges, tag colours and the beat grid in the editor")
    show_ranges: BoolProperty(name="Ranges", default=True, update=_tag_redraw)
    show_tags: BoolProperty(name="Tag Colours", default=True, update=_tag_redraw)
    show_beat_grid: BoolProperty(name="Beat Grid", default=False, update=_tag_redraw)
    overlay_alpha: FloatProperty(name="Opacity", default=0.6, min=0.05, max=1.0,
                                 subtype="FACTOR", update=_tag_redraw)

    bpm: FloatProperty(name="BPM", default=120.0, min=1.0, max=999.0, precision=2,
                       update=_tag_redraw)
    beats_per_bar: IntProperty(name="Beats / Bar", default=4, min=1, max=32, update=_tag_redraw)
    subdivision: EnumProperty(
        name="Subdivision",
        items=[("1", "Beats", ""), ("2", "1/2", ""), ("4", "1/4", ""), ("3", "Triplets", "")],
        default="1", update=_tag_redraw,
    )
    beat_offset: FloatProperty(name="Offset", default=0.0, precision=2, update=_tag_redraw,
                               description="Frames from the range start to the first beat")
    beat_use_scene_range: BoolProperty(name="Use Scene Range", default=True, update=_tag_redraw)
    beat_start: IntProperty(name="Start", default=1, update=_tag_redraw)
    beat_end: IntProperty(name="End", default=250, update=_tag_redraw)
    beat_prefix: StringProperty(name="Prefix", default="B",
                                description="Beat marker names are <prefix><bar>[.<beat>]. "
                                            "Clear only removes names in exactly that form")


def beat_range(scene, st):
    if st.beat_use_scene_range:
        return scene.frame_start, scene.frame_end
    return st.beat_start, st.beat_end


# ---------------------------------------------------------------------------
# marker operators
# ---------------------------------------------------------------------------

def _marker(context, index):
    markers = context.scene.timeline_markers
    if 0 <= index < len(markers):
        return markers[index]
    return None


class SHIRO_OT_marker_add(Operator):
    bl_idname = "shiro.marker_add"
    bl_label = "Add Marker"
    bl_description = "Add a marker at the current frame"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Name", default="")
    bind_camera: BoolProperty(name="Bind Scene Camera", default=False)

    def invoke(self, context, event):
        self.name = f"F_{context.scene.frame_current:04d}"
        return context.window_manager.invoke_props_dialog(self, width=260)

    def execute(self, context):
        scene = context.scene
        m = scene.timeline_markers.new(self.name or "Marker", frame=scene.frame_current)
        if self.bind_camera and scene.camera:
            m.camera = scene.camera
        set_marker_index(scene.shiro_timeline, len(scene.timeline_markers) - 1)
        return {"FINISHED"}


class SHIRO_OT_marker_jump(Operator):
    bl_idname = "shiro.marker_jump"
    bl_label = "Jump to Marker"
    bl_description = "Move the playhead to this marker"

    index: IntProperty()

    def execute(self, context):
        if _marker(context, self.index) is None:
            return {"CANCELLED"}
        context.scene.shiro_timeline.marker_index = self.index
        return {"FINISHED"}


class SHIRO_OT_marker_delete(Operator):
    bl_idname = "shiro.marker_delete"
    bl_label = "Delete Marker"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        m = _marker(context, self.index)
        if m is None:
            return {"CANCELLED"}
        context.scene.timeline_markers.remove(m)
        set_marker_index(context.scene.shiro_timeline,
                         min(self.index, len(context.scene.timeline_markers) - 1))
        return {"FINISHED"}


class SHIRO_OT_marker_bind_camera(Operator):
    bl_idname = "shiro.marker_bind_camera"
    bl_label = "Bind Camera"
    bl_description = ("Bind the active camera object (or the scene camera) to this marker. "
                      "Ctrl: unbind")
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    unbind: BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.unbind = event.ctrl
        return self.execute(context)

    def execute(self, context):
        m = _marker(context, self.index)
        if m is None:
            return {"CANCELLED"}
        if self.unbind:
            m.camera = None
            return {"FINISHED"}
        obj = context.active_object
        cam = obj if obj and obj.type == "CAMERA" else context.scene.camera
        if cam is None:
            self.report({"WARNING"}, "No camera to bind")
            return {"CANCELLED"}
        m.camera = cam
        return {"FINISHED"}


def _next_marker_frame(scene, frame):
    later = [m.frame for m in scene.timeline_markers if m.frame > frame]
    return min(later) if later else None


class SHIRO_OT_marker_to_range(Operator):
    bl_idname = "shiro.marker_to_range"
    bl_label = "Range to Next Marker"
    bl_description = "From this marker up to the frame before the next one"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    target: EnumProperty(items=[("PRESET", "New Preset", ""), ("PREVIEW", "Preview Range", "")],
                         default="PRESET")

    def execute(self, context):
        scene = context.scene
        m = _marker(context, self.index)
        if m is None:
            return {"CANCELLED"}
        nxt = _next_marker_frame(scene, m.frame)
        end = nxt - 1 if nxt is not None else scene.frame_end
        if self.target == "PREVIEW":
            scene.use_preview_range = True
            scene.frame_preview_start, scene.frame_preview_end = m.frame, max(m.frame, end)
        else:
            _add_range(scene, m.name, m.frame, max(m.frame, end), camera=m.camera)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# range operators
# ---------------------------------------------------------------------------

def _add_range(scene, name, start, end, camera=None):
    mgr = scene.timeline_manager_props
    item = mgr.presets.add()
    item.name = name
    item.start, item.end = min(start, end), max(start, end)
    item.color = palette(len(mgr.presets) - 1)
    item.camera = camera
    mgr.active_index = len(mgr.presets) - 1
    return item


def _range(context, index):
    mgr = context.scene.timeline_manager_props
    if 0 <= index < len(mgr.presets):
        return mgr.presets[index]
    return None


def apply_range(scene, item, target="SCENE"):
    if target == "PREVIEW":
        scene.use_preview_range = True
        scene.frame_preview_start = item.start
        scene.frame_preview_end = item.end
    else:
        # set end first when moving forward so start <= end holds throughout
        if item.start > scene.frame_end:
            scene.frame_end, scene.frame_start = item.end, item.start
        else:
            scene.frame_start, scene.frame_end = item.start, item.end
    if item.camera is not None:
        scene.camera = item.camera


class SHIRO_OT_range_add(Operator):
    bl_idname = "shiro.range_add"
    bl_label = "Add Range"
    bl_description = "Store a frame range preset"
    bl_options = {"REGISTER", "UNDO"}

    source: EnumProperty(
        items=[("SCENE", "Scene Range", ""), ("PREVIEW", "Preview Range", ""),
               ("MARKERS", "Selected Markers", "From the first to the last selected marker")],
        default="SCENE",
    )

    def execute(self, context):
        scene = context.scene
        mgr = scene.timeline_manager_props
        name = f"Range {len(mgr.presets) + 1}"
        if self.source == "PREVIEW" and scene.use_preview_range:
            start, end = scene.frame_preview_start, scene.frame_preview_end
        elif self.source == "MARKERS":
            frames = [m.frame for m in scene.timeline_markers if m.select]
            if len(frames) < 2:
                self.report({"WARNING"}, "Select at least two markers")
                return {"CANCELLED"}
            start, end = min(frames), max(frames)
        else:
            start, end = scene.frame_start, scene.frame_end
        _add_range(scene, name, start, end)
        return {"FINISHED"}


class SHIRO_OT_range_apply(Operator):
    bl_idname = "shiro.range_apply"
    bl_label = "Apply Range"
    bl_description = "Set the scene (or preview) range from this preset. Shift: preview range"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    preview: BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.preview = event.shift
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        item = _range(context, self.index)
        if item is None:
            return {"CANCELLED"}
        mgr = scene.timeline_manager_props
        target = "PREVIEW" if self.preview else mgr.apply_to
        apply_range(scene, item, target)
        mgr.active_index = self.index
        return {"FINISHED"}


class SHIRO_OT_range_remove(Operator):
    bl_idname = "shiro.range_remove"
    bl_label = "Remove Range"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        mgr = context.scene.timeline_manager_props
        i = mgr.active_index
        if not (0 <= i < len(mgr.presets)):
            return {"CANCELLED"}
        mgr.presets.remove(i)
        mgr.active_index = min(i, len(mgr.presets) - 1)
        return {"FINISHED"}


class SHIRO_OT_range_move(Operator):
    bl_idname = "shiro.range_move"
    bl_label = "Move Range"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        mgr = context.scene.timeline_manager_props
        i = mgr.active_index
        j = i - 1 if self.direction == "UP" else i + 1
        if not (0 <= i < len(mgr.presets) and 0 <= j < len(mgr.presets)):
            return {"CANCELLED"}
        mgr.presets.move(i, j)
        mgr.active_index = j
        return {"FINISHED"}


class SHIRO_OT_range_duplicate(Operator):
    bl_idname = "shiro.range_duplicate"
    bl_label = "Duplicate Range"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        item = _range(context, scene.timeline_manager_props.active_index)
        if item is None:
            return {"CANCELLED"}
        _add_range(scene, item.name + " copy", item.start, item.end, item.camera)
        return {"FINISHED"}


class SHIRO_OT_range_copy_to_scenes(Operator):
    bl_idname = "shiro.range_copy_to_scenes"
    bl_label = "Copy Presets to All Scenes"
    bl_description = "Copy every range preset of this scene into all other scenes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = context.scene.timeline_manager_props.presets
        count = 0
        for scene in bpy.data.scenes:
            if scene == context.scene:
                continue
            names = {p.name for p in scene.timeline_manager_props.presets}
            for p in src:
                if p.name in names:
                    continue
                item = _add_range(scene, p.name, p.start, p.end)
                item.color = p.color
                count += 1
        self.report({"INFO"}, f"Copied {count} preset(s)")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# beat operators
# ---------------------------------------------------------------------------

_taps = []


class SHIRO_OT_beat_tap(Operator):
    bl_idname = "shiro.beat_tap"
    bl_label = "Tap"
    bl_description = "Click along with the music to measure the BPM (pause 2s to restart)"

    def execute(self, context):
        now = time.perf_counter()
        if _taps and now - _taps[-1] > 2.0:
            _taps.clear()
        _taps.append(now)
        del _taps[:-9]
        if len(_taps) >= 2:
            gaps = [b - a for a, b in zip(_taps, _taps[1:])]
            context.scene.shiro_timeline.bpm = round(60.0 / (sum(gaps) / len(gaps)), 1)
        return {"FINISHED"}


class SHIRO_OT_beat_markers(Operator):
    bl_idname = "shiro.beat_markers"
    bl_label = "Generate Beat Markers"
    bl_description = "Place a marker on every beat. Previous beat markers are replaced"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        st = scene.shiro_timeline
        clear_beat_markers(scene, st.beat_prefix)
        start, end = beat_range(scene, st)
        sub = int(st.subdivision)
        fpb = frames_per_beat(scene, st.bpm)
        made = 0
        for f, i in beat_frames(start, end, fpb, sub, st.beat_offset):
            bar, beat, s = beat_label(i, st.beats_per_bar, sub)
            scene.timeline_markers.new(beat_name(st.beat_prefix, bar, beat, s, sub),
                                       frame=int(round(f)))
            made += 1
        _ensure_beat_tag(st)
        self.report({"INFO"}, f"{made} beat markers ({fpb:.2f} frames per beat)")
        return {"FINISHED"}


def clear_beat_markers(scene, prefix):
    rx = beat_name_regex(prefix)
    doomed = [m for m in scene.timeline_markers if rx.match(m.name)]
    for m in doomed:
        scene.timeline_markers.remove(m)
    return len(doomed)


def _ensure_beat_tag(st):
    for t in st.tags:
        if t.match == "REGEX" and t.name == "Beat":
            t.pattern = beat_name_regex(st.beat_prefix).pattern
            return
    t = st.tags.add()
    t.name, t.match = "Beat", "REGEX"
    t.case_sensitive = True
    t.pattern = beat_name_regex(st.beat_prefix).pattern
    t.color = (0.75, 0.45, 0.95)


class SHIRO_OT_beat_clear(Operator):
    bl_idname = "shiro.beat_clear"
    bl_label = "Clear Beat Markers"
    bl_description = "Remove markers named exactly <prefix><bar>[.<beat>]. Others are kept"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = clear_beat_markers(context.scene, context.scene.shiro_timeline.beat_prefix)
        self.report({"INFO"}, f"Removed {n} beat marker(s)")
        return {"FINISHED"}


def iter_selected_keyframe_curves(context):
    """F-Curves of selected objects (and their data) through the 5.x channelbag API."""
    from bpy_extras import anim_utils
    seen = set()
    ids = []
    for obj in context.selected_objects:
        ids.append(obj)
        if obj.data is not None and hasattr(obj.data, "animation_data"):
            ids.append(obj.data)
        sk = getattr(obj.data, "shape_keys", None)
        if sk is not None:
            ids.append(sk)
    for idb in ids:
        ad = getattr(idb, "animation_data", None)
        if ad is None or ad.action is None or ad.action_slot is None:
            continue
        cb = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        if cb is None or cb.as_pointer() in seen:
            continue
        seen.add(cb.as_pointer())
        yield from cb.fcurves


class SHIRO_OT_beat_snap_keys(Operator):
    bl_idname = "shiro.beat_snap_keys"
    bl_label = "Snap Keys to Beats"
    bl_description = "Move the selected keyframes of the selected objects onto the nearest beat"
    bl_options = {"REGISTER", "UNDO"}

    whole_frames: BoolProperty(name="Whole Frames", default=True,
                               description="Round snapped keys to whole frames")

    def execute(self, context):
        import numpy as np
        scene = context.scene
        st = scene.shiro_timeline
        start, _end = beat_range(scene, st)
        step = frames_per_beat(scene, st.bpm) / int(st.subdivision)
        moved = 0
        for fc in iter_selected_keyframe_curves(context):
            kps = fc.keyframe_points
            n = len(kps)
            if n == 0:
                continue
            sel = np.zeros(n, dtype=bool)
            kps.foreach_get("select_control_point", sel)
            if not sel.any():
                continue
            co = np.zeros(n * 2, dtype=np.float32)
            hl = np.zeros(n * 2, dtype=np.float32)
            hr = np.zeros(n * 2, dtype=np.float32)
            kps.foreach_get("co", co)
            kps.foreach_get("handle_left", hl)
            kps.foreach_get("handle_right", hr)
            x = co[0::2]
            base = start + st.beat_offset
            target = base + np.round((x - base) / step) * step
            if self.whole_frames:
                target = np.round(target)
            delta = np.where(sel, target - x, 0.0)
            co[0::2] += delta
            hl[0::2] += delta
            hr[0::2] += delta
            kps.foreach_set("co", co)
            kps.foreach_set("handle_left", hl)
            kps.foreach_set("handle_right", hr)
            fc.update()
            moved += int(sel.sum())
        self.report({"INFO"}, f"Snapped {moved} key(s)")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# tag operators
# ---------------------------------------------------------------------------

class SHIRO_OT_tag_add(Operator):
    bl_idname = "shiro.tag_add"
    bl_label = "Add Tag"
    bl_options = {"REGISTER", "UNDO"}

    defaults: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        st = context.scene.shiro_timeline
        if self.defaults:
            for name, match, pattern, color in (
                ("Camera", "CAMERA", "", (0.30, 0.70, 0.95)),
                ("Shot", "PREFIX", "sh", (0.95, 0.60, 0.25)),
                ("Key", "CONTAINS", "key", (0.55, 0.85, 0.35)),
            ):
                t = st.tags.add()
                t.name, t.match, t.pattern, t.color = name, match, pattern, color
            _ensure_beat_tag(st)
        else:
            t = st.tags.add()
            t.name = f"Tag {len(st.tags)}"
            t.color = palette(len(st.tags) + 2)
        st.tag_index = len(st.tags) - 1
        return {"FINISHED"}


class SHIRO_OT_tag_remove(Operator):
    bl_idname = "shiro.tag_remove"
    bl_label = "Remove Tag"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        st = context.scene.shiro_timeline
        if not (0 <= st.tag_index < len(st.tags)):
            return {"CANCELLED"}
        st.tags.remove(st.tag_index)
        st.tag_index = min(st.tag_index, len(st.tags) - 1)
        _regex_cache.clear()
        return {"FINISHED"}


class SHIRO_OT_tag_move(Operator):
    bl_idname = "shiro.tag_move"
    bl_label = "Move Tag"
    bl_description = "Tags are matched top to bottom, the first hit colours the marker"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        st = context.scene.shiro_timeline
        i = st.tag_index
        j = i - 1 if self.direction == "UP" else i + 1
        if not (0 <= i < len(st.tags) and 0 <= j < len(st.tags)):
            return {"CANCELLED"}
        st.tags.move(i, j)
        st.tag_index = j
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI lists
# ---------------------------------------------------------------------------

class SHIRO_UL_markers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        st = context.scene.shiro_timeline
        tag = marker_tag(st, item)
        row = layout.row(align=True)
        swatch = row.row(align=True)
        swatch.ui_units_x = 0.8
        if tag is not None:
            swatch.prop(tag, "color", text="")
        else:
            swatch.label(text="", icon="MARKER" if not item.select else "MARKER_HLT")
        row.prop(item, "name", text="", emboss=False)
        if item.camera is not None:
            row.label(text="", icon="VIEW_CAMERA")
        cur = context.scene.frame_current == item.frame
        fr = row.row(align=True)
        fr.alignment = "RIGHT"
        fr.alert = cur
        fr.label(text=f"{item.frame}")
        row.operator("shiro.marker_jump", text="", icon="PLAY", emboss=False).index = index

    def filter_items(self, context, data, propname):
        st = context.scene.shiro_timeline
        markers = getattr(data, propname)
        flags = [self.bitflag_filter_item if marker_visible(st, m) else 0 for m in markers]
        if st.marker_sort == "NAME":
            order = bpy.types.UI_UL_list.sort_items_by_name(markers, "name")
        else:
            order = bpy.types.UI_UL_list.sort_items_helper(
                [(i, m.frame) for i, m in enumerate(markers)], key=lambda t: t[1])
        return flags, order


class SHIRO_UL_ranges(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        scene = context.scene
        row = layout.row(align=True)
        sw = row.row(align=True)
        sw.ui_units_x = 0.8
        sw.prop(item, "color", text="")
        row.prop(item, "name", text="", emboss=False)
        if item.camera is not None:
            row.label(text="", icon="VIEW_CAMERA")
        live = scene.frame_start == item.start and scene.frame_end == item.end
        info = row.row(align=True)
        info.alignment = "RIGHT"
        info.alert = live
        info.label(text=f"{item.start}–{item.end}  ({item.end - item.start + 1}f)")
        op = row.operator("shiro.range_apply", text="", icon="CHECKMARK" if live else "PLAY",
                          emboss=False)
        op.index = index


class SHIRO_UL_tags(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        row = layout.row(align=True)
        sw = row.row(align=True)
        sw.ui_units_x = 0.8
        sw.prop(item, "color", text="")
        row.prop(item, "name", text="", emboss=False)
        sub = row.row(align=True)
        sub.active = False
        label = "camera bound" if item.match == "CAMERA" else f"{item.match.lower()}: {item.pattern}"
        sub.label(text=label)
        row.prop(item, "isolate", text="", icon="RESTRICT_SELECT_OFF" if item.isolate
                 else "RESTRICT_SELECT_ON", emboss=False)
        row.prop(item, "show", text="", icon="HIDE_OFF" if item.show else "HIDE_ON", emboss=False)


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------

class _TimelinePanel:
    bl_space_type = "DOPESHEET_EDITOR"
    bl_region_type = "UI"
    bl_category = TAB


class SHIRO_PT_timeline(_TimelinePanel, Panel):
    bl_idname = "SHIRO_PT_timeline"
    bl_label = "Shiro Timeline"

    def draw_header(self, context):
        self.layout.prop(context.scene.shiro_timeline, "overlay", text="")

    def draw(self, context):
        st = context.scene.shiro_timeline
        col = self.layout.column(align=True)
        col.active = st.overlay
        row = col.row(align=True)
        row.prop(st, "show_ranges", toggle=True, icon="PREVIEW_RANGE")
        row.prop(st, "show_tags", toggle=True, icon="COLOR")
        row.prop(st, "show_beat_grid", toggle=True, icon="SEQ_LUMA_WAVEFORM")
        col.prop(st, "overlay_alpha", slider=True)


class SHIRO_PT_tl_markers(_TimelinePanel, Panel):
    bl_idname = "SHIRO_PT_tl_markers"
    bl_parent_id = "SHIRO_PT_timeline"
    bl_label = "Markers"

    def draw_header(self, context):
        n = len(context.scene.timeline_markers)
        self.layout.label(text=f"({n})")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        st = scene.shiro_timeline

        row = layout.row(align=True)
        row.prop(st, "search", text="", icon="VIEWZOOM")
        row.prop(st, "search_case", text="", icon="SMALL_CAPS")
        row.prop(st, "search_invert", text="", icon="ARROW_LEFTRIGHT")
        row.prop(st, "marker_sort", text="", icon="SORTSIZE", icon_only=True)

        row = layout.row(align=True)
        row.operator("screen.marker_jump", text="", icon="TRIA_LEFT").next = False
        row.operator("shiro.marker_add", text="Add", icon="ADD")
        row.operator("screen.marker_jump", text="", icon="TRIA_RIGHT").next = True

        if not scene.timeline_markers:
            layout.label(text="No markers", icon="INFO")
            return
        layout.template_list("SHIRO_UL_markers", "", scene, "timeline_markers",
                             st, "marker_index", rows=6)

        m = _marker(context, st.marker_index)
        if m is None:
            return
        box = layout.box()
        row = box.row(align=True)
        row.prop(m, "frame")
        row.operator("shiro.marker_delete", text="", icon="TRASH").index = st.marker_index
        row = box.row(align=True)
        row.operator("shiro.marker_bind_camera", icon="VIEW_CAMERA",
                     text=m.camera.name if m.camera else "Bind Camera").index = st.marker_index
        row = box.row(align=True)
        op = row.operator("shiro.marker_to_range", text="Preset to Next", icon="PREVIEW_RANGE")
        op.index, op.target = st.marker_index, "PRESET"
        op = row.operator("shiro.marker_to_range", text="Preview to Next", icon="TIME")
        op.index, op.target = st.marker_index, "PREVIEW"


class SHIRO_PT_tl_ranges(_TimelinePanel, Panel):
    bl_idname = "SHIRO_PT_tl_ranges"
    bl_parent_id = "SHIRO_PT_timeline"
    bl_label = "Ranges"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        mgr = scene.timeline_manager_props

        row = layout.row()
        row.template_list("SHIRO_UL_ranges", "", mgr, "presets", mgr, "active_index", rows=4)
        col = row.column(align=True)
        col.operator_menu_enum("shiro.range_add", "source", text="", icon="ADD")
        col.operator("shiro.range_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("shiro.range_duplicate", text="", icon="DUPLICATE")
        col.separator()
        col.operator("shiro.range_move", text="", icon="TRIA_UP").direction = "UP"
        col.operator("shiro.range_move", text="", icon="TRIA_DOWN").direction = "DOWN"

        layout.row().prop(mgr, "apply_to", expand=True)
        item = _range(context, mgr.active_index)
        if item is not None:
            box = layout.box()
            row = box.row(align=True)
            row.prop(item, "start")
            row.prop(item, "end")
            row = box.row(align=True)
            row.prop(item, "camera", text="", icon="VIEW_CAMERA")
            row.prop(item, "show", text="", icon="HIDE_OFF" if item.show else "HIDE_ON")
            if item.start > item.end:
                box.label(text="Start is after End", icon="ERROR")
            big = box.row()
            big.scale_y = 1.3
            big.operator("shiro.range_apply", text="Apply", icon="PLAY").index = mgr.active_index
        if len(bpy.data.scenes) > 1:
            layout.operator("shiro.range_copy_to_scenes", icon="SCENE_DATA")


class SHIRO_PT_tl_beats(_TimelinePanel, Panel):
    bl_idname = "SHIRO_PT_tl_beats"
    bl_parent_id = "SHIRO_PT_timeline"
    bl_label = "Beats (BPM)"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        st = scene.shiro_timeline

        row = layout.row(align=True)
        row.prop(st, "bpm")
        tap = row.row(align=True)
        tap.scale_x = 0.8
        tap.operator("shiro.beat_tap", icon="EVENT_T")
        col = layout.column(align=True)
        col.prop(st, "beats_per_bar")
        col.prop(st, "subdivision", text="")
        col.prop(st, "beat_offset")
        col.prop(st, "beat_use_scene_range")
        if not st.beat_use_scene_range:
            row = col.row(align=True)
            row.prop(st, "beat_start")
            row.prop(st, "beat_end")
        fpb = frames_per_beat(scene, st.bpm)
        info = layout.row()
        info.active = False
        info.label(text=f"{fpb:.2f} frames / beat  ·  {fpb * st.beats_per_bar:.1f} / bar")

        row = layout.row(align=True)
        row.prop(st, "show_beat_grid", text="Grid", toggle=True, icon="SEQ_LUMA_WAVEFORM")
        row.operator("shiro.beat_snap_keys", text="Snap Keys", icon="SNAP_ON")
        row = layout.row(align=True)
        row.operator("shiro.beat_markers", text="Markers", icon="MARKER_HLT")
        row.operator("shiro.beat_clear", text="", icon="TRASH")
        layout.prop(st, "beat_prefix")


class SHIRO_PT_tl_tags(_TimelinePanel, Panel):
    bl_idname = "SHIRO_PT_tl_tags"
    bl_parent_id = "SHIRO_PT_timeline"
    bl_label = "Colour Tags"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        st = context.scene.shiro_timeline
        if not st.tags:
            layout.operator("shiro.tag_add", text="Add Default Tags", icon="COLOR").defaults = True
        row = layout.row()
        row.template_list("SHIRO_UL_tags", "", st, "tags", st, "tag_index", rows=3)
        col = row.column(align=True)
        col.operator("shiro.tag_add", text="", icon="ADD")
        col.operator("shiro.tag_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("shiro.tag_move", text="", icon="TRIA_UP").direction = "UP"
        col.operator("shiro.tag_move", text="", icon="TRIA_DOWN").direction = "DOWN"
        if 0 <= st.tag_index < len(st.tags):
            t = st.tags[st.tag_index]
            col = layout.column(align=True)
            col.prop(t, "match", text="")
            if t.match != "CAMERA":
                row = col.row(align=True)
                row.prop(t, "pattern", text="")
                row.prop(t, "case_sensitive", text="", icon="SMALL_CAPS")


# ---------------------------------------------------------------------------
# overlay
# ---------------------------------------------------------------------------

def overlay_geometry(scene, st, frame_to_x, x_min, x_max, height, ui_scale):
    """Build overlay rectangles and labels in region pixel space.

    Returns (rects, labels): rects are (x0, y0, x1, y1, (r, g, b, a)),
    labels are (x, y, text, (r, g, b, a)). Pure so it can be tested headless.
    """
    rects, labels = [], []
    alpha = st.overlay_alpha
    top = height - 24 * ui_scale  # below the frame-number scrubbing strip

    if st.show_ranges:
        mgr = scene.timeline_manager_props
        band = 11 * ui_scale
        lane = 0
        for i, item in enumerate(mgr.presets):
            if not item.show:
                continue
            x0, x1 = frame_to_x(item.start), frame_to_x(item.end + 1)
            if x1 < x_min or x0 > x_max:
                lane += 1
                continue
            r, g, b = item.color
            active = i == mgr.active_index
            y1 = top - lane * (band + 2 * ui_scale)
            y0 = y1 - band
            rects.append((x0, y0, x1, y1, (r, g, b, alpha * (0.9 if active else 0.55))))
            if active:
                rects.append((x0, 0, x1, y0, (r, g, b, alpha * 0.07)))
            if x1 - x0 > 40 * ui_scale:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                tc = (0.05, 0.05, 0.05, 1.0) if lum > 0.55 else (1.0, 1.0, 1.0, 1.0)
                labels.append((max(x0, x_min) + 4 * ui_scale, y0 + 2 * ui_scale, item.name, tc))
            lane += 1
            if lane >= 6:
                break

    if st.show_tags and st.tags:
        w = max(1.0, 1.5 * ui_scale)
        for m in scene.timeline_markers:
            tag = marker_tag(st, m)
            if tag is None or not tag.show:
                continue
            x = frame_to_x(m.frame)
            if x < x_min - 2 or x > x_max + 2:
                continue
            r, g, b = tag.color
            rects.append((x - w * 0.5, 0, x + w * 0.5, top, (r, g, b, alpha * 0.45)))
            rects.append((x - 3 * ui_scale, 0, x + 3 * ui_scale, 4 * ui_scale, (r, g, b, alpha)))

    if st.show_beat_grid:
        start, end = beat_range(scene, st)
        sub = int(st.subdivision)
        fpb = frames_per_beat(scene, st.bpm)
        step_px = abs(frame_to_x(start + fpb / sub) - frame_to_x(start))
        beat_px = step_px * sub
        bar_px = beat_px * st.beats_per_bar
        if bar_px >= 6:
            for f, i in beat_frames(start, end, fpb, sub, st.beat_offset):
                x = frame_to_x(f)
                if x < x_min or x > x_max:
                    continue
                bar, beat, s = beat_label(i, st.beats_per_bar, sub)
                if beat == 1 and s == 1:
                    a, w = 0.40, 2.0
                elif s == 1:
                    if beat_px < 6:
                        continue
                    a, w = 0.18, 1.0
                else:
                    if step_px < 6:
                        continue
                    a, w = 0.08, 1.0
                w *= ui_scale
                rects.append((x - w * 0.5, 0, x + w * 0.5, top, (1.0, 1.0, 1.0, a * alpha)))
                if beat == 1 and s == 1 and bar_px > 30 * ui_scale:
                    labels.append((x + 3 * ui_scale, 8 * ui_scale, str(bar),
                                   (1.0, 1.0, 1.0, 0.5 * alpha)))
    return rects, labels


_draw_handle = None


def _draw_overlay():
    try:
        context = bpy.context
        space = context.space_data
        region = context.region
        scene = context.scene
        if space is None or region is None or scene is None or region.type != "WINDOW":
            return
        st = getattr(scene, "shiro_timeline", None)
        if st is None or not st.overlay:
            return
        if not (st.show_ranges or st.show_tags or st.show_beat_grid):
            return
        v2d = region.view2d

        def frame_to_x(f):
            return v2d.view_to_region(f, 0.0, clip=False)[0]

        ui_scale = context.preferences.system.ui_scale
        rects, labels = overlay_geometry(scene, st, frame_to_x, 0, region.width,
                                         region.height, ui_scale)
        if not rects and not labels:
            return

        import gpu
        from gpu_extras.batch import batch_for_shader
        import blf

        if rects:
            pos, col, idx = [], [], []
            for x0, y0, x1, y1, c in rects:
                base = len(pos)
                pos += [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                col += [c] * 4
                idx += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
            shader = gpu.shader.from_builtin("FLAT_COLOR")
            batch = batch_for_shader(shader, "TRIS", {"pos": pos, "color": col}, indices=idx)
            gpu.state.blend_set("ALPHA")
            batch.draw(shader)
            gpu.state.blend_set("NONE")

        font = 0
        blf.size(font, 10 * ui_scale)
        for x, y, text, c in labels:
            blf.color(font, *c)
            blf.position(font, x, y, 0)
            blf.draw(font, text)
    except Exception as err:  # never break the editor's drawing
        print("[ShiroTools] timeline overlay:", err)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

classes = (
    SHIRO_PG_range, SHIRO_PG_ranges, SHIRO_PG_tag, SHIRO_PG_timeline,
    SHIRO_OT_marker_add, SHIRO_OT_marker_jump, SHIRO_OT_marker_delete,
    SHIRO_OT_marker_bind_camera, SHIRO_OT_marker_to_range,
    SHIRO_OT_range_add, SHIRO_OT_range_apply, SHIRO_OT_range_remove, SHIRO_OT_range_move,
    SHIRO_OT_range_duplicate, SHIRO_OT_range_copy_to_scenes,
    SHIRO_OT_beat_tap, SHIRO_OT_beat_markers, SHIRO_OT_beat_clear, SHIRO_OT_beat_snap_keys,
    SHIRO_OT_tag_add, SHIRO_OT_tag_remove, SHIRO_OT_tag_move,
    SHIRO_UL_markers, SHIRO_UL_ranges, SHIRO_UL_tags,
    SHIRO_PT_timeline, SHIRO_PT_tl_markers, SHIRO_PT_tl_ranges, SHIRO_PT_tl_beats,
    SHIRO_PT_tl_tags,
)


def register():
    global _draw_handle
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.timeline_manager_props = PointerProperty(type=SHIRO_PG_ranges)
    bpy.types.Scene.shiro_timeline = PointerProperty(type=SHIRO_PG_timeline)
    if not bpy.app.background:
        _draw_handle = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
            _draw_overlay, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    del bpy.types.Scene.shiro_timeline
    del bpy.types.Scene.timeline_manager_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _regex_cache.clear()
