# ShiroTools Audit: Blender 5.3 Migration

Audit date: 2026-11-11. Target: Blender 5.3 (released 2026-11-10).
Scope: 16 addon files in this folder. Nothing has been rewritten yet. This is the pre-rewrite review.

---

## 1. What 5.3 changes for these addons

Short version: **5.3 itself breaks nothing here.** The real breakage comes from 5.0 and 5.2 API changes, plus older code.

5.3 changes checked against the code:

- `PropertyGroup.name` is now read-only unless the subclass defines its own `name`. Every group here that sets `.name` defines it (`TimelinePreset`, `VGSmoothItem`, `OCB_MaterialItem`, `CAMBM_CameraBookmark`, `ClothLayerItem`). Safe.
- The order of `bpy.data.all_ids` changed. Not used anywhere.
- Vulkan is now the default backend on Windows. No addon draws with `gpu`.
- **DLSS** is a Cycles **viewport-only** denoiser. It needs an NVIDIA GPU and driver 620 or newer. The release notes don't name the Python property, so check it in the 5.3 Python console before scripting it.
- Useful new APIs:
  - `context.markers` and `context.selected_markers`
  - `UILayout.label_multiline`
  - 2D `KDTree`
  - `NodeTreeInterface.root_panel`
  - built-in `OpenEXR` and `opentimelineio` modules
  - `WindowManager.undo_stack`
  - `convert_rotation_mode`
  - `constraints.new(type, name)`
  - EEVEE panoramic cameras
  - Curve to Mesh "Miter Scale"
  - Geometry Nodes "Combine List"

Older breaking changes that still affect this code:

- **5.0:** the legacy Action API was removed (`action.fcurves`, `action.groups`, `action.id_root`). F-Curves now live in per-slot channelbags (`bpy_extras.anim_utils.action_ensure_channelbag_for_slot`).
- **5.0:** `Material.use_nodes` and `World.use_nodes` are deprecated. They always return `True`, setting them does nothing, and they will be removed in 6.0.
- **5.0:** `save_render` / `ImageFormatSettings` now require `media_type` to be set before `file_format`.
- **5.2:** Geometry Nodes modifier inputs moved from custom properties (`mod["Socket_2"]`) to RNA (`mod.properties.inputs.<identifier>.value`).
- **5.2:** the Compare and Random Value nodes have new socket identifiers.

---

## 2. Status overview

| Addon                       | Target | State                                                         |
| --------------------------- | ------ | ------------------------------------------------------------- |
| rename_channels             | 4.2    | **Broken in 5.0+** (legacy Action API)                        |
| cloth_layer_distances       | 4.2    | **Partly broken in 5.2+** (GN modifier inputs)                |
| one_click_baker             | 4.0    | Works, but slow and produces wrong output                     |
| texture_optimizer           | 3.0    | Works, but redraws are slow and it installs Pillow at runtime |
| vg_weight_smoother          | 4.0    | Works, very slow on dense meshes                              |
| BPMmarker (3rd party)       | 3.0    | Works, can **delete your own markers**                        |
| camera_bookmarks            | 4.2    | Works, but camera moves can't be undone                       |
| bake_shapekey_to_attribute  | 4.0    | Works, slow loops, unsafe attribute names allowed             |
| Ranger                      | 4.2    | Leftover, panel may not show in 5.x                           |
| Timeline Manager.py         | 4.2    | **Leftover duplicate of Marker Jump. Delete.**                |
| Marker Jump Blender5        | 5.0    | OK, small bugs                                                |
| invert_vgroup_sides         | 4.0    | OK                                                            |
| geonodes_shapekey_tools     | 3.0    | OK, mostly duplicates built-in UI                             |
| texture_stats               | 5.1    | OK, uses a deprecated property                                |
| surface_deform_target_check | 5.2    | Good                                                          |
| ivygen_nodes                | 5.2    | Good, but it is a script, not an addon                        |

**About `Timeline Manager.py`:** it is actually Marker Jump v1.3, not a timeline manager. It registers the same `Scene.marker_jump_props` and `MJUMP_PT_panel` as `Marker Jump Blender5.py`, so the two conflict if both are enabled. It also uses the `marker.` operator prefix, which belongs to Blender's built-in marker operators. The real 4.2 leftovers are this file and Ranger.

---

## 3. Per-addon findings

### rename_channels (Graph Editor: Quick Rename)
- **Broken:** uses `action.fcurves.new/remove` and `action.groups.new`, both removed in 5.0. Port it to the channelbag API.
- **Bug** at `rename_channels.py:161`: when a path contains `[` but no `"`, `split('"')[-2]` raises an IndexError.
- The proxy copy drops `back`, `amplitude`, `period`, F-Curve modifiers, extrapolation and curve colour. Min/max are hardcoded to ±10000.
- Uses bare `except:`. Only handles `context.object`, not bones on several objects at once.
- The F2 / Shift+F2 keymap shadows the global F2 rename.
- **Feature ideas:** batch rename with find/replace or regex, "remove proxy and bake back", copy keys with `foreach_get/set`.

