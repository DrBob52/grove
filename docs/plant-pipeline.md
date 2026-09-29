# Plant pipeline: FABOTANIC to USDZ

Tool details in this guide were checked on 2026-09-29. Section 10 lists the sources and says which claims could not be confirmed.

## 1. What this is

Grove shows 3D plants that people can orbit, zoom and tap. Each plant is generated in FABOTANIC (a web plant generator by AMIX), exported as a GLB file, converted once to USDZ by a script that runs on your Mac or in the cloud, and bundled in the app for RealityKit to load. There is no wind animation in v1, because FABOTANIC drives wind from JavaScript and the GLB file is static.

The path for one plant:

| Step | What you do | Section |
|---|---|---|
| 1 | Export the GLB, a settings JSON and a thumbnail PNG from FABOTANIC | 3 |
| 2 | Inspect the GLB | 4 |
| 3 | Convert it to USDZ with `scripts/convert_plant.py` | 5 |
| 4 | Install it in the app, then check it there | 8, 6 |
| 5 | Fail: repair in Blender, export a new GLB, go back to step 3. Pass: log it in `LICENSE-RECORD.md` | 7, 8 |

### Glossary

You will see these terms in every section. Blender has its own vocabulary, which section 7 introduces where you need it.

| Term | Meaning |
|---|---|
| glTF | An open 3D file format from the Khronos Group. It describes shapes, materials and textures. FABOTANIC exports it. |
| GLB | The single-file form of glTF. The shapes, materials and texture images are packed into one file. |
| USDZ | Apple's single-file 3D format, a zip of a USD scene (Universal Scene Description, from Pixar) and its textures. RealityKit loads it. |
| Mesh | The surface of a model: points (vertices) joined into small flat pieces. |
| Triangle | The flat piece a graphics chip draws. Triangle count is the main measure of how heavy a model is. |
| Material | The description of how a surface looks: color, shininess, transparency. |
| Texture, map | An image wrapped onto a mesh. A base color map holds the surface color, a normal map fakes fine bumps in the lighting, and a roughness map says which parts are shiny and which are matte. |
| Alpha | The transparency channel of an image or material. 1 is fully visible, 0 is invisible. |
| Alpha cutout (MASK) | Every pixel is either fully visible or fully hidden, split at a cutoff (usually 0.5). Leaves are drawn as flat cards and the texture cuts out their outline. glTF calls this MASK. USD stores the cutoff as `opacityThreshold`. |
| Alpha blend (BLEND) | Partly transparent pixels mix with whatever is behind them. The renderer has to draw surfaces back to front, and overlapping leaves cannot be sorted correctly, so you get flicker and leaves in front of or behind each other wrongly. |
| Double-sided | A double-sided material draws both faces of a surface. A single-sided material draws only the front face, so a leaf card disappears when you look at its back. RealityKit ignores USD's `doubleSided` flag, so the app turns off face culling on plant materials when it loads them. |
| Origin | The point that a model's position and rotation refer to. For plants it sits at the base of the stem, so the plant stands on the floor at (0, 0, 0). |
| Y-up | The Y axis points up. glTF and RealityKit are both Y-up and both use meters. Blender is Z-up and converts on import and export. |

## 2. One-time setup

### 2.1 FABOTANIC

1. Open https://amix-design.com/tl/fab-botanic/ in a browser.
2. Use your browser's page translation. The tool page is written in Japanese.

The tool runs in the browser and needs no account. The site states that generating, editing and saving all happen in the browser and that no data is sent to a server.

### 2.2 The converter environment

The converter is a Python script, so you set up a Python virtual environment once. A virtual environment is a private folder of packages that stays out of the rest of your system.

1. In Terminal, run `python3 --version`. You need 3.10 or newer. If yours is older, install a current Python 3 from python.org or with `brew install python`, then run the next commands with that `python3`.
2. From the repo root, run `python3 -m venv .venv`.
3. Run `.venv/bin/pip install -r scripts/requirements-convert.txt`. This installs the packages the converter needs, including `usd-core`, Pixar's USD library.
4. Run `source .venv/bin/activate`. Repeat this in every new Terminal window before you run the scripts. While the environment is active, `python3` means the environment's Python.

The `.venv/` folder is git-ignored.

### 2.3 Why there is no Reality Converter step

