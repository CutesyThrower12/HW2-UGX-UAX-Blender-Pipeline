# Halo Wars 2 UGX/UAX Pipeline for Blender 5.x+

**A Blender 5 add-on for importing, editing, and exporting Halo Wars 2 UGX models and UAX animations.**

Built by **CutesyThrower12** during Project Nuphillion tool development, this add-on brings the modern Halo Wars 2 model pipeline and the proven legacy UAX animation workflow into one clean Blender sidebar.

> **Status:** actively developed and verified with custom Halo Wars 2 structure animation tests.

---

## What does this add-on do?

Halo Wars 2 uses formats that Blender does not support natively. This add-on helps bridge that gap:

- **UGX models** can be imported into Blender and exported back through `ugx.exe`.
- **UAX animations** can be imported onto a selected armature.
- **Custom UAX animations** can be exported through the workflow that actually works in-game: sampled DAE → GR2 → UAX.
- **Animation cleanup tools** are available in a dedicated UAX sidebar for offsets, pose capture, and grounding/contact fixes.

The goal is to make Halo Wars 2 model and animation iteration faster, cleaner, and easier for modders.

---

## Key features

### UGX model workflow

- Import `.ugx` models by converting UGX → glTF with `ugx.exe`, then loading the result into Blender.
- Export Blender models back through glTF → UGX using `ugx.exe`.
- Automatic import cleanup for armatures, meshes, bones, and display setup.

### UAX animation workflow

- Import `.uax` animations onto a selected armature.
- Supports custom UAX files made by this add-on.
- Imports native scale-shear animation into Blender scale keyframes.
- Includes a **GrannyRoot lock rotation** mode for structure animations that import sideways in raw 1:1 mode.

### Working template-free UAX export

This add-on exports custom UAX animations through the same style of pipeline used by the older HWDE Universal Geometry exporter:

```text
Blender Action
→ sampled Collada / DAE
→ DAEtoGR2.exe
→ temporary GR2
→ gr2ugx.exe -auto -anim
→ final UAX
```

This route was restored because scratch-written UAX files could look structurally valid but still be ignored by Halo Wars 2. The legacy DAE → GR2 → UAX path was verified in-game and is now the default UAX export method.[^legacy-exporter]

### Animation helper tools

The add-on also includes a dedicated **UAX** sidebar tab with helper tools for:

- uniform bone offsets
- capturing pose offsets
- applying offsets to one action or all actions
- grounding/contact cleanup
- selecting contact/floor bones

---

## Requirements

- **Blender 5.x+**
- `ugx.exe` for UGX model import/export

---

## Recommended folder layout

If installing as a zip, the add-on should look like this:

```text
HaloWars2UGXUAXPipeline.zip
└─ HaloWars2UGXUAXPipeline/
   ├─ __init__.py
   └─ tool/
      ├─ DAEtoGR2.exe
      └─ gr2ugx.exe
```

## Installation

1. Download the add-on zip file and `ugx.exe`.
2. Open Blender.
3. Go to **Edit → Preferences → Add-ons**.
4. Click **Install**.
5. Select the zip file.
6. Enable **Halo Wars UGX Pipeline Pro**.
7. Open the 3D View sidebar with **N**.
8. Use the **Halo Wars 2 Pipeline** tab for import/export tools.
9. Use the **UAX** tab for animation helper tools.

After installation, set the `ugx.exe` path in the add-on preferences if the automatic path is not correct.

---

## Basic usage

### Import a UGX model

1. Open the **Halo Wars 2 Pipeline** sidebar.
2. Click **Import UGX Model**.
3. Select a `.ugx` file.
4. The add-on converts the model through `ugx.exe` and imports it into Blender.

### Import a UAX animation

1. Import or select the target armature.
2. Click **Import UAX**.
3. Choose a `.uax` file.
4. For custom structure animations, use **GrannyRoot lock rotation** if the raw 1:1 import appears sideways.
5. The add-on creates a Blender Action on the selected armature.