### one_click_baker_optimized_full_rewrite
- **Slow:** one full bake per material per map type (the throwaway-image trick). Blender already bakes every material in one call, each into its own active image node. One bake per type would be about N times faster.
- **Slow:** Cycles samples are never lowered. Diffuse colour, normal and roughness only need 1 sample. GPU is not forced either.
- **Wrong output:** `save_render()` applies the scene view transform (AgX). That changes baked diffuse colours and corrupts normal maps. Save the raw image with `image.save()` instead.
- It needs the object to be selected, not just active. Otherwise `bpy.ops.object.bake` fails.
- The material list lives on the scene, so it goes stale when you switch objects and can bake materials that aren't on the active object.
- `use_nodes` checks and sets are dead code since 5.0.
- Auto Setup rewires the original material onto a new BSDF, which is destructive. Creating a separate `_baked` material is safer. The TexCoord and Mapping nodes are redundant.
- Bake pass settings (`use_pass_direct/indirect/color`) are not restored afterwards.
- **Feature ideas:** AO, Emission, Alpha and Metallic maps; selected-to-active (high to low poly) with a cage; ORM channel packing; UV check; margin setting.

### texture_optimizer
- **Redraw cost:** `draw()` opens every texture with Pillow and runs `os.path.isfile` six times per image on every redraw. Reading `img.size` in draw also forces unloaded images into memory. Cache the sizes on the image instead.
- It installs Pillow with pip at runtime, which needs network access and is not allowed in extensions. Blender's built-in `imbuf` module (`load` / `resize` / `write`) can replace it with no dependency, and it also handles EXR and HDR.
- It doesn't look inside node groups, while texture_stats does, so the two disagree.
- Resized copies are stored per object, but the image datablock is shared. Resizing on one object changes every object that uses it and writes duplicate files.
- `is_proxy_path` uses a plain prefix check without `normcase`, so Windows path casing can fool it.
- **Feature ideas:** batch for the selection or the whole scene, "Restore all", clean unused resized copies, size by camera distance, convert colour maps to JPG.

### vg_weight_smoother
- **Slow:** the smoothing is a pure Python loop per vertex per pass. Reading weights costs vertices × groups × groups, and writing back calls `vg.add([i])` once per vertex. Use the built-in `object.vertex_group_smooth` or vectorised numpy instead.
- Weights are not normalised after smoothing, which causes deformation artifacts.
- Parenting creates a vertex group for every bone. It should only create groups for deform bones (`use_deform`).
- The Data Transfer modifier is applied while not first in the stack, which triggers the "not first" warning and can give an unexpected result.
- Only nearest-vertex mapping is offered. Add interpolated nearest-face mapping.

### Ranger (frame-range presets)
- `bl_info` says 4.2, and `bl_context = "timeline"` probably hides the panel in 5.x. Marker Jump Blender5 switched to `poll()` for this reason.
- Add, edit and remove have no UNDO flag. A stale index raises IndexError. Nothing checks that start ≤ end.
- **Feature ideas:** apply to the preview range, make a preset from two markers, reorder, copy presets between scenes, batch-render all presets.

### Marker Jump Blender5
- It jumps by marker name, so duplicate names jump to the wrong marker. Jump by frame instead.
- UNDO on the jump and invert operators adds undo noise. The invert operator could just be a toggle property. Dead code at `Marker Jump Blender5.py:99-102`.
- **Feature ideas** (using 5.3 `context.selected_markers`): next/prev marker, rename, delete, bind camera, set the preview range between markers. Merge with Ranger and BPMmarker into one Timeline module.

### invert_vgroup_sides
- The hand-made suffix table could be replaced by `bpy.utils.flip_name()`. It covers `L_`, `Left` and `.L.001`, which the table misses.
- **Feature ideas:** also flip bone and shape-key names, create the missing mirrored groups.

### texture_stats
- `mat.use_nodes` is deprecated.
- It counts every image in `bpy.data`, including unused ones, so VRAM is overestimated.
- UDIMs count only the first tile, so those are underestimated.
- It misses world, light and Geometry Nodes images, and objects inside collection instances.
- **Feature ideas:** click a row to select the object, list unused or duplicate images, downsize from the list (merge with texture_optimizer).

### surface_deform_target_check (best of the set)
- Results are keyed by object name, so renaming an object loses them. Use `session_uid`.
- The face analysis is pure Python, which is slow at around 1M faces.
- **Main missing feature: fix buttons.** Merge doubles, triangulate concave faces, dissolve collinear vertices, then "Test Bind" on the real modifier.

