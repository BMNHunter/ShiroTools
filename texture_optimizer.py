bl_info = {
    "name": "Texture Optimizer",
    "author": "Milan",
    "version": (4, 0, 0),
    "blender": (3, 0, 0),
    "location": "Properties > Material > Texture Optimizer",
    "description": "Resize, save, and apply textures per-object with one click",
    "category": "Material",
}

import bpy
import os
import subprocess
import sys
from bpy.types import Panel, Operator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STANDARD_SIZES = [8192, 4096, 2048, 1024, 512, 256]
SIZE_LABELS = {8192: "8K", 4096: "4K", 2048: "2K", 1024: "1K", 512: "512", 256: "256"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".tiff", ".tif", ".bmp"}
STAMP_KEY = "texopt_original_filepath"


# ---------------------------------------------------------------------------
# Pillow install
# ---------------------------------------------------------------------------

def ensure_pillow():
    try:
        from PIL import Image
        return True
    except ImportError:
        pass

    python = sys.executable

    try:
        subprocess.check_call(
            [python, "-m", "ensurepip", "--upgrade"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    try:
        subprocess.check_call(
            [python, "-m", "pip", "install", "--upgrade", "pip",
             "--quiet", "--no-warn-script-location"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    for extra_args in [[], ["--user"]]:
        try:
            subprocess.check_call(
                [python, "-m", "pip", "install", "Pillow",
                 "--quiet", "--no-warn-script-location"] + extra_args
            )
            import importlib, site
            importlib.reload(site)
            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
            from PIL import Image
            return True
        except Exception:
            continue

    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_optimized_root():
    """Return the 'Optimized Textures' folder next to the .blend file."""
    if not bpy.data.filepath:
        return None
    return os.path.join(os.path.dirname(bpy.data.filepath), "Optimized Textures")


def safe_name(name):
    return bpy.path.clean_name(name)


def is_proxy_path(filepath):
    """Return True if the path lives inside the Optimized Textures folder."""
    root = get_optimized_root()
    if not root or not filepath:
        return False
    return os.path.normpath(filepath).startswith(os.path.normpath(root))


# ---------------------------------------------------------------------------
# Original-path tracking
#
# The key idea: we NEVER swap image datablocks. We repoint the same
# datablock's .filepath to the proxy and .reload(). That means the custom
# property (stamp) always stays on the same object.
#
# get_original() — read the stamp, safe to call from draw()
# ensure_original() — stamp the current filepath if not stamped yet
#                      (call from operators, before repointing)
# ---------------------------------------------------------------------------

def get_original(img):
    """Return the stamped original filepath, or None."""
    val = img.get(STAMP_KEY)
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode("utf-8")
    if val and os.path.isfile(val):
        return val
    return None


def ensure_original(img):
    """
    If this image hasn't been stamped yet, stamp its current filepath
    as the original. Returns the original path (stamped or current).
    Never overwrites an existing valid stamp.
    Never stamps a proxy path.
    """
    # Already stamped and valid — return it
    existing = get_original(img)
    if existing:
        return existing

    # Read current filepath
    current = bpy.path.abspath(img.filepath) if img.filepath else ""
    if not current or not os.path.isfile(current):
        return None

    # Don't stamp proxy paths
    if is_proxy_path(current):
        return None

    img[STAMP_KEY] = current
    return current


# ---------------------------------------------------------------------------
# Image / object queries
# ---------------------------------------------------------------------------

def get_object_images(obj):
    """
    Return dict {img.name: bpy.types.Image} for all Image Texture nodes on obj.
    Deduplicated — same image referenced by multiple nodes counted once.
    """
    images = {}
    if obj is None:
        return images
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                img = node.image
                if img.name not in images and (img.filepath or img.packed_file):
                    images[img.name] = img
    return images


def get_source_path(img):
    """
    Return the absolute path to the original source file.
    Checks stamp first, then falls back to current filepath
    (only if it's not a proxy).
    """
    stamped = get_original(img)
    if stamped:
        return stamped
    current = bpy.path.abspath(img.filepath) if img.filepath else ""
    if current and os.path.isfile(current) and not is_proxy_path(current):
        return current
    return None


def get_original_dimensions(img):
    """
    Return (width, height) of the original source file on disk via Pillow.
    Falls back to the currently loaded size if Pillow can't read it.
    """
    src = get_source_path(img)
    if src:
        ext = os.path.splitext(src)[1].lower()
        if ext not in (".exr", ".hdr"):
            try:
                from PIL import Image as PilImage
                with PilImage.open(src) as pil_img:
                    return pil_img.size
            except Exception:
                pass
    return img.size[0], img.size[1]


def size_to_bucket(px):
    """Snap a pixel count down to the nearest standard size."""
    for s in STANDARD_SIZES:
        if px >= s:
            return s
    return 0


def max_known_size(img):
    """
    The largest resolution bucket this image can be served at.
    Based on the original file's actual pixel dimensions.
    """
    orig_w, orig_h = get_original_dimensions(img)
    bucket = size_to_bucket(max(orig_w, orig_h))
    if bucket > 0:
        return bucket

    # Fallback: whatever is loaded in Blender right now
    return size_to_bucket(max(img.size[0], img.size[1]))


def proxy_path_for(img, obj_name, size_int):
    """Expected output path for a proxy at a given size."""
    root = get_optimized_root()
    if root is None:
        return None
    src = get_source_path(img)
    if src is None:
        return None
    ext = os.path.splitext(src)[1]
    folder = os.path.join(root, safe_name(obj_name), SIZE_LABELS[size_int])
    return os.path.join(folder, safe_name(img.name) + ext)


def proxy_exists(img, obj_name, size_int):
    p = proxy_path_for(img, obj_name, size_int)
    return p is not None and os.path.isfile(p)


def resize_and_save(src_path, dst_path, target_w, target_h):
    """Resize src_path to (target_w, target_h) and save to dst_path."""
    from PIL import Image
    ext = os.path.splitext(src_path)[1].lower()
    if ext in (".exr", ".hdr"):
        return False, "EXR/HDR not supported"
    pil_img = Image.open(src_path)
    resized = pil_img.resize((target_w, target_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    resized.save(dst_path)
    return True, ""


# ---------------------------------------------------------------------------
# Core apply logic (shared by single and apply-all operators)
# ---------------------------------------------------------------------------

def apply_size_to_image(img, obj, target_size, report_fn):
    """
    Resize + repoint a single image to the target size.
    Instead of swapping datablocks, we repoint img.filepath and reload.
    Returns True on success, False on skip/error.
    """
    if not bpy.data.filepath:
        report_fn({"ERROR"}, "Save your .blend file first.")
        return False

    # Stamp the original path before we touch anything
    ensure_original(img)

    src_path = get_source_path(img)
    dst_path = proxy_path_for(img, obj.name, target_size)

    if dst_path is None:
        report_fn({"ERROR"}, f"Cannot resolve path for '{img.name}' — packed or missing.")
        return False

    # Check ceiling
    if target_size > max_known_size(img):
        report_fn({"WARNING"}, f"'{img.name}' — target above max known size, skipping.")
        return False

    # Step 1: Resize and save proxy if it doesn't exist yet
    if not os.path.isfile(dst_path):
        if src_path is None:
            report_fn({"ERROR"}, f"'{img.name}' is packed or missing — unpack first.")
            return False

        if not ensure_pillow():
            report_fn({"ERROR"}, "Could not install Pillow.")
            return False

        # Use original dimensions for correct aspect ratio
        orig_w, orig_h = get_original_dimensions(img)
        longest = max(orig_w, orig_h)
        if longest == 0:
            report_fn({"ERROR"}, f"'{img.name}' has zero size.")
            return False

        scale = target_size / longest
        new_w = max(1, round(orig_w * scale))
        new_h = max(1, round(orig_h * scale))

        ok, msg = resize_and_save(src_path, dst_path, new_w, new_h)
        if not ok:
            report_fn({"WARNING"}, f"Could not resize '{img.name}': {msg}")
            return False

    # Step 2: Check if this is the original size — repoint back to original
    orig_w, orig_h = get_original_dimensions(img)
    orig_bucket = size_to_bucket(max(orig_w, orig_h))
    if target_size == orig_bucket and src_path:
        target_path = src_path
    else:
        target_path = dst_path

    # Step 3: Repoint the SAME image datablock and reload
    # (This is the key difference — no datablock swap, so stamp is preserved)
    img.filepath = bpy.path.relpath(target_path)
    img.reload()

    return True


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class TEXOPT_OT_ApplySize(Operator):
    """Resize if needed, then apply this resolution to the texture."""
    bl_idname = "texopt.apply_size"
    bl_label = "Apply Texture Size"
    bl_description = "Resize if needed, then apply this resolution"

    image_name: bpy.props.StringProperty()
    target_size: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "No active object.")
            return {"CANCELLED"}

        images = get_object_images(obj)
        img = images.get(self.image_name)
        if img is None:
            self.report({"ERROR"}, f"Image '{self.image_name}' not found on object.")
            return {"CANCELLED"}

        ok = apply_size_to_image(img, obj, self.target_size, self.report)

        for area in context.screen.areas:
            area.tag_redraw()

        return {"FINISHED"} if ok else {"CANCELLED"}


class TEXOPT_OT_ApplyAllSizes(Operator):
    """Apply a target size to every texture on the object at once."""
    bl_idname = "texopt.apply_all"
    bl_label = "Apply Size to All"
    bl_description = "Resize and apply this resolution to all textures on the object"

    target_size: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "No active object.")
            return {"CANCELLED"}

        images = get_object_images(obj)
        if not images:
            self.report({"WARNING"}, "No textures found on this object.")
            return {"CANCELLED"}

        applied, skipped = [], []

        for img_name, img in images.items():
            ok = apply_size_to_image(img, obj, self.target_size, self.report)
            if ok:
                applied.append(img_name)
            else:
                skipped.append(img_name)

        size_label = SIZE_LABELS[self.target_size]
        report = f"[{size_label}] Applied {len(applied)}"
        if skipped:
            report += f", skipped {len(skipped)}: {', '.join(skipped)}"
        self.report({"INFO"}, report)

        for area in context.screen.areas:
            area.tag_redraw()

        return {"FINISHED"}


class TEXOPT_OT_RestoreOriginal(Operator):
    """Restore the original full-resolution texture."""
    bl_idname = "texopt.restore_original"
    bl_label = "Restore Original"
    bl_description = "Repoint this texture back to its original file"

    image_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "No active object.")
            return {"CANCELLED"}

        images = get_object_images(obj)
        img = images.get(self.image_name)
        if img is None:
            self.report({"ERROR"}, f"Image '{self.image_name}' not found.")
            return {"CANCELLED"}

        orig = get_original(img)
        if not orig:
            self.report({"WARNING"}, "No original path stored for this image.")
            return {"CANCELLED"}

        img.filepath = bpy.path.relpath(orig)
        img.reload()

        self.report({"INFO"}, f"Restored original for '{self.image_name}'")
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class TEXOPT_OT_OpenFolder(Operator):
    bl_idname = "texopt.open_folder"
    bl_label = "Open Output Folder"
    bl_description = "Open the Optimized Textures folder in your file explorer"

    def execute(self, context):
        root = get_optimized_root()
        if root and os.path.isdir(root):
            if sys.platform == "win32":
                os.startfile(root)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", root])
            else:
                subprocess.Popen(["xdg-open", root])
        else:
            self.report({"WARNING"}, "Output folder doesn't exist yet.")
        return {"FINISHED"}


class TEXOPT_OT_RescanMaterials(Operator):
    """Rescan all material slots on the active object."""
    bl_idname = "texopt.rescan_materials"
    bl_label = "Rescan Materials"
    bl_description = "Re-scan all materials on this object and refresh the texture list"

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "No active object.")
            return {"CANCELLED"}

        for slot in obj.material_slots:
            mat = slot.material
            if mat and mat.node_tree:
                mat.node_tree.update_tag()

        for area in context.screen.areas:
            area.tag_redraw()

        count = len(get_object_images(obj))
        self.report({"INFO"}, f"Rescanned '{obj.name}' — {count} texture(s) found.")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class TEXOPT_PT_MainPanel(Panel):
    bl_label = "Texture Optimizer"
    bl_idname = "TEXOPT_PT_main"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if obj is None:
            layout.label(text="No object selected.", icon="INFO")
            return

        if not bpy.data.filepath:
            layout.label(text="Save your .blend file to use this addon.", icon="ERROR")
            return

        images = get_object_images(obj)

        # Header
        header = layout.box()
        row = header.row()
        row.label(text=obj.name, icon="OBJECT_DATA")
        row.label(text=f"{len(images)} texture(s)", icon="IMAGE_DATA")
        row.operator("texopt.rescan_materials", text="", icon="FILE_REFRESH")

        if not images:
            layout.label(text="No Image Texture nodes found.", icon="ERROR")
            return

        layout.separator(factor=0.5)

        # Apply-all strip
        all_box = layout.box()
        all_box.label(text="Apply to all textures:", icon="EXPORT")
        all_row = all_box.row(align=True)
        for size in STANDARD_SIZES:
            op = all_row.operator("texopt.apply_all", text=SIZE_LABELS[size])
            op.target_size = size

        layout.separator(factor=0.5)

        # Per-texture list
        layout.label(text="Per-texture:", icon="TEXTURE")

        for img_name, img in images.items():
            w, h = img.size[0], img.size[1]
            src_path = get_source_path(img)
            ceiling = max_known_size(img)
            current_bucket = size_to_bucket(max(w, h))

            box = layout.box()

            # Row 1: name + current resolution + restore button
            name_row = box.row()
            name_row.label(
                text=img_name,
                icon="IMAGE_DATA" if src_path else "ERROR",
            )
            name_row.label(text=f"{w} × {h}")

            # Show restore button if we have an original stamped
            # and the current filepath is a proxy
            current_abs = bpy.path.abspath(img.filepath) if img.filepath else ""
            if get_original(img) and is_proxy_path(current_abs):
                restore_op = name_row.operator(
                    "texopt.restore_original",
                    text="",
                    icon="LOOP_BACK",
                )
                restore_op.image_name = img_name

            # Row 2: size buttons
            btn_row = box.row(align=True)
            for size in STANDARD_SIZES:
                size_label = SIZE_LABELS[size]
                is_available = size <= ceiling
                already_saved = proxy_exists(img, obj.name, size)
                is_active = (current_bucket == size)

                sub = btn_row.row(align=True)
                sub.enabled = is_available

                if is_active:
                    icon = "LAYER_ACTIVE"
                elif already_saved:
                    icon = "FILE_REFRESH"
                else:
                    icon = "FILE_IMAGE"

                op = sub.operator("texopt.apply_size", text=size_label, icon=icon)
                op.image_name = img_name
                op.target_size = size

            # Warning for packed/missing
            if src_path is None:
                box.label(text="Packed or missing — unpack before resizing", icon="ERROR")

        layout.separator(factor=0.5)

        # Footer
        root = get_optimized_root()
        footer = layout.box()
        footer.label(text="Output root:", icon="FILE_FOLDER")
        display_path = root if len(root) < 58 else "…" + root[-55:]
        footer.label(text=display_path)
        if root and os.path.isdir(root):
            footer.operator("texopt.open_folder", icon="FILEBROWSER")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    TEXOPT_OT_ApplySize,
    TEXOPT_OT_ApplyAllSizes,
    TEXOPT_OT_RestoreOriginal,
    TEXOPT_OT_OpenFolder,
    TEXOPT_OT_RescanMaterials,
    TEXOPT_PT_MainPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