Older guides send you to Apple's Reality Converter app. Apple no longer offers it. An Apple engineer wrote on the developer forums in June 2026 that Preview superseded it, and forum threads from 2026 report that the old download link now lands on Apple's AR tools page instead of a file. Do not go looking for a copy.

Conversion is done by `scripts/convert_plant.py`, which wraps `usdzconvert`, Apple's command-line converter, through a community Python 3 port (https://github.com/niw/usdzconvert, MIT license). Do not upload plant files to online GLB-to-USDZ converters. Several are run by AI 3D companies, and the FABOTANIC terms forbid feeding models into other 3D generators or training sets.

### 2.4 Blender (repairs only)

You open Blender only when the inspector or the visual check finds a problem.

1. Go to https://www.blender.org/download/ and download the current release, Blender 5.2 LTS (released 2026-07-14, supported until July 2028). Blender 5.3 is still in alpha, with a release planned for November, so skip it.
2. Open the downloaded `.dmg` and drag Blender into the Applications folder.

Blender 5.0 and later run on Apple silicon Macs only. On an Intel Mac, download an older release from https://www.blender.org/download/releases/. The steps in section 7 match Blender 4.2 and later.

## 3. Exporting a plant from FABOTANIC

Each plant takes two export passes: one with the detail-view settings, which produces the GLB and the settings JSON, and one with the thumbnail settings, which produces only the PNG.

FABOTANIC exports in meters, Y-up, with the origin at the plant's base. RealityKit also uses meters and Y-up, so no axis or scale fix is needed anywhere in the pipeline.

**Unverified:** the tool page could not be opened from the research environment, so the exact button and menu labels were not confirmed. The site's own description confirms that the tool offers normal and low-poly styles, near, mid and far views, a texture resolution option, a lightweight GLB and a standard GLB, a ground option, and saving as GLB, image or Three.js ZIP. This guide uses the spec's names for the settings. If a label in the tool differs, match it by meaning and keep the values in the table.

### Settings

| Setting | Detail view (hero plant) | Catalog thumbnail | Reason |
|---|---|---|---|
| Style | Normal | Normal | Low-poly reads as a different art style |
| Use | Near | Mid | Near keeps detail for close orbit. Mid is lighter. |
| Generation quality | Standard | Light | Fine adds triangles a phone screen cannot show |
| Texture resolution | 1K | 512px | 2K quadruples texture memory over 1K |
| 3D data structure | Normal GLB (compatibility) | Normal GLB | Lightweight GLB uses GPU instancing, which converters may drop |
| Include ground | Off | Off | The app draws its own floor |
| Also save | Settings JSON | Transparent PNG | The JSON lets you regenerate the same plant later |

The site describes the lightweight GLB as one that needs a viewer supporting the `EXT_mesh_gpu_instancing` extension, while the standard GLB stores the shapes in expanded form. GPU instancing means each leaf is stored once and the graphics chip repeats it. This pipeline has not been tested with the lightweight GLB, so always pick the normal one. Ignore the Three.js ZIP option.

### Steps

1. Choose the plant type and shape it with the tool's controls until you like it. The tool offers 41 species, including trees, flowers, ferns, foliage plants and cacti.
2. Choose the plant ID (see Naming below).
3. Set the detail-view column of the table. Save the GLB and the settings JSON.
4. Write down the readouts (see Readouts below).
5. Load the settings JSON you saved so the plant is identical, switch the settings to the thumbnail column, and save the transparent PNG. Load the JSON through whatever the tool offers for opening a saved settings file. Its label was not verified.
6. Move the files into place:

| File | Path |
|---|---|
| GLB | `Assets/Plants/source/<plant-id>.glb` |
| Settings JSON | `Assets/Plants/source/<plant-id>.json` |
| Thumbnail PNG as exported | `Assets/Plants/source/<plant-id>.png` |

### Naming

A plant ID is lowercase kebab case: the FABOTANIC system name, a letter, and a two-digit number that you raise for each variation you export. Examples: `fern-a-01`, `succulent-b-02`.

The same ID names the GLB, the USDZ, the thumbnail and the manifest entry. `scripts/check_plant_assets.py` rejects IDs that do not match this pattern.

### Readouts

Write these down while the plant is on screen. You cannot get them back without regenerating the plant.

