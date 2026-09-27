# VR Studio for Freebird XR — v@VERSION@

**"Blender is powerful but hard in VR" → "in VR you just touch it."**

VR Studio is a plugin for [Freebird XR](https://freebirdxr.com) (the VR modeling add-on for Blender).
It adds a small panel area next to Freebird's VR menu where you can colour objects, give them a
material look, and switch how the scene is shaded — all with one press of the trigger.

It installs like any other Blender add-on: no Git, no code, no copying files by hand.
It does **not** modify Blender, Freebird XR or any other add-on.

> Early public version (alpha). Feedback is welcome on the
> [GitHub issues page](https://github.com/niwanotorico/FreebirdCollabo/issues).

---

## Requirements

| | Version |
|---|---|
| **Blender** | **5.2** (tested). Blender 4.2+ is expected to work but has not been tested yet. |
| **Freebird XR** | **2.15.0** or newer (tested with 2.15.0), installed and enabled. |
| **VR** | A PC VR headset that already works with Freebird XR. |

---

## Installation

1. Download **`vr_studio-v@VERSION@.zip`**. **Do not unzip it.**
2. In Blender: **Edit › Preferences › Add-ons**, click the **⌄** menu at the top right → **Install from Disk…**,
   and choose `vr_studio-v@VERSION@.zip`.
3. Make sure **VR Studio for Freebird XR** is ticked (enabled).
   It sets the plugin up for Freebird automatically — expand the add-on to see
   *VR Studio plugin: Installed @VERSION@ - up to date*.
4. Start VR with Freebird XR → open the Freebird menu → **Plugins** → **Color**, **Look**, **View**.
   (If they are not there yet, restart Blender once.)

### Updating

Install the newer zip the same way (Install from Disk). When the add-on is enabled it replaces the old
plugin, after moving the old copy to a backup folder (`~/.freebird/plugin_backups/`). Nothing is deleted.
If a *newer* VR Studio is already installed, it is left alone.

### What the add-on shows

Expand **VR Studio for Freebird XR** in the Add-ons list:

- the installed plugin version and whether it is up to date
- **Install / Update** — copies the bundled plugin again (an existing copy is backed up first)
- **Remove VR Studio Plugin** — deletes the plugin (asks first)
- **Open Plugins Folder / Open Backups Folder**

---

## How to use

Open the Freebird menu in VR → **Plugins**, then press a button to show or hide its panel.
The panels appear in the VR Studio area right next to the main menu and follow your hand.

| Button | What it does |
|---|---|
| **Color** | 32-colour palette. Select objects, point at a colour, pull the trigger — done. |
| **Look** | 8 material looks: **Clay, Matte, Glossy, Plastic, Metallic, Glass, Emission, Toon**. Independent of Color: a blue Matte object turned Metallic stays blue, and a new colour keeps the look. |
| **View** | Switches how *your headset* shows the scene: **Wireframe / Solid / Material Preview / Rendered**. It only changes your own view. |

- **Undo / Redo** are Freebird's own (they cover Color and Look changes).
- Colour and look are ordinary Blender materials, so they are saved in the .blend and render normally.
- If an object has no material, VR Studio creates one (`VR <object name>`). A material shared with an
  object you did *not* select is copied first, so the other object keeps its look.
- If Base Color is driven by an image texture, pressing a colour unplugs that texture (the image node
  stays in the material; Undo plugs it back in).

### Using it together with Freebird Collaboration Layer (optional)

VR Studio works on its own. If you also use the
[Freebird Collaboration Layer](https://github.com/niwanotorico/FreebirdCollabo) add-on, colour and
look changes reach everyone in the room through its material sync. **View** is never shared — each
person keeps their own shading.

---

## Uninstall

1. **Edit › Preferences › Add-ons** → expand **VR Studio for Freebird XR** → **Remove VR Studio Plugin** → confirm.
2. Then remove the add-on itself (**⌄** next to it → **Uninstall**), and restart Blender.

Disabling or removing only the add-on does **not** remove the plugin — VR Studio keeps working in Freebird
until you press *Remove VR Studio Plugin* (or delete `~/.freebird/plugins/vr_studio` yourself).
Backups in `~/.freebird/plugin_backups/` are never deleted automatically.
Materials you already made stay in your .blend files.

---

## Troubleshooting

| Problem | What to check |
|---|---|
| Color / Look / View are not in the Plugins menu | Restart Blender. Check the add-on says *Installed … up to date*. Is Freebird XR 2.15.0 or newer? |
| The add-on says *Freebird XR was not found* | Install and enable Freebird XR, then press **Install VR Studio**. |
| Nothing happens when I press a colour | Select one or more objects first (the panel says *Select an object first*). |
| **Rendered** view is very slow | Rendered uses the scene's render engine. EEVEE is fine in VR; Cycles is very heavy for two eyes. |
| **Glass** does not look transparent | In EEVEE, enable Raytracing in Render Properties. |

*Window › Toggle System Console* shows messages starting with `[vr_studio]` or `[VR Studio setup]` —
please include them when reporting an issue.

Where things go (for reference): the plugin lives in `C:\Users\<you>\.freebird\plugins\vr_studio`
(macOS/Linux: `~/.freebird/plugins/vr_studio`), which is where Freebird XR loads plugins from.

---

## License

GPL-2.0-or-later (same as Freebird XR). See `LICENSE.txt`.
VR Studio is an independent project and is not affiliated with the Freebird XR team.