### ivygen_nodes
- It has no `bl_info` or `register()`, so it can't be installed. It needs an "Add Ivy" operator.
- 5.3 adds a Miter Scale input to Curve to Mesh, worth exposing.
- `find_socket` falls back to the first socket with a matching name. On Random Value, which has several "Min"/"Max" sockets, that could pick the wrong type, so verify it in 5.3.
- **Feature ideas:** a Growth 0–1 input for animation (cheap, the trim node already exists), grow from a curve or a painted vertex group, wind, flowers, LOD.

---

## 4. Newly added addons

### BPMmarker.py (third party: Aodaruma, targets 3.0)
- **Data-loss bug:** "clear previous marks" uses `re.match(r"\|?[\d]\|?", name)`. That matches any marker whose name **starts with a digit**, such as `1st_shot` or `2024_cut`, so it deletes your own markers.
- **Bug:** it removes markers while iterating over `scene.timeline_markers`, so some get skipped.
- **Timing drift:** `frame += fpb` accumulates float error, and `int()` truncates instead of rounding. Use `start + round(i * fpb)`.
- **Wrong at NTSC rates:** it uses `render.fps` without `render.fps_base`, so 29.97 and 23.976 fps projects get the wrong beat spacing.
- Start and end default to 0/250 instead of the scene range. There's no beat offset and no subdivisions (half or quarter beats).
- Dead code for Blender before 2.80. It prints to the console on every register.
- **Licence:** it is third-party code. Check the licence of `github.com/Aodaruma/BPMmark_maker` and credit the author before bundling.
- **Feature ideas:** tap tempo, offset in milliseconds, markers on bars only, snap selected keys to beats, generate from a sound strip's start. Merge into the Timeline module.

### bake_shapekey_to_attribute.py (targets 4.0)
- **Slow:** per-vertex Python loops. `kb.data.foreach_get("co")` plus `attr.data.foreach_set("vector")` would be about 100 times faster.
- **Unsafe names:** naming the attribute `position` overwrites the mesh's real vertex positions, because the type and domain match. Reject built-in and `.`-prefixed names.
- **Edit mode:** running it in Edit Mode writes data that gets overwritten when you leave Edit Mode. Leave Edit Mode first or poll for Object Mode.
- Relative mode always diffs against `reference_key`, not the key's own `relative_key`.
- Settings are stored on the scene, not per object.
- **Feature ideas:** bake all shape keys at once (with a prefix), reverse direction (attribute to shape key), batch for selected objects, bake from the evaluated mesh. Merge with geonodes_shapekey_tools into a Shape Keys module.

### geonodes_shapekey_tools.py (targets 3.0)
- It's a 65-line panel that calls the built-in `object.modifier_apply_as_shapekey`. Blender's modifier dropdown already has "Apply as Shape Key" and "Save as Shape Key", so its only value is one-click visibility.
- It shows for every GN modifier, but applying fails when the modifier changes topology. Check vertex counts in `poll` or warn.
- **Feature idea with real value:** "Bake GN / sim to shape keys over a frame range", one key per frame keyed on and off. It pairs with the bake-to-attribute tool.

### camera_bookmarks.py (targets 4.2)
- **No UNDO flags.** Load moves the camera and you can't Ctrl+Z it. Save and delete aren't undoable either.
- The camera is found by name (`camera_name`), so renaming it breaks the bookmark. Use a `PointerProperty(type=Object)`.
- `is_ortho` is a bool, so loading turns **panoramic cameras** (new for EEVEE in 5.3) into perspective ones. Store the `type` enum instead.
- Not stored: sensor width/fit, shift X/Y, DOF (focus distance, f-stop, focus object).
- An animated camera snaps back on the next frame change. Offer "Load + Insert Keyframe".
- **Feature ideas:** viewport bookmarks without a camera (`region_3d.view_matrix`), bind bookmarks to markers for camera cuts (Marker Jump synergy), create a camera from a bookmark, batch-render stills of all bookmarks, a smooth keyed transition between two bookmarks.

### cloth_layer_distances.py (targets 4.2, v0.3)
- **Broken in 5.2+:** it reads and writes GN modifier inputs with `mod[identifier]`. 5.2 moved those to `mod.properties.inputs.<identifier>.value`. `try/except` hides the failure, so Spacing and Base silently fall back to 1.0 and 0.0, and the "Layer Table" input never gets set. IvyGen already uses the new API, so the pattern exists in this repo.
- `geonodes_modifier()` returns the first NODES modifier, which is wrong when there are several.
- Island scan is slow: the union-find is a Python loop per edge, and `refresh_islands` runs `labels == id` once per island (islands × vertices). Use `np.bincount` and `np.unique`.
- Caches and the table object are keyed by object name, so renaming the cloth orphans the `CLOTH_TABLE_*` object.
- Possible simplification: drop the helper table object and write a per-vertex `distance` float attribute straight onto the cloth (`distances[layer_values]` with numpy). That is cheap enough to redo on every slider change.
- **Bundling:** it depends on a GeoNodes cloth setup (inputs "Spacing", "Base", "Layer Table") that isn't in this folder. Ship that node group as an asset with the extension, or the tool is useless on its own.
- **Feature ideas:** colour-code layers in the viewport (write a colour attribute), auto-layer by island order or by distance from the body, save and load layer presets.

