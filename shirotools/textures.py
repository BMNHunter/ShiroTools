"""Textures: scene texture stats (VRAM estimate) and per-object texture resizing.

Replaces Texture Stats and Texture Optimizer.

* Sizes of images that are not loaded are read from the file headers, so the
  scan never loads pixels (the old "Full Scan" that could eat GBs is gone).
* Resizing uses Blender's own ``imbuf`` module: no Pillow, no pip, and EXR /
  HDR / 16-bit PNG work too. Large downscales go through a halving chain,
  which filters like a mip chain instead of aliasing.
* Resized copies are stored per image (not per object), since the image
  data-block is shared by every object that uses it.
* Draw code never touches the disk or forces images to load.

Data compatibility: the original-path stamp key is unchanged, so images resized
by Texture Optimizer v4 can still be restored.
"""

import math
import os
from collections import defaultdict

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel

from . import imgheader
from .common import (TAB, human_bytes, image_abspath, material_images, object_images,
                     redraw, scene_objects_deep)

SIZES = (8192, 4096, 2048, 1024, 512, 256, 128)
LABEL = {8192: "8K", 4096: "4K", 2048: "2K", 1024: "1K", 512: "512", 256: "256", 128: "128"}
STAMP_KEY = "texopt_original_filepath"
STAMP_SIZE = "texopt_original_size"
ROOT_NAME = "Optimized Textures"
FLOAT_EXT = {".exr", ".hdr"}
TOP_N = 10

_stats = None          # last scan result
_proxy_files = None    # set of normalised paths under the output root


# ---------------------------------------------------------------------------
# sizes & VRAM
# ---------------------------------------------------------------------------

def image_size(img, read_headers=True):
    """(w, h) summed over UDIM tiles is not meaningful, so returns tile list.

    Returns a list of (w, h) per tile (one entry for non-tiled images), or None
    when unknown. Never forces the image to load.
    """
    if img.source == "TILED":
        out = []
        base = image_abspath(img)
        for tile in img.tiles:
            if img.has_data and tile.size[0]:
                out.append((tile.size[0], tile.size[1]))
            elif read_headers and base:
                dims = imgheader.read_size(base.replace("<UDIM>", str(tile.number)))
                if dims:
                    out.append(dims)
        return out or None
    if img.has_data:
        w, h = img.size[0], img.size[1]
        return [(w, h)] if w and h else None
    if not read_headers:
        return None
    if img.packed_file is not None:
        ext = os.path.splitext(img.filepath)[1] or ".png"
        dims = imgheader.read_size_bytes(img.packed_file.data, ext)
    else:
        path = image_abspath(img)
        dims = imgheader.read_size(path) if path else None
    return [dims] if dims else None


def image_is_float(img):
    if img.has_data:
        return img.is_float
    path = image_abspath(img)
    if path and img.packed_file is None:
        return imgheader.is_high_bitdepth(path)
    return os.path.splitext(img.filepath)[1].lower() in FLOAT_EXT


def estimate_vram(img, tiles):
    bpp = 8 if image_is_float(img) else 4  # RGBA16F vs RGBA8 on the GPU
    return sum(w * h * bpp for w, h in tiles) * 4 // 3  # + mip chain


def used_images(scene):
    """{image: set(object names)} for everything the scene can render."""
    users = defaultdict(set)
    for obj in scene_objects_deep(scene):
        for img in object_images(obj):
            users[img].add(obj.name)
        if obj.type == "LIGHT" and getattr(obj.data, "node_tree", None):
            for img in material_images(obj.data):
                users[img].add(obj.name)
    if scene.world is not None:
        for img in material_images(scene.world):
            users[img].add("World")
    return users


