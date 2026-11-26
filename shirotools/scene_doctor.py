"""Scene Doctor: find and fix the usual .blend file rot.

* missing files (images, libraries, sounds, clips, fonts, caches)
* the same image file loaded as several data-blocks
* "Name.001" materials / node groups / meshes that are identical to "Name"
  (compared by content fingerprint, so only true duplicates are merged)
* orphan data, empty material slots, meshes without material
* absolute paths, packed files, objects that render but are hidden in the viewport
"""

import hashlib
import os
import re
from collections import defaultdict

import bpy
from bpy.props import StringProperty
from bpy.types import Operator, Panel

from .common import SEVERITY_ICON, TAB, human_bytes, image_abspath

_issues = None
NUMBERED = re.compile(r"^(.*)\.(\d{3,})$")
DEDUPE_TYPES = ("node_groups", "materials", "meshes")  # node groups first: materials use them


class Issue:
    __slots__ = ("kind", "severity", "title", "items", "fix", "fix_label")

    def __init__(self, kind, severity, title, items, fix=None, fix_label=""):
        self.kind, self.severity, self.title = kind, severity, title
        self.items, self.fix, self.fix_label = items, fix, fix_label


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _missing(path):
    return bool(path) and not os.path.exists(path)


def missing_files():
    out = []
    for img in bpy.data.images:
        if img.packed_file is not None or img.source not in {"FILE", "SEQUENCE", "MOVIE",
                                                             "TILED"}:
            continue
        path = image_abspath(img)
        if img.source == "TILED" and path:
            path = path.replace("<UDIM>", str(img.tiles[0].number if img.tiles else 1001))
        if _missing(path):
            out.append(f"Image: {img.name}")
    for lib in bpy.data.libraries:
        if lib.is_missing or _missing(bpy.path.abspath(lib.filepath)):
            out.append(f"Library: {lib.name}")
    for coll, label in ((bpy.data.sounds, "Sound"), (bpy.data.movieclips, "Clip"),
                        (bpy.data.cache_files, "Cache")):
        for idb in coll:
            if getattr(idb, "packed_file", None) is None and \
                    _missing(bpy.path.abspath(idb.filepath, library=idb.library)):
                out.append(f"{label}: {idb.name}")
    for font in bpy.data.fonts:
        if font.filepath != "<builtin>" and font.packed_file is None and \
                _missing(bpy.path.abspath(font.filepath)):
            out.append(f"Font: {font.name}")
    return out


def duplicate_image_files():
    groups = defaultdict(list)
    for img in bpy.data.images:
        if img.library is None and img.source in {"FILE", "TILED"} and img.filepath:
            groups[(os.path.normcase(image_abspath(img)), img.colorspace_settings.name)].append(img)
    return [imgs for imgs in groups.values() if len(imgs) > 1]


SIMPLE = {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}
UI_ONLY = {"name", "name_full", "location", "location_absolute", "width", "height",
           "dimensions", "select", "hide", "show_options", "show_preview", "show_texture",
           "label", "color", "use_custom_color", "warning_propagation", "color_tag",
           "description"}


def _value(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, bpy.types.ID):
        return ("ID", v.name)
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    if isinstance(v, set):
        return tuple(sorted(v))
    try:
        return tuple(_value(x) for x in v)
    except TypeError:
        return repr(v)


def _props(struct):
    out = []
    for p in struct.bl_rna.properties:
        if p.identifier in UI_ONLY or p.is_readonly or p.type not in SIMPLE | {"POINTER"}:
            continue
        try:
            v = getattr(struct, p.identifier)
        except (AttributeError, RuntimeError):
            continue
        if p.type == "POINTER" and not isinstance(v, bpy.types.ID) and v is not None:
            continue
        out.append((p.identifier, _value(v)))
    return tuple(out)


def _tree(nt):
    if nt is None:
        return None
    nodes = []
    for n in sorted(nt.nodes, key=lambda n: n.name):
        socks = tuple((s.identifier, _value(s.default_value)) for s in n.inputs
                      if hasattr(s, "default_value") and not s.is_linked)
        nodes.append((n.bl_idname, n.name, _props(n), socks))
    links = sorted((l.from_node.name, l.from_socket.identifier, l.to_node.name,
                    l.to_socket.identifier) for l in nt.links)
    iface = []
    if hasattr(nt, "interface"):
        for item in nt.interface.items_tree:
            iface.append((item.item_type, item.name, getattr(item, "in_out", ""),
                          getattr(item, "socket_type", ""),
                          _value(getattr(item, "default_value", None))))
    return (tuple(nodes), tuple(links), tuple(iface))