### Export a UAX animation

1. Select the target armature.
2. Choose an output folder.
3. Choose whether to export the active action or all actions.
4. Click **Export UAX**.
5. The add-on exports a sampled DAE, converts it to GR2, converts GR2 to UAX, validates the result, and writes a debug report.

### Export a UGX model

1. Set the `ugx.exe` path if needed.
2. Choose whether to export selected objects only.
3. Click **Export UGX Model**.
4. The add-on exports through Blender → glTF → UGX.

---

## Debug reports

Every UAX export creates a debug report beside the exported `.uax`. Keep this file when reporting issues.

The report includes:

- selected armature
- action name
- frame range
- DAE writer used
- legacy tool paths
- exact `DAEtoGR2.exe` command
- exact `gr2ugx.exe` command
- GR2 and UAX output sizes
- UAX validation status
- orientation patch details when used

If an exported animation does not play in-game, the debug report is the first thing to check.

---

## Troubleshooting

### `ugx.exe` is missing

Set the correct path in the add-on preferences. UGX import/export needs `ugx.exe`.

### `gr2ugx.exe` returns a warning or non-zero code

The add-on validates the produced UAX directly. Some legacy tool return codes can be noisy even when a valid UAX was written.

### Imported custom UAX appears sideways

Use **GrannyRoot lock rotation** during UAX import. Raw 1:1 mode is useful for inspection, but structure clips often need the GrannyRoot lock mode for comfortable editing.

### Exported UAX plays sideways in-game

Use the latest version of the add-on. The working export path includes an automatic orientation patch for manual Blender 5.x DAE output.

---

## What this project is not

This is not an official Halo Wars 2 tool. It is a community modding add-on.

This repo also does not claim ownership of Halo Wars, Halo Wars 2, UGX, UAX, Granny, or any external conversion tools. Use the add-on responsibly and follow the licenses for any tools or game assets you use.

---

## Credits

- **CutesyThrower12** — add-on author and Project Nuphillion pipeline testing.
- **Bleh** — creator of `ugx.exe` and the ensemble-formats project powering the modern UGX workflow.
- **Stumpy** — creator of the original HWDE Universal Geometry Pipeline and associated Halo Wars Blender workflows.
- Legacy HWDE Universal Geometry workflow — referenced for the working sampled DAE → GR2 → UAX export path.[^legacy-exporter]
- Halo Wars modding community — format research, testing, and workflow knowledge.

---

## Legal notice

This project is not affiliated with, sponsored by, or endorsed by Microsoft, Xbox Game Studios, 343 Industries, Creative Assembly, Ensemble Studios, or any other rights holder.

Halo Wars and Halo Wars 2 are trademarks and properties of their respective owners.

Do not redistribute external conversion tools unless you have permission to do so.

---

## References

[^legacy-exporter]: Historical HWDE Universal Geometry exporter behavior inspected from the legacy Python exporter used during development. Its animation path selected the armature, exported sampled Collada with `include_animations=True`, `include_all_actions=False`, `deform_bones_only=True`, `export_animation_type_selection='sample'`, `sampling_rate=1`, then ran `DAEtoGR2.exe -debug` followed by `gr2ugx.exe -auto -anim`.
[^blender-addons]: Blender Manual, “Add-ons,” documents Blender add-ons as secondary scripts used to extend Blender functionality: https://docs.blender.org/manual/en/latest/editors/preferences/addons.html
[^collada]: Khronos Group, “COLLADA - 3D Asset Exchange Schema,” describes COLLADA as an XML-based schema for visual scenes, geometry, shaders/effects, animation, kinematics, and related 3D asset data: https://www.khronos.org/collada/
[^halo-wars-modding-ugx]: Halo Wars Modding, “The Blender UGX Pipeline,” documents older community UGX pipeline context: https://halowarsmodding.github.io/tools/ugx