def gather_stats(scene, used_only=True):
    users = used_images(scene)
    if used_only:
        images = list(users)
    else:
        images = [i for i in bpy.data.images if i.source in {"FILE", "SEQUENCE", "MOVIE",
                                                             "TILED"}]
    res_count, res_vram = defaultdict(int), defaultdict(int)
    vram_of = {}
    total = linked = unknown = 0
    total_vram = 0
    for img in images:
        tiles = image_size(img)
        if not tiles:
            unknown += 1
            continue
        total += 1
        linked += img.library is not None
        v = estimate_vram(img, tiles)
        vram_of[img] = v
        total_vram += v
        key = tiles[0] if len(tiles) == 1 else (tiles[0][0], tiles[0][1], len(tiles))
        res_count[key] += 1
        res_vram[key] += v

    per_obj = defaultdict(lambda: [0, 0])  # name -> [count, vram]
    for img, names in users.items():
        for name in names:
            per_obj[name][0] += 1
            per_obj[name][1] += vram_of.get(img, 0)
    unused = sum(1 for i in bpy.data.images if i.users == 0 or (i.users == 1 and i.use_fake_user))
    return {
        "total": total, "linked": linked, "unknown": unknown, "vram": total_vram,
        "used_only": used_only, "unused": unused,
        "res": sorted(res_count.items(), key=lambda kv: -res_vram[kv[0]]),
        "res_vram": dict(res_vram),
        "by_count": sorted(per_obj.items(), key=lambda kv: -kv[1][0])[:TOP_N],
        "by_vram": sorted(per_obj.items(), key=lambda kv: -kv[1][1])[:TOP_N],
    }


def res_label(key):
    if len(key) == 3:
        return f"{key[0]}×{key[1]} ×{key[2]} UDIM"
    return f"{key[0]}×{key[1]}"


# ---------------------------------------------------------------------------
# optimizer helpers
# ---------------------------------------------------------------------------

def output_root():
    if not bpy.data.filepath:
        return None
    return os.path.join(os.path.dirname(bpy.data.filepath), ROOT_NAME)


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def is_proxy(path):
    root = output_root()
    if not root or not path:
        return False
    return _norm(path).startswith(_norm(root) + os.sep)


def original_path(img):
    val = img.get(STAMP_KEY)
    if isinstance(val, bytes):
        val = val.decode("utf-8")
    if val and os.path.isfile(val):
        return val
    cur = image_abspath(img)
    if cur and not is_proxy(cur) and os.path.isfile(cur):
        return cur
    return None


def original_size(img):
    """Original file dimensions, cached on the image so draw stays cheap."""
    val = img.get(STAMP_SIZE)
    if val is not None and len(val) == 2 and val[0]:
        return int(val[0]), int(val[1])
    src = original_path(img)
    dims = imgheader.read_size(src) if src else None
    if dims is None and img.has_data and not is_proxy(image_abspath(img)):
        dims = (img.size[0], img.size[1])
    return dims


def stamp(img):
    """Remember where the full-resolution file lives. Never stamps a proxy."""
    src = original_path(img)
    if src is None:
        return None
    if not img.get(STAMP_KEY):
        img[STAMP_KEY] = src
    dims = imgheader.read_size(src)
    if dims:
        img[STAMP_SIZE] = list(dims)
    return src


def bucket(px):
    for s in SIZES:
        if px >= s:
            return s
    return SIZES[-1]


def proxy_path(img, size):
    root = output_root()
    src = original_path(img)
    if root is None or src is None:
        return None
    ext = os.path.splitext(src)[1]
    return os.path.join(root, LABEL[size], bpy.path.clean_name(img.name) + ext)


def proxy_files():
    global _proxy_files
    if _proxy_files is None:
        _proxy_files = set()
        root = output_root()
        if root and os.path.isdir(root):
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    _proxy_files.add(_norm(os.path.join(dirpath, f)))
    return _proxy_files


def invalidate_proxy_cache():
    global _proxy_files
    _proxy_files = None