def _mesh(me):
    import numpy as np
    h = hashlib.sha1()
    for coll, attr, width, dt in ((me.vertices, "co", 3, np.float32),
                                  (me.edges, "vertices", 2, np.int32),
                                  (me.loops, "vertex_index", 1, np.int32),
                                  (me.polygons, "loop_total", 1, np.int32),
                                  (me.polygons, "material_index", 1, np.int32)):
        buf = np.empty(len(coll) * width, dtype=dt)
        coll.foreach_get(attr, buf)
        h.update(buf.tobytes())
    for uv in me.uv_layers:
        buf = np.empty(len(me.loops) * 2, dtype=np.float32)
        uv.uv.foreach_get("vector", buf)
        h.update(uv.name.encode() + buf.tobytes())
    h.update(repr([m.name if m else None for m in me.materials]).encode())
    h.update(repr(sorted(a.name for a in me.attributes)).encode())
    return h.hexdigest()


def fingerprint(idb):
    """Content hash that ignores the data-block name. None = can't compare."""
    try:
        if isinstance(idb, bpy.types.Material):
            data = (_props(idb), _tree(idb.node_tree))
        elif isinstance(idb, bpy.types.NodeTree):
            data = (idb.bl_idname, _tree(idb))
        elif isinstance(idb, bpy.types.Mesh):
            data = _mesh(idb)
        else:
            return None
    except (AttributeError, RuntimeError, TypeError):
        return None
    return hashlib.sha1(repr(data).encode()).hexdigest()


def identical_numbered_copies():
    """[(original, [copies])] where every copy hashes equal to the original."""
    out = []
    for attr in DEDUPE_TYPES:
        coll = getattr(bpy.data, attr)
        for idb in coll:
            if idb.library is not None:
                continue
            m = NUMBERED.match(idb.name)
            if not m:
                continue
            base = coll.get(m.group(1))
            if base is None or base.library is not None or base == idb:
                continue
            hb = fingerprint(base)
            if hb is not None and hb == fingerprint(idb):
                out.append((base, idb))
    merged = defaultdict(list)
    for base, copy in out:
        merged[base].append(copy)
    return list(merged.items())


def orphans():
    out = []
    for attr in ("meshes", "materials", "images", "node_groups", "textures", "actions",
                 "curves", "armatures", "cameras", "lights", "worlds", "particles",
                 "grease_pencils", "brushes"):
        coll = getattr(bpy.data, attr, None)
        for idb in coll or ():
            if idb.users == 0 and not idb.use_fake_user:
                out.append(f"{attr[:-1] if attr.endswith('s') else attr}: {idb.name}")
    return out


def run_scan(scene):
    issues = []
    miss = missing_files()
    if miss:
        issues.append(Issue("MISSING", "ERROR", f"{len(miss)} missing file(s)", miss,
                            "MISSING", "Find Missing Files..."))
    dups = duplicate_image_files()
    if dups:
        issues.append(Issue("DUP_IMAGES", "WARNING",
                            f"{sum(len(d) - 1 for d in dups)} image(s) load a file twice",
                            [", ".join(i.name for i in d) for d in dups], "DUP_IMAGES", "Merge"))
    copies = identical_numbered_copies()
    if copies:
        issues.append(Issue("DUP_DATA", "WARNING",
                            f"{sum(len(c) for _b, c in copies)} identical .001 copies",
                            [f"{b.name} ← {', '.join(c.name for c in cs)}" for b, cs in copies],
                            "DUP_DATA", "Merge"))
    orph = orphans()
    if orph:
        issues.append(Issue("ORPHANS", "INFO", f"{len(orph)} orphan data-block(s)", orph,
                            "ORPHANS", "Purge"))
    empty = [o.name for o in scene.objects if any(s.material is None for s in o.material_slots)]
    if empty:
        issues.append(Issue("EMPTY_SLOTS", "WARNING", f"{len(empty)} object(s) with empty "
                            "material slots", empty, "EMPTY_SLOTS", "Remove Empty Slots"))
    nomat = [o.name for o in scene.objects if o.type == "MESH" and not any(
        s.material for s in o.material_slots)]
    if nomat:
        issues.append(Issue("NO_MATERIAL", "INFO", f"{len(nomat)} mesh(es) without material",
                            nomat, "SELECT", "Select"))
    absolute = [i.name for i in bpy.data.images if i.filepath and not i.filepath.startswith("//")
                and i.packed_file is None and i.library is None and i.source == "FILE"]
    if absolute and bpy.data.filepath:
        issues.append(Issue("ABSOLUTE", "INFO", f"{len(absolute)} absolute image path(s)",
                            absolute, "ABSOLUTE", "Make Relative"))
    packed = [(i.name, i.packed_file.size) for i in bpy.data.images if i.packed_file]
    if packed:
        size = sum(s for _n, s in packed)
        issues.append(Issue("PACKED", "INFO", f"{len(packed)} packed image(s), "
                            f"{human_bytes(size)}", [n for n, _s in packed]))
    hidden = [o.name for o in scene.objects if o.hide_get() and not o.hide_render
              and o.type in {"MESH", "CURVE", "CURVES", "VOLUME", "POINTCLOUD", "FONT"}]
    if hidden:
        issues.append(Issue("HIDDEN_RENDER", "INFO", f"{len(hidden)} hidden object(s) still "
                            "render", hidden, "SELECT", "Select"))
    return issues


