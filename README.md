# pb_renderman

**Physically Based → PxrSurface** for Maya + RenderMan.

Browse the [Physically Based](https://physicallybased.info) material database from inside Maya and set up a `PxrSurface` from measured values in one click: metals with real complex IOR, glass and liquids with Beer–Lambert interior absorption, scattering media, skin subsurface and thin-film iridescence. Everything is converted to your scene's rendering space (ACEScg or linear sRGB).

Author: **Julien Duval** · Version **1.0.0** · License **MIT**

---

## Requirements

- Maya 2022 or later (Python 3, PySide2 or PySide6)
- RenderMan for Maya 24 or later (developed and tested with 27.2)
- Internet access on first launch (the database is then cached for a week)

## Installation

1. Copy the `pb_renderman` folder into your Maya scripts folder:
   - Windows: `Documents\maya\scripts\` (all versions) or `Documents\maya\<version>\scripts\`
   - macOS: `~/Library/Preferences/Autodesk/maya/scripts/`
   - Linux: `~/maya/scripts/`

   ```
   scripts/
     pb_renderman/
       __init__.py
       pb_renderman.py
       README.txt
       pb_renderman.png, pb_renderman_150.png, pb_renderman_200.png, pb_renderman.svg   (icon)
   ```
2. Create the shelf button (once), in the Script Editor (Python tab):
   ```python
   import pb_renderman
   pb_renderman.install_shelf_button()
   ```
   A button with the tool's icon is added to the current shelf; Maya saves it when it quits.
3. Click the button to open the tool.

The button runs `import pb_renderman; pb_renderman.show()`, which you can also run from the Script Editor. It reloads the tool each time, so an updated `pb_renderman.py` is picked up without restarting Maya.

If an older single-file version (`scripts\pb_renderman.py`) was used in the current Maya session, delete that file and restart Maya once.

## Usage

1. Pick a material in the list (search by name or tag, filter by category). The panel shows the database render, the values and exactly what will be set.
2. Choose an action:
   - **Apply to Selected Materials**: updates the `PxrSurface` nodes that are selected, or assigned to the selected objects. Creates nothing.
   - **Create & Assign Material**: creates a new `PxrSurface` + shading group and assigns it to the selected objects or faces (or creates it unassigned).

Every action is a single undo step. Attributes connected to a texture or pattern are never overwritten.

### MyLibrary: your own presets

The **MyLibrary** category lists your own PxrSurface presets, stored in their own folder, shared by all Maya versions and kept apart from Maya's preset folders:
`Documents\maya\physicallybased_MyLibrary\` (Maya's user folder)

- They are applied exactly as saved, with the same two buttons. Only *Non-directional (faster SSS / Glass Single Scatter)* applies to them; the other options do not.
- **Save Selected to MyLibrary…** saves the selected PxrSurface (or the one assigned to the selected object) as a new preset. The files use Maya's attribute-preset format (`.mel`), so you can also copy one into Maya's own preset folder if you want it in the Attribute Editor's *Presets* menu.
- **Delete from MyLibrary…** moves the selected preset (and its preview image) to the `_trash` subfolder of the library, so it can be restored.
- **Render Thumbnail** renders the selected preset in RenderMan's own Preset Browser preview scene (same scene and renderer, lit by the Luxo_jr HDR), at 256 px (the browser renders 128 px) with up to 512 samples, converted to Maya's display/view transform, and saves it next to the `.mel` as a `.png`. You can also use your own `.png` or `.jpg` with the same name as the `.mel`.
- MyLibrary is available even when the database cannot be downloaded.

### Options

| Option | Default | What it does |
|---|---|---|
| Color space | auto | Detected from Maya's rendering space (ACEScg or linear sRGB). The Maya rendering space is shown below it. |
| Specular roughness | 0.45 | Default roughness. Transmissive materials use 0.001. |
| Database value if meaningful | on | Uses the database roughness when it is not 0 or 0.5 (generic placeholders), e.g. brick 1.0, paper 0.8. |
| SSS scale | 10 | Multiplier on the database subsurface radius (mm) → `subsurfaceDmfp`. |
| Extinction scale | 1 | Multiplier on interior extinction. Coefficients are treated as per cm: 1 for a scene in cm, 100 in meters. |
| Non-directional (faster SSS / Glass Single Scatter) | off | Replaces directional scattering by isotropic scattering: reduced scattering with `g` = 0 in the glass interior, Directionality 0 for subsurface and single scatter. Similar look, much faster, works in XPU. Also applies to MyLibrary presets (the files are not changed). |
| Reset other attributes to defaults | on | *Apply* only: resets every unconnected attribute to its default before applying. |

Click an option's name to reset it to its default value; right-click a checkbox for the same.

## How materials are mapped

**Opaque dielectrics**: `diffuseColor` from the database color, Primary Specular in GGX with physical Fresnel and the database IOR.

**Metals**: Primary Specular with physical Fresnel and per-channel complex IOR (n, k) in the scene's color space. When the database n, k do not match the material color (tinted variants such as anodized aluminum), n, k are derived from color + edge tint with Gulbrandsen's inverse mapping.

**Transmissive materials** (glass, water, liquids, plastics, gems): the Glass lobe only, for refraction *and* reflection. The Primary Specular is left untouched and the diffuse color is not changed. The tint comes from the interior:
- `extinction` from the database volume coefficients, or from color + transmission depth (Beer–Lambert, `σ = −ln(color) / depth`)
- `ssAlbedo` and `g` (the interior *g0* parameter) from the database scattering and anisotropy
- `multiScatter` enabled whenever the single-scatter albedo is above 0

**Subsurface**: `subsurfaceColor` from the color, `subsurfaceDmfp` / `subsurfaceDmfpColor` from the database radius. Skin uses a Directionality of 0.8.

**Thin film** (soap bubble, pearl, anodized titanium): Iridescence lobe in Physical mode with the database thickness. PxrSurface has no film IOR, so its face gain is set to the film's normal-incidence reflectance `((n − 1) / (n + 1))²`.

### Color space conversion

The database provides ACEScg and linear sRGB values for most colors and metals. When only sRGB exists, colors go through the Bradford sRGB → ACEScg matrix. Volume coefficients are never put through that matrix: extinction is converted through transmittance and albedo through the multiple-scattering diffuse reflectance, which are the quantities you actually see.

### Overrides

Three settings at the top of the script hold per-material overrides:
```python
SSS_DIRECTIONALITY  = {"Skin": 0.8}                    # by name prefix
FORCE_ISOTROPIC     = ("Blood", "Coffee")              # by name prefix
THIN_FILM_OVERRIDES = {"Titanium (Anodized)": 300.0}   # nm, by exact name
```
Blood and coffee are always rendered non-directional: they are dense and strongly forward-scattering (blood: g ≈ 0.99, about 900 scattering events per cm), so directional mode needs far too many light bounces to be usable.

## Known limitations

- **Multiscattering is not supported in XPU.** Scattering media rendered in directional mode (juices, beer, wine) can render too dark in XPU; tick *Non-directional (faster SSS / Glass Single Scatter)*.
- **Volume units are inferred.** The database does not state the unit of its volume coefficients. Per cm matches the measured absorption of water and the glass/water/ice depths, but some liquids look about ten times off. Use *Extinction scale* to adjust.
- **RenderMan's built-in metal presets are defined in linear sRGB** and are not converted for ACEScg scenes, so they will differ slightly from this tool in an ACEScg scene. This tool's values are the color-managed ones.
- Per-channel (RGB) n, k and volume coefficients are an approximation of spectral data.

## Files

The database is cached in `Documents\maya\physicallybased_cache\` (or `MAYA_APP_DIR`, or the temp folder). Delete it at any time; it is rebuilt on the next launch.

Your presets live in `Documents\maya\physicallybased_MyLibrary\`. Back this folder up: it is your own data.

## Credits

- Author: Julien Duval
- Developed with the assistance of Claude (Anthropic)
- Material data: [Physically Based API](https://api.physicallybased.info), CC0, by Anton Palmqvist and contributors

## License

MIT, see [LICENSE](LICENSE).
