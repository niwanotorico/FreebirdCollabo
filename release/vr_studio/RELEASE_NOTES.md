# VR Studio v@VERSION@ — first public build (alpha)

**Tag:** `vr-studio-v@VERSION@` · **Pre-release** · Asset: `vr_studio-v@VERSION@.zip`

VR Studio is a plugin for **Freebird XR** that lets you colour, restyle and re-shade your Blender
scene from inside VR with one trigger press. It is a separate download from the Freebird
Collaboration Layer add-on and works without it.

## Install (like any Blender add-on)

1. Download **`vr_studio-v@VERSION@.zip`** below — don't unzip it.
2. Blender → **Edit › Preferences › Add-ons › ⌄ › Install from Disk…** → pick the zip → make sure
   **VR Studio for Freebird XR** is enabled.
3. Start VR → Freebird menu → **Plugins** → **Color / Look / View** (restart Blender once if they're not there yet).

The add-on only sets up the plugin for Freebird XR (install / update / remove). Updating = installing a
newer zip the same way; the old plugin is backed up to `~/.freebird/plugin_backups/` first.
Uninstall: add-on preferences → **Remove VR Studio Plugin**, then remove the add-on.

## Requirements

- **Blender 5.2** (tested). Blender 4.2+ expected to work, not yet tested.
- **Freebird XR 2.15.0** or newer.

## Features

- **Color** — 32-colour palette. Select objects, point, pull the trigger.
- **Look** — 8 material presets: Clay / Matte / Glossy / Plastic / Metallic / Glass / Emission / Toon.
  Look and Color are independent of each other.
- **View** — headset shading switch: Wireframe / Solid / Material Preview / Rendered. Local to you only.
- Panels live in a VR Studio area to the right of Freebird's main menu and follow your hand.
- Freebird's Undo / Redo work for Color and Look.
- With Freebird Collaboration Layer: Color / Look changes sync to everyone; View never syncs.

## Known limitations

- **Rendered** with Cycles is very heavy in VR — use EEVEE.
- **Glass** transparency needs Raytracing enabled (EEVEE).
- Pressing a colour unplugs an image texture on Base Color (Undo restores it).
- Only tested on Windows with Blender 5.2 + Freebird XR 2.15.0.

## Checksums

```
@SHA256@  vr_studio-v@VERSION@.zip
```