---

## 5. Bundle plan

- One extension package `shirotools/` with a `blender_manifest.toml` (no `bl_info`), one submodule per tool, and a preferences toggle to enable each one.
- Proposed modules:
  - **Timeline:** Marker Jump + Ranger + BPMmarker
  - **Camera:** Camera Bookmarks (with a marker-binding hook)
  - **Animation:** Graph Rename / Proxy Channel
  - **Textures:** texture_stats + texture_optimizer
  - **Bake:** One-Click Baker
  - **Weights:** vg_weight_smoother + invert_vgroup_sides
  - **Shape Keys:** bake_shapekey_to_attribute + geonodes_shapekey_tools
  - **Mesh Check:** Surface Deform Target Check
  - **Cloth Layers:** cloth_layer_distances + its node group asset
  - **IvyGen:** script turned into an operator, node group optionally shipped as an asset
- Drop Pillow. Key caches by `session_uid`. Use one consistent N-panel tab ("Shiro") with subpanels, or keep each tool in its own editor.
- Delete `Timeline Manager.py`.

---

## 6. New addon ideas

1. **Viewport Perf Switch:** one click switches between a "DLSS Cycles preview" (DLSS denoiser, low samples, Simplify texture limit) and an EEVEE/solid fallback, plus a "hide heavy modifiers" toggle. Fits the 5070 Ti.
2. **Scene Doctor:** missing files, duplicate `.001` materials and images, orphans, relinking.
3. **Batch Render Queue:** renders Ranger presets and camera bookmarks one after another, with marker-bound cameras.
4. **Keyframe Toolkit:** stagger or offset keys across objects and bones, retime, remove redundant keys, snap keys to BPM beats.
5. **Weight Toolkit:** limit total to 4 influences, normalise, clean, mirror. It lives next to the smoother.
6. **Channel Packer:** ORM / RMA packing from baker output.
7. **Collection Batch Export:** FBX or glTF per collection, with presets.
8. **Modifier Cost Inspector:** ranks the modifiers that cost the most polygons or time and toggles them.
9. **Sim to Shape Keys / Alembic helper:** bakes cloth or GN results per frame to shape keys or an attribute, building on the Shape Keys module.

---

## 7. Decisions needed before the rewrite

1. Should Marker Jump, Ranger and BPMmarker merge into one Timeline panel, or stay separate?
2. Should Baker Auto Setup create a new `_baked` material, or keep rewiring the original?
3. Is removing Pillow OK? It means switching to Blender's built-in `imbuf`.
4. Is BPMmarker's licence OK to bundle, or should it be rewritten from scratch?
5. Should the Cloth Layers GN node group ship inside the extension as an asset?
6. Which of the new addon ideas should be included?

---

## 8. Not verified

- No addon has been run inside 5.3. Everything above comes from reading the code and the release notes.
- The DLSS Python property name is unknown. Check it in the 5.3 console.
- The `bl_context = "timeline"` problem in Ranger and the Random Value socket-name risk in IvyGen still need testing in 5.3.

---

## Sources

- [5.3 Python API](https://developer.blender.org/docs/release_notes/5.3/python_api/)
- [5.3 release notes](https://developer.blender.org/docs/release_notes/5.3/)
- [5.3 Cycles](https://developer.blender.org/docs/release_notes/5.3/cycles/)
- [5.3 EEVEE & Viewport](https://developer.blender.org/docs/release_notes/5.3/eevee/)
- [5.3 Animation & Rigging](https://developer.blender.org/docs/release_notes/5.3/animation_rigging/)
- [5.3 Geometry Nodes](https://developer.blender.org/docs/release_notes/5.3/geometry_nodes/)
- [5.3 Modeling](https://developer.blender.org/docs/release_notes/5.3/modeling/)
- [5.0 Python API](https://developer.blender.org/docs/release_notes/5.0/python_api/)
- [5.1 Python API](https://developer.blender.org/docs/release_notes/5.1/python_api/)
- [5.2 Python API](https://developer.blender.org/docs/release_notes/5.2/python_api/)
- [Digital Production: DLSS in 5.3](https://digitalproduction.com/2026/06/03/nvidia-brings-dlss-denoising-to-blender-5-3/)
- [80.lv: DLSS coming to 5.3](https://80.lv/articles/dlss-is-officially-coming-to-blender-5-3)