| Readout | Where it goes |
|---|---|
| Height in meters | `heightMeters` in `plants.json`. It sets the camera distance. |
| Triangle count | The Triangles column of `Assets/Plants/LICENSE-RECORD.md`. The triangle cap will be set after profiling the heaviest plant. |
| FABOTANIC version | The FABOTANIC version column of the record. Whether the tool shows a version number was not verified. If it does not, write `not shown`. |
| License version | The License version column of the record. Read it on the tool's License and Terms of Use page (1.0.0 on 2026-09-29). |

## 4. Inspecting the GLB

The inspector reads the GLB and reports problems before you convert. Run it right after you export.

```
python3 scripts/inspect_glb.py Assets/Plants/source/<plant-id>.glb
```

It prints the materials, textures, triangle count and bounds (the smallest box that holds the plant), then a list of findings. Each finding has a level and a code:

- **ERROR** stops the pipeline. The command exits with status 1. Fix it before you convert.
- **WARN** means the plant will probably look wrong. Fix it, or accept it and write the reason in the Notes column of the record.
- **INFO** reports something to confirm later.

`convert_plant.py` runs the inspector again before it converts, so you see the same findings twice.

| Level | Code | What it means | What to do |
|---|---|---|---|
| ERROR | `external-texture` | A texture image is stored in a separate file instead of inside the GLB. The converter cannot find it and the plant comes out untextured. | Re-export from FABOTANIC as a Normal GLB. If a Blender-saved file triggers it, export again with Format set to **glTF Binary (.glb)** (section 7.9), which packs the images into the file. |
| WARN | `gpu-instancing` | This is the lightweight GLB. Leaves are stored once and repeated by the graphics chip. Converters may drop the repeats and leave a bare stem. | Re-export from FABOTANIC with 3D data structure set to Normal GLB (compatibility). |
| WARN | `compressed-geometry` | The file uses Draco, meshopt, basisu or quantization. These are compression methods that need special support, and the conversion may fail or come out empty. | Re-export from FABOTANIC as a Normal GLB. If the file came from Blender, export again with compression off (section 7.8). |
| WARN | `alpha-texture-on-opaque` | The leaf texture has transparent pixels but the material is marked opaque, so the transparency is ignored and leaves render as solid rectangles. | Set the leaf material to alpha cutout in Blender (section 7.2). |
| WARN | `blend-alpha` | The material uses blended transparency, which shows sorting errors and flicker. | Change it to alpha cutout (MASK) in Blender (section 7.2). |
| WARN | `single-sided-cutout` | A transparent material is single-sided, so leaf backs vanish when you orbit. | The app already turns off face culling on plant materials, so check in the app first. If leaf backs still vanish there, see section 7.3. |
| WARN | `texture-over-1k` | A texture is larger than 1024 pixels on a side, the limit for this app. | Re-export from FABOTANIC at 1K. If you cannot, resize the image in Blender (section 7.7). |
| WARN | `base-not-at-origin` | The lowest point of the plant is not at Y = 0 (the tolerance is 5 mm plus 1% of the plant's height). The plant will float or sink. | Move the origin to the base in Blender (section 7.6). |
| INFO | `alpha-cutout` | A material uses MASK transparency. This is the healthy state for leaves. | Confirm in the app that leaves show gaps around their edges (section 6). |
| INFO | `texture-memory` | An estimate of texture memory once loaded. A 1K RGBA texture takes about 4 MB of GPU memory, and a plant with leaf and bark materials can carry six maps (base color, normal and roughness for each). | Compare plants against each other. Record heavy ones in the Notes column. |

## 5. Converting to USDZ

`scripts/convert_plant.py` reads `Assets/Plants/source/<plant-id>.glb`, runs the inspector, converts the file, writes `Assets/Plants/usdz/<plant-id>.usdz`, and checks the result. The checks confirm that the leaf materials received an `opacityThreshold`, that the textures are packed inside the USDZ, that the scene is Y-up and in meters, and that the plant's base sits at zero. `usdzconvert` turns a glTF alphaMode of MASK into `opacityThreshold` using the material's `alphaCutoff`, and connects the opacity to the texture's alpha.

1. In Terminal, go to the repo root and run `source .venv/bin/activate` if the environment is not already active.
2. Run:

   ```
   python3 scripts/convert_plant.py <plant-id>
   ```

3. Read the output. Any failed check means the USDZ is not ready. Use the table below to find the repair, then come back to step 2.
4. When every check passes, you have `Assets/Plants/usdz/<plant-id>.usdz`. To copy it into the app in the same step, run the script again with `--install`. That copies the passing USDZ to `App/Resources/Plants/<plant-id>.usdz`.

If a flag or message differs from this guide, run `python3 scripts/convert_plant.py --help`.

| Failed check | Likely cause | Repair |
|---|---|---|
| Leaf materials have no `opacityThreshold` | The leaf material is opaque or blended in the GLB | Section 7.2 |
| Textures not packed | The GLB points at external image files | Section 4, `external-texture` |
| Not Y-up or not in meters | The GLB is oriented or scaled wrongly | Section 7.6 |
| Base not at zero | The origin is not at the base | Section 7.6 |

The files in `Assets/Plants/usdz/` are git-ignored staging output. The copy in `App/Resources/Plants/` is the one you commit.

## 6. The visual check

You check each plant in the app itself, first in the iOS Simulator and then on a real iPhone. Install the plant first: do steps 1 to 5 of section 8, then come back here.

1. In Xcode, open the destination menu in the toolbar and choose an iPhone simulator.
2. Choose **Product > Run** (Cmd+R).
3. Tap the plant's thumbnail in the catalog grid.
4. Drag to orbit. Hold Option and drag to pinch and zoom. Tap the plant to open the info card.
5. Go through the checklist below.
6. Connect an iPhone, choose it in the destination menu, run again and repeat the checklist. The Simulator's rendering can differ from a device and says nothing about speed.

Checklist:

- [ ] Leaves show as leaves, with clear gaps around them.
- [ ] Nothing flickers or swaps front and back as you orbit.
- [ ] Leaf backs are visible from behind.
- [ ] Bark or stem texture is present, with a visible pattern.
- [ ] No black surfaces.
- [ ] Nothing looks like plastic or wax.
- [ ] The plant stands upright, with its base on the floor.
- [ ] The whole plant is in frame when the detail view opens.
- [ ] Orbiting stays smooth on the iPhone.

| What you see | Likely cause | Inspector code | Fix |
|---|---|---|---|
| Leaves are solid rectangles with no gaps | The leaf material is opaque although its texture has transparency | `alpha-texture-on-opaque` | Section 7.2 |
| Leaves flicker or swap order as you orbit | Blended transparency cannot be sorted | `blend-alpha` | Section 7.2 |
| Leaves vanish when you view them from behind | The material is single-sided and the app's face culling override did not apply | `single-sided-cutout` | Section 7.3 |
| A surface is black | A missing texture link, flipped normals, or metallic set to 1 | `external-texture` | Section 7.4 |
| The plant looks glossy or waxy | Low roughness or nonzero metallic | none | Section 7.5 |
| The stem is a flat color | The base color texture is missing or not linked | `external-texture` | Section 7.4 |
| The plant floats, sinks or lies on its side | The origin is not at the base, or the orientation is wrong | `base-not-at-origin` | Section 7.6 |
| The camera cuts off the top or leaves a lot of empty space | `heightMeters` is wrong | none | Section 8, step 3 |
| The app stutters or reports memory pressure | Large textures or a high triangle count | `texture-over-1k`, `texture-memory` | Section 7.7, or export a lighter plant |

## 7. Repairing in Blender

Blender is a free 3D modeling program. The plan is the same for every repair: import the GLB, fix one thing, export a new GLB over the old one, then run the inspector and `convert_plant.py` again. Never export USDZ from Blender. Its USDZ exporter produces broken materials for this kind of plant. Blender edits, exports GLB, and the converter script makes the USDZ.

The FABOTANIC settings JSON can regenerate the original export at any time, so you do not need to keep a copy of the unrepaired GLB. Add a line to the Notes column of the record describing what you changed.

### Blender words you need

| Term | Meaning |
|---|---|
| 3D viewport | The large window that shows the scene. Drag the colored axis gizmo at its top right to orbit. The hand icon under it pans and the magnifier icon zooms. **View > Frame All** (Home key) centers everything. |
| Object Mode, Edit Mode | Object Mode moves whole objects. Edit Mode edits the points and faces inside one. The Tab key switches between them while the pointer is over the viewport. |
| Outliner | The list of objects at the top right. |
| Properties editor | The panel at the right with a column of tabs, such as Render, Object and Material. |
| Shader Editor | The window that shows a material as boxes (nodes) joined by lines (links). |
| Node, socket | A box in the Shader Editor, and one of its input or output dots. You connect nodes by dragging from an output socket to an input socket. |
| Principled BSDF | The main material node. Its inputs include Base Color, Metallic, Roughness and Alpha. |
| Normal | The direction a face points. Lighting and back-face culling depend on it. |
| Z-up | Blender's up axis is Z. The glTF importer turns the GLB's Y-up into Z-up, and the exporter's **+Y Up** option (on by default) turns it back. A plant that stands upright in Blender stands upright in the app. |

### 7.0 Import the GLB

1. Open Blender and choose **File > New > General**.
2. Move the pointer over the viewport, press A to select everything, then press Delete. This removes the default cube, camera and light so they do not end up in your export.
3. Choose **File > Import > glTF 2.0 (.glb/.gltf)**, select `Assets/Plants/source/<plant-id>.glb`, and click **Import glTF 2.0**. Leave the options at their defaults.
4. Click the **Shading** tab in the row of workspace tabs at the top of the window. The viewport switches to Material Preview, which shows textures. The Shader Editor appears along the bottom and the Properties editor and Outliner at the right.
5. Move the pointer over the viewport and press Home to frame the plant.

### 7.1 Which material am I editing

Click a leaf or stem in the viewport. The Shader Editor shows that object's material. To see every material, click the **Material Properties** tab in the Properties editor (the sphere icon near the bottom of the tab column). Click a slot in the list to switch materials.

### 7.2 Leaves are solid rectangles, or they flicker

Inspector codes: `alpha-texture-on-opaque`, `blend-alpha`. The convert check "leaf materials have no `opacityThreshold`" points here too.

Blender 4.2 removed the Blend Mode dropdown (with the Alpha Clip choice) that older tutorials and older exporters used. The glTF exporter now reads the mode from what is wired into the Principled BSDF's Alpha socket:

| Wiring | glTF mode |
|---|---|
| Nothing, or a constant 1 | OPAQUE |
| Alpha passes through a Math node set to Round or Greater Than, so it becomes 0 or 1 | MASK (alpha cutout) |
| Alpha wired in directly | BLEND |

To make a leaf material export as MASK:

1. Select the leaf material (section 7.1).
2. In the Shader Editor, find the **Image Texture** node that holds the leaf picture and the **Principled BSDF** node.
3. Move the pointer over the Shader Editor, press Shift+A, type `Math`, press Enter, and click an empty spot to drop the node.
4. On the new Math node, change the operation dropdown (it says **Add**) to **Round**. Round sets alpha of 0.5 or more to 1 and anything lower to 0.
5. Drag from the Image Texture node's **Alpha** output socket to the Math node's input socket.
6. Drag from the Math node's **Value** output socket to the Principled BSDF's **Alpha** input socket. If Alpha was already connected, the new link replaces it.
7. Repeat for every material whose texture has leaf transparency.
8. Export (section 7.9). Run the inspector. `alpha-cutout` should appear as INFO, and the two WARN codes should be gone.

Round uses a cutoff of 0.5. The Blender manual describes other cutoffs with Math nodes, but 0.5 is enough for plants.

### 7.3 Leaf backs vanish

Inspector code: `single-sided-cutout`. Check the app first, because the app turns off face culling on plant materials when it loads them. Fix it in Blender only if leaf backs still vanish there.

1. Select the leaf material (section 7.1).
2. In the Properties editor, click the **Render Properties** tab (the icon that looks like the back of a camera) and check that **Render Engine** says EEVEE. If it says Cycles, switch it to EEVEE. You can switch back afterwards.
3. Click the **Material Properties** tab, scroll to **Settings**, and open the **Surface** subpanel.
4. Find **Backface Culling** and clear the **Camera** checkbox. The exporter writes glTF's double-sided flag as the opposite of this checkbox, so cleared means double-sided.
5. Repeat for each leaf material, export, and run the inspector. `single-sided-cutout` should be gone.

Blender 4.4 users reported that the checkbox seemed to have no effect on the export. A follow-up post in the same thread found the Camera checkbox worked in 4.4.3. If the warning stays after you export, confirm the exported file with the inspector rather than trusting the checkbox.

### 7.4 Black surfaces

Three causes, in the order to check them.

**Missing texture link.**

1. Select the material (section 7.1).
2. In the Shader Editor, check that the Image Texture node's **Color** output is connected to the Principled BSDF's **Base Color** input. If not, drag a link between them.
3. If the Image Texture node shows an empty picture with an **Open** button, the image did not come through. The GLB probably has an external texture (`external-texture`). Re-export from FABOTANIC.

**Flipped normals.**

1. Move the pointer over the viewport. Open the **Viewport Overlays** dropdown (the arrow next to the two overlapping circles in the viewport header) and tick **Face Orientation**. Blue faces point outward and red faces point inward.
2. Click the object that shows red. Press Tab to enter Edit Mode and press A to select everything.
3. Choose **Mesh > Normals > Recalculate Outside** (Shift+N). Press Tab to go back to Object Mode.
4. Untick **Face Orientation**.

Leaf cards are flat sheets with no inside, so Blender picks a side for them arbitrarily. Since leaves are drawn double-sided, only stems and trunks need this fix.

**Metallic set to 1.** A fully metallic surface has nothing to reflect and looks black. In the Principled BSDF, set **Metallic** to 0. If the socket is connected, drag the link's end off the socket and release over empty space, then type 0.

### 7.5 Plastic or waxy look

Plants have no metal in them and are only slightly glossy. Check Blender's Material Preview first. If the plant looks matte there but glossy in the app, the cause is the app's lighting and not the plant.

1. Select the material (section 7.1).
2. In the Principled BSDF, set **Metallic** to 0.
3. Set **Roughness** to about 0.8. If a link is connected to the socket, drag its end off the socket and release over empty space, then type the value. Removing the link drops the per-pixel variation, which is acceptable for leaves and bark.
4. Export, convert, and check in the app. Try values between 0.7 and 0.9 if 0.8 looks off.

### 7.6 The plant floats, sinks or lies down

Inspector code: `base-not-at-origin`. Also the convert checks for Y-up and base at zero.

**Lying down.** The importer converts Y-up to Z-up, so a correctly authored GLB stands upright in Blender's viewport. If yours lies down:

1. In Object Mode, press A over the viewport to select all objects.
2. Press N to open the sidebar and click the **Item** tab. Set **Rotation X** to 90 or -90 until the plant stands upright.
3. Choose **Object > Apply > Rotation**.

**Floating or sinking.** You need the base of the stem at the world origin, the point where the red, green and blue axis lines cross.

1. Press A over the viewport to select all objects, then choose **Object > Apply > All Transforms**. This bakes any hidden rotation, scale or offset into the geometry. If Blender says it cannot apply to a multi-user object, choose **Object > Relations > Make Single User > Object & Data** first, then apply again.
2. Choose **View > Viewpoint > Front** to view the plant from the side.
3. Click the stem or trunk object. Press Tab to enter Edit Mode and press 1 to select by vertex. Click the lowest vertex at the base of the stem. If you cannot see it, press Alt+Z (Option+Z on a Mac keyboard) to toggle X-ray, which makes the mesh see-through.
4. Press Shift+S and choose **Cursor to Selected**. The 3D cursor, the small red and white target, now sits at the base.
5. Press Tab to return to Object Mode. Press A to select all objects.
6. Choose **Object > Set Origin > Origin to 3D Cursor**. This moves every selected object's origin to the base.
7. Choose **Object > Snap > Selection to World Origin**. This moves every selected object so its origin lands at (0, 0, 0). The base is now at the origin.
8. In the sidebar Item tab, check that Location reads 0, 0, 0.
9. Export (section 7.9) and run the inspector. `base-not-at-origin` should be gone.

### 7.7 Textures over 1K

Inspector code: `texture-over-1k`. Re-exporting from FABOTANIC at 1K is the better fix, because it produces a clean texture. Use this only if you have to keep the current plant.

1. In the Shading workspace, find the **Image Editor** at the lower left.
2. Click the image dropdown in its header and choose the oversized image. Press N in the Image Editor, click the **Image** tab, and read its size.
3. Choose **Image > Resize**. Set both X and Y to 1024, or lower, and confirm.
4. Repeat for every image larger than 1024 pixels on either side, including normal and roughness maps.
5. Export (section 7.9) and run the inspector. If `texture-over-1k` is still there, the exporter used the old pixels. In the Image Editor, choose **Image > Save As** to write the resized image to a PNG, then export again.

### 7.8 Compression or external textures from a Blender export

If the inspector reports `compressed-geometry` or `external-texture` on a file that you saved from Blender, the export dialog has compression on or is not packing images. Open the export dialog (section 7.9), keep **Format** on **glTF Binary (.glb)**, and clear every Compression checkbox you find under **Data** (Draco, and in Blender 5.2 also meshopt). Blender 5.2 added meshopt compression to the glTF exporter. Leave the image format on PNG or JPEG and do not pick WebP, since converters may not read it.

### 7.9 Export the GLB

1. Choose **File > Export > glTF 2.0 (.glb/.gltf)**.
2. In the dialog's right-hand panel, set **Format** to **glTF Binary (.glb)**. This packs the mesh, materials and images into one file.
3. Under **Transform**, tick **+Y Up**. It is on by default.
4. Under **Data**, find the **Compression** setting and leave it cleared. In Blender 5.2 this includes meshopt as well as Draco.
5. Under **Data > Material**, set Materials to **Export**. If leaf transparency disappears after the export, open the Images setting in the same area and choose **PNG**, which is lossless and keeps alpha.
6. Choose `Assets/Plants/source/` as the folder, type `<plant-id>.glb` as the file name (this overwrites the old file), and click **Export glTF 2.0**.
7. Run `python3 scripts/inspect_glb.py Assets/Plants/source/<plant-id>.glb`, then `python3 scripts/convert_plant.py <plant-id>`.

Blender's **File > Export** menu also lists **Universal Scene Description**. Do not use it for plants.

## 8. Adding the plant to the app

1. Convert and install. Run `python3 scripts/convert_plant.py <plant-id> --install` to copy the passing USDZ into `App/Resources/Plants/<plant-id>.usdz`.
2. Copy the thumbnail. Copy `Assets/Plants/source/<plant-id>.png` to `App/Resources/Plants/<plant-id>-thumb.png`.
3. Add an entry to the array in `App/Resources/Plants/plants.json`. Keep the file valid JSON, with commas between entries:

   ```json
   { "id": "fern-a-01", "displayName": "Arching Fern", "category": "fern", "heightMeters": 0.62, "thumbnail": "fern-a-01-thumb", "model": "fern-a-01" }
   ```

   | Field | Value |
   |---|---|
   | `id` | The plant ID |
   | `displayName` | The name shown in the app |
   | `category` | A short group name, such as `fern` or `succulent` |
   | `heightMeters` | FABOTANIC's height readout. It sets the camera distance. |
   | `thumbnail` | The thumbnail file name without the `.png` extension |
   | `model` | The USDZ file name without the `.usdz` extension |

4. Run `python3 scripts/check_plant_assets.py`. It checks the manifest, that the files exist, that each USDZ is under 8 MB, and that textures are at most 1024 pixels. It prints `ERROR` and `WARN` lines and exits with status 1 on any error.
5. Run `xcodegen` from the repo root so Xcode picks up the new files.
6. Run the visual check (section 6). If the plant fails, use section 7, then run `python3 scripts/convert_plant.py <plant-id> --install` again. To drop the plant, delete its two files and its manifest entry.
7. When the plant passes, add a row to the table in `Assets/Plants/LICENSE-RECORD.md`: plant ID, FABOTANIC version, license version, license URL, export date, settings JSON path, triangles, height and the result of the visual check. Put anything you changed in Blender in Notes.

## 9. Troubleshooting

**`convert_plant.py` says it cannot import `pxr` or a USD module.**
The virtual environment is not active or the packages are not installed. Run `source .venv/bin/activate` from the repo root. If the error persists, repeat step 3 of section 2.2.

**The inspector warns `single-sided-cutout`, but the plant looks fine in the app.**
That is expected. The app turns off face culling on plant materials, so leaf backs show whether or not the flag is set. Leave the warning, and write "single-sided in GLB, culling off in app" in the Notes column.

**Can I use Blender's USDZ exporter, or an online converter, to save time?**
No to both. Blender's USDZ exporter gives broken materials. Online converters mean uploading models to a third party, and the FABOTANIC terms forbid feeding models into other 3D generators or training sets.

**The USDZ looks black when I open it in Preview.**
Preview on macOS 26.2 fails to render some USDZ files correctly. They open normally in Xcode, and an Apple engineer acknowledged the problem on the developer forums in February 2026. Judge the plant by how it looks in the app.

**`check_plant_assets.py` reports a USDZ over 8 MB.**
Look at the texture list in the inspector output. Resize any map over 1K (section 7.7). If the file is still too large, export that plant with a lighter FABOTANIC setting and record the change in the Notes column.

**FABOTANIC shows a newer license version than the one in my records.**
A later license version applies only to assets exported under it. Older rows in `LICENSE-RECORD.md` keep the version that applied at export. Read the new terms before you export more plants, and again before each release.

## 10. Sources

Checked on 2026-09-29. Pages marked (search excerpt) blocked the research environment's fetch tool, so their content was confirmed only from the excerpt a web search returned.

**FABOTANIC**

- Tool page: https://amix-design.com/tl/fab-botanic/ (search excerpt). Confirms 41 species, normal and low-poly styles, near, mid and far views, texture resolution, lightweight GLB against standard GLB, ground option, GLB, image and Three.js ZIP saves, browser-only operation, and no registration.
- About page: https://amix-design.com/tl/fab-botanic/about.html (search excerpt)
- License and Terms of Use, version 1.0.0: the terms are reached from the tool page above (search excerpt). Commercial use in apps, modification and credit as optional are confirmed. A separate terms URL was not found.
- **Not confirmed:** the tool's exact UI labels, where or whether it shows a version number, and the label for loading a settings JSON. Section 3 uses the spec's setting names.

**Apple and the converter**

- Reality Converter superseded by Preview, Apple staff reply, June 2026: https://developer.apple.com/forums/thread/828230
- Reality Converter download gone (user post, July 2026): https://developer.apple.com/forums/thread/787674
- Preview renders some USDZ files black on macOS 26.2: https://developer.apple.com/forums/thread/814527
- Xcode failing to compile a Blender-exported USDZ, March 2026 (thread title only): https://developer.apple.com/forums/thread/820614
- Community Python 3 port of `usdzconvert`, MIT license, Python 3.10 or later, special handling for glTF alphaMode MASK: https://github.com/niw/usdzconvert
- `usd-core` on PyPI (Pixar's USD libraries, version 26.8 released 2026-07-20): https://pypi.org/project/usd-core/

**Blender**

- Download page: https://www.blender.org/download/ (search excerpt)
- Blender 5.2 LTS release, 2026-07-14, supported until July 2028: https://www.blender.org/releases/5-2/ (search excerpt)
- 5.2 release notes: https://developer.blender.org/docs/release_notes/5.2/ (search excerpt). Confirms meshopt compression added to the glTF exporter.
- Installing on macOS, Apple silicon requirement for 5.0 and later: https://docs.blender.org/manual/en/dev/getting_started/installing/macos.html (search excerpt)
- glTF 2.0 manual, 5.2 LTS (alpha modes, Backface Culling (Camera), double-sided): https://docs.blender.org/manual/en/latest/addons/scene_gltf2.html (search excerpt)
- glTF 2.0 manual, 4.5 LTS (alpha modes, Y Up, File formats, image options), read through a GitHub copy: https://github.com/wlk-r-dev/blender-manual-4.5/blob/main/addons/import_export/scene_gltf2.md
- How the exporter reads alphaMode from nodes since Blender 4.2: https://github.com/KhronosGroup/glTF-Blender-IO/issues/2111
- Camera checkbox for double-sided in Blender 4.4.3: https://blenderartists.org/t/cant-set-double-sided-materials-flag-in-gltf-export-v-4-4/1589489 (search excerpt)
- Mesh > Normals > Recalculate Outside: https://docs.blender.org/manual/en/latest/modeling/meshes/editing/mesh/normals.html (search excerpt)
- Image Editor, Resize: https://docs.blender.org/manual/en/latest/editors/image/editing.html (search excerpt)
- 3D cursor: https://docs.blender.org/manual/en/latest/editors/3dview/3d_cursor.html (search excerpt)
- **Not confirmed:** the exact wording of Blender's export dialog panels in 5.2 (Data > Mesh, Data > Material, Images), the location of the Image Editor and the Image tab in the Shading workspace, and the Overlays > Face Orientation checkbox, and the Object > Set Origin, Snap and Apply menu items in 5.2. They are the long-standing Blender menu names. If a label differs in your build, look for the same name a level up or down.

**RealityKit**

- Apple's USD feature table marks "Double-sided meshes" as supported by Raytracer, Storm and SceneKit, and not by RealityKit. That is why the app turns off face culling itself: https://developer.apple.com/documentation/usd/validating-usd-files
