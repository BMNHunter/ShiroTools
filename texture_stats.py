bl_info = {
    "name": "Texture Stats",
    "author": "Milan",
    "version": (1, 3, 0),
    "blender": (5, 1, 0),
    "location": "Properties > Scene > Texture Stats",
    "description": "Counts textures (incl. linked), breaks down by resolution, estimates VRAM, ranks heaviest objects",
    "category": "System",
}

import bpy
from collections import defaultdict


# --- Cache -------------------------------------------------------------------

_CACHE = None

TOP_N = 10  # how many objects to list in the rankings


# --- Size reading WITHOUT forcing a full image load --------------------------

def read_dims_cheap(img):
    if not img.has_data:
        return None
    w, h = img.size[0], img.size[1]
    if w == 0 or h == 0:
        return None
    return (w, h)


# --- VRAM estimation ---------------------------------------------------------

def bytes_per_channel(image):
    if image.is_float:
        return 2
    return 1


def estimate_image_vram(image, w, h):
    base = w * h * 4 * bytes_per_channel(image)
    return base * 4 // 3  # +1/3 for the mipmap chain


def human_bytes(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024.0


# --- Image node collection ---------------------------------------------------

def iter_image_nodes(node_tree, _seen=None):
    """Yield every Image Texture node in a tree, descending into node groups.

    _seen guards against recursive/shared group trees being walked twice.
    """
    if node_tree is None:
        return
    if _seen is None:
        _seen = set()
    if node_tree.as_pointer() in _seen:
        return
    _seen.add(node_tree.as_pointer())

    for node in node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            yield node.image
        elif node.type == 'GROUP' and node.node_tree is not None:
            yield from iter_image_nodes(node.node_tree, _seen)


def object_images(obj):
    """Return the set of unique images referenced by an object's materials."""
    imgs = set()
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        for img in iter_image_nodes(mat.node_tree):
            imgs.add(img)
    return imgs


# --- Data gathering ----------------------------------------------------------

def gather_stats(force_load=False):
    res_counts = defaultdict(int)
    res_vram = defaultdict(int)
    total = 0
    linked = 0
    total_vram = 0
    not_loaded = 0

    # Per-image dimension cache so the object pass doesn't re-read sizes.
    dim_of = {}

    def get_dims(img):
        ptr = img.as_pointer()
        if ptr in dim_of:
            return dim_of[ptr]
        if force_load:
            w, h = img.size[0], img.size[1]
            dims = (w, h) if (w and h) else None
        else:
            dims = read_dims_cheap(img)
        dim_of[ptr] = dims
        return dims

    # --- global image pass ---
    for img in bpy.data.images:
        if img.source not in {'FILE', 'SEQUENCE', 'MOVIE', 'TILED'}:
            continue
        dims = get_dims(img)
        if dims is None:
            not_loaded += 1
            continue
        w, h = dims
        total += 1
        if img.library is not None:
            linked += 1
        key = (w, h)
        res_counts[key] += 1
        v = estimate_image_vram(img, w, h)
        res_vram[key] += v
        total_vram += v

    ordered = sorted(res_counts.keys(), key=lambda k: k[0] * k[1], reverse=True)

    # --- per-object pass (naive: each object charged for what it references) ---
    obj_by_count = []  # (name, texture_count)
    obj_by_vram = []   # (name, vram_bytes, loaded_count)

    for obj in bpy.context.scene.objects:
        imgs = object_images(obj)
        if not imgs:
            continue
        count = len(imgs)
        vram = 0
        loaded = 0
        for img in imgs:
            dims = get_dims(img)
            if dims is None:
                continue
            loaded += 1
            vram += estimate_image_vram(img, dims[0], dims[1])
        obj_by_count.append((obj.name, count))
        obj_by_vram.append((obj.name, vram, loaded))

    obj_by_count.sort(key=lambda t: t[1], reverse=True)
    obj_by_vram.sort(key=lambda t: t[1], reverse=True)

    return {
        "total": total,
        "linked": linked,
        "not_loaded": not_loaded,
        "forced": force_load,
        "ordered": ordered,
        "res_counts": res_counts,
        "res_vram": res_vram,
        "total_vram": total_vram,
        "obj_by_count": obj_by_count[:TOP_N],
        "obj_by_vram": obj_by_vram[:TOP_N],
    }


# --- Operators ---------------------------------------------------------------

class TEXSTATS_OT_refresh(bpy.types.Operator):
    bl_idname = "texstats.refresh"
    bl_label = "Refresh (loaded only)"
    bl_description = ("Scan only textures already in memory. Safe and cheap -- "
                     "will not load anything or spike RAM")

    def execute(self, context):
        global _CACHE
        _CACHE = gather_stats(force_load=False)
        self.report({'INFO'},
                    f"{_CACHE['total']} loaded textures, "
                    f"{human_bytes(_CACHE['total_vram'])} VRAM")
        return {'FINISHED'}


class TEXSTATS_OT_refresh_full(bpy.types.Operator):
    bl_idname = "texstats.refresh_full"
    bl_label = "Full Scan (loads everything!)"
    bl_description = ("Force-load EVERY texture to read its size. This can use "
                     "many GB of RAM and may crash on heavy scenes. Use only "
                     "when you accept that cost")
    bl_options = {'REGISTER', 'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global _CACHE
        _CACHE = gather_stats(force_load=True)
        self.report({'WARNING'},
                    f"Full scan: {_CACHE['total']} textures, "
                    f"{human_bytes(_CACHE['total_vram'])} VRAM")
        return {'FINISHED'}


class TEXSTATS_OT_print(bpy.types.Operator):
    bl_idname = "texstats.print_console"
    bl_label = "Print Texture Stats to Console"
    bl_description = "Dump the cached breakdown to the system console"

    def execute(self, context):
        stats = _CACHE
        if stats is None:
            self.report({'WARNING'}, "Nothing cached -- press Refresh first")
            return {'CANCELLED'}
        print("=" * 50)
        mode = "FULL SCAN" if stats["forced"] else "loaded-only"
        print(f"Texture Stats ({mode})")
        print(f"Total: {stats['total']} (linked: {stats['linked']}, "
              f"not loaded: {stats['not_loaded']})")
        print(f"Estimated VRAM: {human_bytes(stats['total_vram'])}")
        print("-" * 50)
        for key in stats["ordered"]:
            w, h = key
            print(f"  {w} x {h}: {stats['res_counts'][key]} tex, "
                  f"{human_bytes(stats['res_vram'][key])}")
        print("-" * 50)
        print("Top objects by texture count:")
        for name, c in stats["obj_by_count"]:
            print(f"  {c:>4}  {name}")
        print("Top objects by VRAM (loaded only):")
        for name, v, loaded in stats["obj_by_vram"]:
            print(f"  {human_bytes(v):>11}  {name}  ({loaded} loaded)")
        print("=" * 50)
        self.report({'INFO'}, "Printed to console")
        return {'FINISHED'}


# --- UI ----------------------------------------------------------------------

class TEXSTATS_PT_panel(bpy.types.Panel):
    bl_label = "Texture Stats"
    bl_idname = "TEXSTATS_PT_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout

        row = layout.row(align=True)
        row.operator("texstats.refresh", icon='FILE_REFRESH')
        row.operator("texstats.print_console", text="", icon='CONSOLE')
        layout.operator("texstats.refresh_full", icon='ERROR')

        stats = _CACHE
        if stats is None:
            layout.label(text="Press Refresh to scan.", icon='INFO')
            return

        col = layout.column(align=True)
        mode = "full scan" if stats["forced"] else "loaded only"
        col.label(text=f"Total textures: {stats['total']}  ({mode})", icon='TEXTURE')
        if stats["linked"]:
            col.label(text=f"  of which linked: {stats['linked']}", icon='LINKED')
        if stats["not_loaded"]:
            col.label(text=f"  not loaded (skipped): {stats['not_loaded']}", icon='CHECKBOX_DEHLT')
        col.label(text=f"Est. VRAM: {human_bytes(stats['total_vram'])}", icon='MEMORY')

        layout.separator()
        layout.label(text="Resolution breakdown:")

        box = layout.box()
        if not stats["ordered"]:
            box.label(text="No loaded textures found.")
        else:
            header = box.row()
            header.label(text="Resolution")
            header.label(text="Count")
            header.label(text="VRAM")
            for key in stats["ordered"]:
                w, h = key
                r = box.row()
                r.label(text=f"{w} x {h}")
                r.label(text=str(stats["res_counts"][key]))
                r.label(text=human_bytes(stats["res_vram"][key]))

        layout.separator()
        layout.label(text="Heaviest objects by texture count:")
        cbox = layout.box()
        if not stats["obj_by_count"]:
            cbox.label(text="No textured objects in scene.")
        else:
            for name, c in stats["obj_by_count"]:
                r = cbox.row()
                r.label(text=name, icon='OBJECT_DATA')
                r.label(text=f"{c} tex")

        layout.label(text="Heaviest objects by VRAM (loaded only):")
        vbox = layout.box()
        if not stats["obj_by_vram"]:
            vbox.label(text="No textured objects in scene.")
        else:
            for name, v, loaded in stats["obj_by_vram"]:
                r = vbox.row()
                r.label(text=name, icon='OBJECT_DATA')
                r.label(text=human_bytes(v))


classes = (
    TEXSTATS_OT_refresh,
    TEXSTATS_OT_refresh_full,
    TEXSTATS_OT_print,
    TEXSTATS_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _CACHE
    _CACHE = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