def target_dims(orig, size):
    w, h = orig
    scale = size / max(w, h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def resize_file(src, dst, w, h):
    """High-quality downscale with imbuf: halve until close, then one final step."""
    import imbuf
    ib = imbuf.load(src)
    try:
        cw, ch = ib.size
        while cw // 2 >= w and ch // 2 >= h and (cw > w or ch > h):
            cw, ch = max(1, cw // 2), max(1, ch // 2)
            ib.resize((cw, ch), method="BILINEAR")
        if (cw, ch) != (w, h):
            ib.resize((w, h), method="BILINEAR")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        imbuf.write(ib, filepath=dst)
    finally:
        ib.free()
    proxy_files().add(_norm(dst))


def apply_size(img, size, report):
    """Point `img` at a copy resized to `size` (longest side). True on success."""
    if not bpy.data.filepath:
        report({"ERROR"}, "Save the .blend file first")
        return False
    if img.packed_file is not None:
        report({"WARNING"}, f"'{img.name}' is packed, unpack it first")
        return False
    src = stamp(img)
    if src is None:
        report({"WARNING"}, f"'{img.name}': original file is missing")
        return False
    orig = original_size(img)
    if not orig:
        report({"WARNING"}, f"'{img.name}': can't read the original size")
        return False
    if size >= bucket(max(orig)):
        target = src  # the original is already this size or smaller
    else:
        target = proxy_path(img, size)
        if _norm(target) not in proxy_files() or not os.path.isfile(target):
            try:
                resize_file(src, target, *target_dims(orig, size))
            except (RuntimeError, OSError, ValueError) as err:
                report({"WARNING"}, f"'{img.name}': resize failed ({err})")
                return False
    if _norm(image_abspath(img)) != _norm(target):
        img.filepath = bpy.path.relpath(target)
        img.reload()
    return True


def restore(img):
    src = original_path(img)
    if src is None or not img.get(STAMP_KEY):
        return False
    if _norm(image_abspath(img)) != _norm(src):
        img.filepath = bpy.path.relpath(src)
        img.reload()
    return True


def current_bucket(img):
    path = image_abspath(img)
    if is_proxy(path):
        parent = os.path.basename(os.path.dirname(path))
        for s, lab in LABEL.items():
            if lab == parent:
                return s
    orig = original_size(img)
    return bucket(max(orig)) if orig else None


def screen_size_px(scene, obj):
    """Approximate on-screen diameter of `obj` in pixels from the scene camera."""
    cam = scene.camera
    if cam is None:
        return None
    from mathutils import Vector
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    center = sum(corners, Vector()) / 8.0
    radius = max((c - center).length for c in corners)
    res_x = scene.render.resolution_x * scene.render.resolution_percentage / 100.0
    if cam.data.type == "ORTHO":
        return 2.0 * radius / cam.data.ortho_scale * res_x
    dist = (center - cam.matrix_world.translation).length
    if dist <= radius:
        return float("inf")
    return (radius / (dist * math.tan(cam.data.angle * 0.5))) * res_x


def objects_in_scope(context, scope):
    if scope == "ACTIVE":
        return [context.active_object] if context.active_object else []
    return list(context.selected_objects)


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SHIRO_OT_tex_scan(Operator):
    bl_idname = "shiro.tex_scan"
    bl_label = "Scan Textures"
    bl_description = ("Count textures and estimate VRAM. Unloaded images are measured "
                      "from their file headers, nothing gets loaded")

    def execute(self, context):
        global _stats
        _stats = gather_stats(context.scene, context.scene.shiro_tex_used_only)
        self.report({"INFO"}, f"{_stats['total']} textures, ~{human_bytes(_stats['vram'])}")
        return {"FINISHED"}


class SHIRO_OT_tex_print(Operator):
    bl_idname = "shiro.tex_print"
    bl_label = "Print Texture Stats"
    bl_description = "Print the last scan to the system console"

    def execute(self, context):
        s = _stats
        if s is None:
            self.report({"WARNING"}, "Scan first")
            return {"CANCELLED"}
        print("=" * 60)
        print(f"Texture Stats: {s['total']} textures, ~{human_bytes(s['vram'])} VRAM "
              f"(linked {s['linked']}, unknown size {s['unknown']})")
        for key, count in s["res"]:
            print(f"  {res_label(key):>22}: {count:>4}  {human_bytes(s['res_vram'][key])}")
        print("Heaviest objects by VRAM:")
        for name, (count, vram) in s["by_vram"]:
            print(f"  {human_bytes(vram):>10}  {count:>3} tex  {name}")
        print("=" * 60)
        return {"FINISHED"}


class SHIRO_OT_tex_apply(Operator):
    bl_idname = "shiro.tex_apply"
    bl_label = "Apply Texture Size"
    bl_description = "Use a copy of this texture resized to this resolution (longest side)"
    bl_options = {"REGISTER", "UNDO"}

    image_name: StringProperty()
    size: IntProperty()

    def execute(self, context):
        img = bpy.data.images.get(self.image_name)
        if img is None:
            return {"CANCELLED"}
        ok = apply_size(img, self.size, self.report)
        redraw(context)
        return {"FINISHED"} if ok else {"CANCELLED"}


class SHIRO_OT_tex_apply_all(Operator):
    bl_idname = "shiro.tex_apply_all"
    bl_label = "Apply Size to All"
    bl_description = "Resize every texture of the active / selected objects"
    bl_options = {"REGISTER", "UNDO"}

    size: IntProperty(default=1024)
    scope: EnumProperty(items=[("ACTIVE", "Active Object", ""),
                               ("SELECTED", "Selected Objects", "")], default="ACTIVE")

    def execute(self, context):
        images = set()
        for obj in objects_in_scope(context, self.scope):
            images |= object_images(obj)
        ok = sum(apply_size(img, self.size, self.report) for img in images)
        self.report({"INFO"}, f"[{LABEL[self.size]}] {ok}/{len(images)} textures")
        redraw(context)
        return {"FINISHED"}


class SHIRO_OT_tex_auto(Operator):
    bl_idname = "shiro.tex_auto"
    bl_label = "Auto Size by Camera"
    bl_description = ("Pick a texture size per object from how large it appears in the "
                      "scene camera. Textures shared by several objects use the largest need")
    bl_options = {"REGISTER", "UNDO"}

    texel_factor: bpy.props.FloatProperty(
        name="Texels per Pixel", default=1.5, min=0.25, max=8.0,
        description="Texture pixels per screen pixel across the object. Higher = sharper")
    min_size: EnumProperty(name="Minimum", items=[(str(s), LABEL[s], "") for s in SIZES],
                           default="256")

    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None and bool(context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        need = {}
        for obj in context.selected_objects:
            px = screen_size_px(context.scene, obj)
            if px is None:
                continue
            want = SIZES[0] if px == float("inf") else px * self.texel_factor
            size = next((s for s in reversed(SIZES) if s >= want), SIZES[0])
            size = max(size, int(self.min_size))
            for img in object_images(obj):
                need[img] = max(need.get(img, 0), size)
        ok = sum(apply_size(img, size, self.report) for img, size in need.items())
        self.report({"INFO"}, f"Auto-sized {ok}/{len(need)} textures")
        redraw(context)
        return {"FINISHED"}


class SHIRO_OT_tex_restore(Operator):
    bl_idname = "shiro.tex_restore"
    bl_label = "Restore Original"
    bl_description = "Point back to the original full-resolution file"
    bl_options = {"REGISTER", "UNDO"}

    image_name: StringProperty()
    scope: EnumProperty(items=[("IMAGE", "Image", ""), ("ACTIVE", "Active Object", ""),
                               ("SELECTED", "Selected Objects", ""), ("ALL", "All Images", "")],
                        default="IMAGE")

    def execute(self, context):
        if self.scope == "IMAGE":
            img = bpy.data.images.get(self.image_name)
            images = [img] if img else []
        elif self.scope == "ALL":
            images = list(bpy.data.images)
        else:
            images = set()
            for obj in objects_in_scope(context, self.scope):
                images |= object_images(obj)
        n = sum(restore(img) for img in images)
        self.report({"INFO"}, f"Restored {n} texture(s)")
        redraw(context)
        return {"FINISHED"}


class SHIRO_OT_tex_open_folder(Operator):
    bl_idname = "shiro.tex_open_folder"
    bl_label = "Open Output Folder"

    def execute(self, context):
        root = output_root()
        if not root or not os.path.isdir(root):
            self.report({"WARNING"}, "Nothing resized yet")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=root)
        return {"FINISHED"}


class SHIRO_OT_tex_clean(Operator):
    bl_idname = "shiro.tex_clean"
    bl_label = "Delete Unused Resized Copies"
    bl_description = ("Delete files in the Optimized Textures folder that no image uses. "
                      "This deletes files from disk")
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        root = output_root()
        if not root or not os.path.isdir(root):
            return {"CANCELLED"}
        used = {_norm(image_abspath(i)) for i in bpy.data.images if i.filepath}
        removed = 0
        for path in list(proxy_files()):
            if path not in used and os.path.isfile(path):
                os.remove(path)
                removed += 1
        invalidate_proxy_cache()
        self.report({"INFO"}, f"Deleted {removed} unused file(s)")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def draw_stats(layout, context):
    scene = context.scene
    row = layout.row(align=True)
    row.scale_y = 1.2
    row.operator("shiro.tex_scan", icon="FILE_REFRESH")
    row.operator("shiro.tex_print", text="", icon="CONSOLE")
    layout.prop(scene, "shiro_tex_used_only")
    s = _stats
    if s is None:
        layout.label(text="Press Scan", icon="INFO")
        return
    col = layout.column(align=True)
    col.label(text=f"{s['total']} textures  ·  ~{human_bytes(s['vram'])} VRAM", icon="TEXTURE")
    if s["linked"]:
        col.label(text=f"{s['linked']} linked", icon="LINKED")
    if s["unknown"]:
        col.label(text=f"{s['unknown']} unreadable / missing", icon="ERROR")
    if s["unused"]:
        col.label(text=f"{s['unused']} unused image data-blocks (Scene Doctor)",
                  icon="ORPHAN_DATA")
    total = max(s["vram"], 1)
    box = layout.box()
    box.label(text="By resolution", icon="IMAGE_DATA")
    for key, count in s["res"]:
        v = s["res_vram"][key]
        row = box.row(align=True)
        split = row.split(factor=0.42, align=True)
        split.label(text=f"{res_label(key)}  ×{count}")
        split.progress(factor=v / total, text=human_bytes(v))
    box = layout.box()
    box.label(text="Heaviest objects (VRAM)", icon="OBJECT_DATA")
    top = max((v for _n, (_c, v) in s["by_vram"]), default=1) or 1
    for name, (count, v) in s["by_vram"]:
        row = box.row(align=True)
        row.operator("shiro.select_object", text="", icon="RESTRICT_SELECT_OFF",
                     emboss=False).name = name
        split = row.split(factor=0.42, align=True)
        split.label(text=name)
        split.progress(factor=v / top, text=f"{human_bytes(v)} · {count} tex")


def draw_optimizer(layout, context):
    obj = context.active_object
    if obj is None:
        layout.label(text="No active object", icon="INFO")
        return
    if not bpy.data.filepath:
        layout.label(text="Save the .blend file first", icon="ERROR")
        return
    images = sorted(object_images(obj), key=lambda i: i.name)
    head = layout.row(align=True)
    head.label(text=f"{obj.name}: {len(images)} texture(s)", icon="OBJECT_DATA")
    head.operator("shiro.tex_open_folder", text="", icon="FILEBROWSER")
    head.operator("shiro.tex_clean", text="", icon="TRASH")
    if not images:
        return

    box = layout.box()
    box.label(text="All textures of", icon="EXPORT")
    row = box.row(align=True)
    row.prop(context.scene, "shiro_tex_scope", expand=True)
    row = box.row(align=True)
    for s in SIZES[:-1]:
        op = row.operator("shiro.tex_apply_all", text=LABEL[s])
        op.size, op.scope = s, context.scene.shiro_tex_scope
    row = box.row(align=True)
    row.operator("shiro.tex_auto", icon="VIEW_CAMERA")
    op = row.operator("shiro.tex_restore", text="Restore", icon="LOOP_BACK")
    op.scope = context.scene.shiro_tex_scope

    files = proxy_files()
    for img in images:
        orig = original_size(img)
        cur = current_bucket(img)
        box = layout.box()
        row = box.row(align=True)
        missing = original_path(img) is None
        row.label(text=img.name, icon="ERROR" if missing else "IMAGE_DATA")
        sub = row.row()
        sub.alignment = "RIGHT"
        sub.active = False
        sub.label(text=f"{orig[0]}×{orig[1]}" if orig else "?")
        if is_proxy(image_abspath(img)):
            op = row.operator("shiro.tex_restore", text="", icon="LOOP_BACK")
            op.image_name, op.scope = img.name, "IMAGE"
        if missing or img.packed_file is not None:
            box.label(text="Packed or missing: unpack / relink to resize", icon="ERROR")
            continue
        ceiling = bucket(max(orig)) if orig else 0
        row = box.row(align=True)
        for s in SIZES[:-1]:
            sub = row.row(align=True)
            sub.enabled = s <= ceiling
            saved = s < ceiling and _norm(proxy_path(img, s) or "") in files
            icon = "LAYER_ACTIVE" if s == cur else ("FILE_TICK" if saved else "NONE")
            op = sub.operator("shiro.tex_apply", text=LABEL[s], icon=icon, depress=s == cur)
            op.image_name, op.size = img.name, s


class SHIRO_PT_textures(Panel):
    bl_idname = "SHIRO_PT_textures"
    bl_label = "Textures"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pass


class SHIRO_PT_tex_stats(Panel):
    bl_idname = "SHIRO_PT_tex_stats"
    bl_label = "Stats & VRAM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_parent_id = "SHIRO_PT_textures"

    def draw(self, context):
        draw_stats(self.layout, context)


class SHIRO_PT_tex_optimizer(Panel):
    bl_idname = "SHIRO_PT_tex_optimizer"
    bl_label = "Resize"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_parent_id = "SHIRO_PT_textures"

    def draw(self, context):
        draw_optimizer(self.layout, context)


class SHIRO_PT_tex_optimizer_material(Panel):
    bl_idname = "SHIRO_PT_tex_optimizer_material"
    bl_label = "Texture Resize (Shiro)"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        draw_optimizer(self.layout, context)


classes = (
    SHIRO_OT_tex_scan, SHIRO_OT_tex_print, SHIRO_OT_tex_apply,
    SHIRO_OT_tex_apply_all, SHIRO_OT_tex_auto, SHIRO_OT_tex_restore, SHIRO_OT_tex_open_folder,
    SHIRO_OT_tex_clean, SHIRO_PT_textures, SHIRO_PT_tex_stats, SHIRO_PT_tex_optimizer,
    SHIRO_PT_tex_optimizer_material,
)


@persistent
def _on_load(_dummy=None):
    global _stats
    _stats = None
    invalidate_proxy_cache()
    imgheader.clear_cache()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shiro_tex_used_only = BoolProperty(
        name="Used by Scene Only", default=True,
        description="Only count images the scene actually uses (off: every image in the file)")
    bpy.types.Scene.shiro_tex_scope = EnumProperty(
        items=[("ACTIVE", "Active", ""), ("SELECTED", "Selected", "")], default="ACTIVE")
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    del bpy.types.Scene.shiro_tex_scope
    del bpy.types.Scene.shiro_tex_used_only
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _on_load()
