# Changelog

All notable changes to Physically Based for RenderMan.

## [1.1.0] - 2026-11-24

### Added
- Display name **Physically Based for RenderMan** (*Real-world material values for PxrSurface in Maya*); the module, folder and repository stay `pb_renderman`.
- **Help** menu: User Guide (F1), links, About (version, credits, license).
- **MyLibrary categories**: subfolders of the library, with New / Rename / Delete Category buttons; separate **Physically Based** and **MyLibrary** category menus, each with a **Hide** checkbox.
- **Update Selected to MyLibrary…** overwrites the preset picked in the list.
- Right-click a preset: Rename…, Render Thumbnail(s), Move to category (several presets at once, or to a new category), Delete….
- **Shared presets**: 17 presets by Julien Duval ship in `pb_renderman/presets/` and are copied into MyLibrary at launch, without overwriting the user's changes.
- Thumbnails: render progress in a Maya progress window (cancellable), several thumbnails in a row (multi-selection), **Draft quality** checkbox, dome light +0.5 stop.
- Right-click any option to reset it to its default value.
- `subsurfaceGain` > 0 turns on *Resolve Self Intersections* (database materials and MyLibrary presets).
- Units in the option labels: SSS scale (mm), Extinction scale (cm).
- README pictures: render sample, window screenshot, shared presets lineup.

### Changed
- Thumbnails rendered at 256 px with pixel variance 0.02.
- Render Thumbnail is sturdier across RenderMan versions (no fixed file names, clear error if the Preset Browser internals change).
- No personal paths in the help and tooltips.

### Fixed
- *Reset other attributes to defaults* zeroed some attributes: defaults are now read from a fresh PxrSurface.

### Removed
- Delete from MyLibrary and Refresh buttons (replaced by the right-click menu and the category buttons).

## [1.0.0] - 2026-11-21

First version.

- Browse the Physically Based database (API v2) and set up a PxrSurface from measured values: physical Fresnel, complex IOR for metals, Glass lobe for transmissive materials with interior extinction and single scatter, subsurface, thin-film iridescence.
- Colors converted to the scene's rendering space (ACEScg or linear sRGB).
- Apply to Selected Materials / Create & Assign Material, one undo step each.
- MyLibrary personal presets with thumbnails rendered with RenderMan's Preset Browser scene.
- Shelf button with icon.

[1.1.0]: https://github.com/lekastor/pb_renderman/releases/tag/v1.1.0
[1.0.0]: https://github.com/lekastor/pb_renderman/tree/1443b46