# ---------------------------------------------------------------------------
# fixes
# ---------------------------------------------------------------------------

def merge_into(keep, others):
    for other in others:
        if other == keep:
            continue
        other.user_remap(keep)
        coll = {bpy.types.Material: bpy.data.materials, bpy.types.Image: bpy.data.images,
                bpy.types.Mesh: bpy.data.meshes, bpy.types.Texture: bpy.data.textures,
                bpy.types.Brush: bpy.data.brushes}
        target = next((c for t, c in coll.items() if isinstance(other, t)), None)
        if target is None and isinstance(other, bpy.types.NodeTree):
            target = bpy.data.node_groups
        if target is not None and other.users == 0:
            target.remove(other)


class SHIRO_OT_doctor_scan(Operator):
    bl_idname = "shiro.doctor_scan"
    bl_label = "Scan Scene"
    bl_description = "Look for missing files, duplicates, orphans and other problems"

    def execute(self, context):
        global _issues
        _issues = run_scan(context.scene)
        self.report({"INFO"}, f"{len(_issues)} finding(s)")
        return {"FINISHED"}


class SHIRO_OT_doctor_fix(Operator):
    bl_idname = "shiro.doctor_fix"
    bl_label = "Fix"
    bl_options = {"REGISTER", "UNDO"}

    kind: StringProperty()
    index: bpy.props.IntProperty(default=-1)

    @classmethod
    def description(cls, context, props):
        return {
            "MISSING": "Search a folder for the missing files",
            "DUP_IMAGES": "Point every user at one image data-block per file",
            "DUP_DATA": "Replace identical .001 copies by the original",
            "ORPHANS": "Delete data-blocks nothing uses (recursive)",
            "EMPTY_SLOTS": "Remove empty material slots",
            "ABSOLUTE": "Make all file paths relative to the .blend",
            "SELECT": "Select these objects",
        }.get(props.kind, "")

    def execute(self, context):
        k = self.kind
        if k == "MISSING":
            bpy.ops.file.find_missing_files("INVOKE_DEFAULT")
            return {"FINISHED"}
        if k == "DUP_IMAGES":
            for imgs in duplicate_image_files():
                merge_into(imgs[0], imgs[1:])
        elif k == "DUP_DATA":
            for _round in range(3):  # merged node groups can make materials identical
                found = identical_numbered_copies()
                if not found:
                    break
                for base, copies in found:
                    merge_into(base, copies)
        elif k == "ORPHANS":
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
        elif k == "EMPTY_SLOTS":
            for obj in context.scene.objects:
                for i in reversed(range(len(obj.material_slots))):
                    if obj.material_slots[i].material is None:
                        obj.active_material_index = i
                        with context.temp_override(object=obj, active_object=obj):
                            bpy.ops.object.material_slot_remove()
        elif k == "ABSOLUTE":
            bpy.ops.file.make_paths_relative()
        elif k == "SELECT" and _issues and 0 <= self.index < len(_issues):
            names = set(_issues[self.index].items)
            for obj in context.view_layer.objects:
                obj.select_set(obj.name in names)
                if obj.name in names:
                    context.view_layer.objects.active = obj
            return {"FINISHED"}
        bpy.ops.shiro.doctor_scan()
        return {"FINISHED"}


class SHIRO_PT_doctor(Panel):
    bl_idname = "SHIRO_PT_doctor"
    bl_label = "Scene Doctor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.scale_y = 1.3
        row.operator("shiro.doctor_scan", icon="VIEWZOOM")
        if _issues is None:
            return
        if not _issues:
            layout.label(text="All clean", icon="CHECKMARK")
            return
        for i, issue in enumerate(_issues):
            box = layout.box()
            row = box.row(align=True)
            row.alert = issue.severity == "ERROR"
            row.label(text=issue.title, icon=SEVERITY_ICON[issue.severity])
            if issue.fix:
                op = row.operator("shiro.doctor_fix", text=issue.fix_label)
                op.kind, op.index = issue.fix, i
            col = box.column(align=True)
            col.active = False
            for line in issue.items[:5]:
                col.label(text=line)
            if len(issue.items) > 5:
                col.label(text=f"… and {len(issue.items) - 5} more")


classes = (SHIRO_OT_doctor_scan, SHIRO_OT_doctor_fix, SHIRO_PT_doctor)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _issues
    _issues = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
