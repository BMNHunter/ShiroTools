# ShiroTools

A Blender extension that bundles my animation, rigging, baking and Unity-export add-ons into one package.
Every tool lives in the **ShiroTools** tab of the 3D View sidebar (N panel), and each module can be switched on or off in the add-on preferences.

Requires **Blender 5.2 or newer** (tested on 5.2 and 5.3).

## Modules

| Module | What it does |
|---|---|
| Timeline | Markers, range presets, BPM beats, colour tags, overlay |
| Camera Bookmarks | Save and jump between camera angles |
| Graph Rename | Group rename, batch rename, proxy channels (F2) |
| Textures | Texture stats / VRAM and resizing |
| One-Click Baker | Bake maps, baked material, Unity channel packing |
| Weights | Rig/transfer/smooth pipeline and weight toolkit |
| Shape Keys | Shape key <-> attribute, frames to shape keys |
| Surface Deform Check | Validate and fix Surface Deform targets |
| Cloth Layers | Collision-distance layers for GN cloth |
| IvyGen | Procedural ivy over edges and surfaces, leaf/bark materials |
| Scene Doctor | Missing files, duplicates, orphans |
| Render Queue | Batch render ranges, bookmarks and camera cuts |
| Unity Export | FBX presets for Unity with a health check |
| Cost Inspector | Heaviest objects and slowest modifiers |

## Install

1. Download the zip from [Releases](../../releases), or build it yourself (below).
2. In Blender: **Edit > Preferences > Get Extensions > Install from Disk...** and pick the zip.
3. Open the sidebar in the 3D View (`N`) and find the **ShiroTools** tab.

The sidebar tab name and the per-module toggles are in the add-on preferences.

## Build

```sh
blender --command extension build --source-dir shirotools --output-dir dist
```

## Tests

```sh
blender -b --factory-startup --python tests/run_tests.py
```

`tests/gui_smoke.py` and `tests/gui_render_queue.py` are GUI checks; run them in a normal Blender window.

## Upgrading from the old single-file add-ons

The `.py` files in the repo root are the original standalone add-ons. Some tests import them to compare old and new behaviour.
ShiroTools keeps their property names, so `.blend` files made with the old add-ons load with their settings intact.
Disable the old add-ons once ShiroTools is installed.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
