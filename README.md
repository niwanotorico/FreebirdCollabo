# FreebirdCollabo

**Real-time multi-user collaboration for Blender — with or without VR.**

[日本語版 README](README_JA.md)

> ⚠️ **Public Alpha (v0.11.1)** — FreebirdCollabo is still in early development. Please back up any important `.blend` files before using it.

FreebirdCollabo is a Blender add-on that keeps a single Blender scene in sync across multiple Blender instances in real time.

- 🖥️ **No VR headset required.** On a regular desktop, it works as a remote co-editing layer for Blender.
- 🥽 **VR is optional.** Pair it with Freebird XR and you can use the same workflow from inside VR.

Inspired by Gravity Sketch's Co-Creation, the goal is to let people build the same 3D space together — right inside Blender.

## Quick Start — 3 steps

> **You don't need to configure any connection settings.**
> The Relay URL is preconfigured, so leave **Relay URL** and **Check Relay** as they are.
> Everyone just enters the **same Room Code** and joins.

1. **Everyone:** Download `freebird_collab_addon.zip` from the [Releases](https://github.com/niwanotorico/FreebirdCollabo/releases) page. In Blender, go to **Edit > Preferences > Add-ons > Install from Disk**, select the zip, and enable **Freebird Collaboration Layer**.
2. **Host (the person creating the room):** In the 3D Viewport, press `N` → open the **COLLAB** tab → click **Create Room**. Share the **6-character Room Code** that appears (via Discord or similar).
3. **Guests:** In the same **COLLAB** tab, type the Room Code into the **Code** field → click **Join Room**.

Once the host's scene loads on your side, you're connected. From then on, everyone's edits show up for everyone in real time.

## Overview

- A small, standalone Blender add-on, `freebird_collab` (Freebird XR itself is not modified).
- The host's Blender scene is authoritative. When someone joins, the `.blend` is sent once; after that, only changes are synced.
- Multiple people can join the same room, and everyone's changes sync in both directions.
- You can see other people's **selected objects** and **active tool** in the desktop 3D Viewport. With Freebird XR, their **head (HMD), both hands, and pointer rays** are also drawn — in both the VR view and on the desktop.
- The UI is just **Create Room / Join Room / Leave Room** in the `COLLAB` panel (N panel). With Freebird XR, you can also use them from the VR menu.

## What gets synced (v0.11.1)

Everything below has been verified on real machines with Blender 5.2.

| Category | What syncs |
| --- | --- |
| Objects | Transform (changed objects only, 30 Hz), create / delete, Duplicate, Rename, parent / child hierarchy |
| Meshes | Mesh edits (vertices / faces — live, even in Edit Mode), UVs |
| Other object data | Changes to Curve, Text, Light, Camera, and Empty data (changed objects only, up to 5 Hz, with size-based bandwidth limiting) |
| Grease Pencil | Create / draw / edit (layers, frames, strokes, points, solid materials) |
| Materials | Create / delete / Rename, assignment to objects and material slots, main Principled BSDF inputs (Base Color / Metallic / Roughness / Alpha / IOR / Emission / Coat / Sheen / Transmission, and more), Render Method, Backface Culling, Viewport Display |
| Textures | Image Textures connected to Base Color and other Principled inputs (each image is sent only once per session) |
| Material Node Tree | Built-in Blender Shader Nodes: add / delete, type, name, location, input values, node-specific properties, links, and the Material Output connection (Principled / Emission / Diffuse / Glass / Transparent / Mix Shader / ColorRamp / Noise / Voronoi / Wave / Mapping / Texture Coordinate / Normal Map / Bump, etc.). For Image Texture nodes, only the reference (name / path / Color Space) is synced |
| Pose | Pose Bone transforms in Pose Mode (Location / Rotation / Scale / Rotation Mode, up to 15 Hz) |
| Armature / Bone | Creating an Armature while connected, adding / deleting / renaming Bones, head / tail / roll, parent / connected (live, even in Edit Mode) |
| Skinning | Create / delete / rename Vertex Groups, per-vertex weights (live during Weight Paint, including Automatic Weights results), the Armature Modifier with its target and main settings. Meshes deform to match the pose on the other side, too |
| Import | GLB import while connected (with hierarchy, multiple meshes, UVs, materials, and textures) |
| Presence | Selection, Active Tool (Freebird tools such as `fb:draw.stroke` when available, otherwise Blender tools), and — with Freebird XR — HMD, left / right controllers (20 Hz), and pointer rays |

For how simultaneous edits to the same thing are handled (e.g. changes are held while someone is in Edit Mode or Weight Paint, and whoever exits last wins) and for per-feature details, see the documents in [`docs/`](docs/).

## Known limitations

- Moving objects between Collections is not supported yet.
- Undo is not shared — your Undo only affects your own Blender.
- No permission management: anyone who knows the Room Code can join and edit.
- No built-in voice chat (use Discord or similar).
- Newer sync features, such as Material Node Tree, are still alpha quality.
- Not synced:
  - Shader nodes: Node Group contents, OSL Script, custom / third-party add-on nodes, Frame, curve shapes such as RGB Curves, and image data of Image Textures (when synced via the Node Tree)
  - Geometry Nodes, Compositor, World Shader
  - Rigging: Constraints / IK / Drivers, Bone Envelope shape, Custom Shape, Bone Collection details, Rigify
  - Animation: Keyframes / Actions / NLA
  - Normals, and modifiers other than Armature
- Not yet optimized for large groups.
- If you need full data sync, the design leaves room to combine it with Multiuser 0.8.x (see [`docs/research.md`](docs/research.md)).

## Installation & settings

Do the same on every participant's PC.

1. Go to **Edit > Preferences > Add-ons > Install from Disk**, install `freebird_collab_addon.zip`, and enable **Freebird Collaboration Layer**.
   (Alternatively, copy the `freebird_collab` folder into `C:\Users\<name>\AppData\Roaming\Blender Foundation\Blender\5.2\scripts\addons\` and enable it.)
2. *(Optional)* Set your **Display Name** and **My Color** in the add-on preferences. **Leave everything else (Connection / Relay URL / Check Relay) as it is.**
   - The Relay URL is prefilled with the default public relay (`wss://freebird-relay.chickenos.workers.dev`). **Participants just enter the same Room Code and join** to connect over the internet.
3. *(Freebird XR users only)* Copy `freebird_plugin/freebird_collab_menu.py` into `C:\Users\<name>\.freebird\plugins\`, then click **Reload All** in Freebird Settings. **Create / Join / Leave Room** will appear under **CUSTOM** in the VR menu.

### Advanced: connection settings (usually not needed)

- **Can't connect?** Click **Check Relay** in the add-on preferences and check that it shows `OK xxx ms`. If it shows `NG`, check your network (e.g. a corporate network or firewall) and look at the `[collab]` lines under **Window > Toggle System Console**.
- **Using your own relay:** Replace the Relay URL with your own relay's URL (`wss://...`, or on a LAN, `ws://192.168.x.x:7788` pointing to the PC running `python3 relay/collab_relay.py`). **All participants must use the same Relay URL.** See [`docs/internet-relay.md`](docs/internet-relay.md) for how to set up a relay. Click the ↺ button next to the field (or clear the field) to switch back to the default relay.
- **Direct (IP address):** Set **Connection** to **Direct** to skip the relay; the host listens on port 7788. Guests enter the host's IP address (LAN, or a VPN such as Tailscale) in the `Host IP` field instead of the Code field. Intended for LAN use and debugging.
- **Upgrading from v0.11.0 or earlier:** If you had entered a Relay URL manually, that value is kept after the update. Click ↺ if you want to switch to the default relay.
- **Mixing versions:** Add-on versions v0.11.0 and earlier have no default Relay URL. If someone is still on an older version, ask them to update to v0.11.1. (The synced content is the same as in v0.11.0, so mixed versions still work — but older versions require entering the Relay URL manually.)

## Usage

Since the Relay URL is preconfigured, **all you need to do is: the host clicks Create Room, and everyone joins with the same Room Code.**

| Step | Host | Guest |
| --- | --- | --- |
| 1 | Open the `.blend` you want to use as the master scene, then click **Create Room** in the COLLAB panel (or the VR menu) | |
| 2 | Share the **6-character Room Code** shown in the panel (via Discord or similar) | Enter the Room Code in the COLLAB panel's Code field and click **Join Room** (Join Room in the VR menu uses the code in this field) |
| 3 | | The host's scene loads into your Blender |
| 4 | Everyone's edits show up in real time. You'll see others' selection outlines and a "Name / Tool / Selected: Cube" label. If Freebird XR is running, you'll also see their head (wire cube), hands (pyramids), and pointer rays | |
| 5 | Click **Save Master Scene** to save the master scene (if a guest clicks it, the scene is saved on the host's side) | |
| 6 | **Leave Room** | **Leave Room** |

Voice chat is not built in — use Discord or similar.

## Project structure

```
FreebirdCollabo/
├─ freebird_collab/          The Blender add-on (place the whole folder in scripts/addons)
│   ├─ __init__.py           UI, operators, timers, public API
│   ├─ session.py            Sync logic (master scene sharing / Transform / object data / materials / Pose / Presence)
│   ├─ object_data.py        Serializes and applies in place the contents of Mesh/Curve/Grease Pencil/Text/Light/Camera/Empty/Armature, materials, and Node Trees
│   ├─ presence.py           GPU drawing of others' head, hands, rays, selection outlines, and labels
│   ├─ hub.py                Room hub (the relay server, and the host's built-in server in Direct mode)
│   ├─ link.py               TCP client (receive thread → queue to the main thread)
│   ├─ protocol.py           Message definitions (4-byte length + JSON)
│   └─ ws.py                 WebSocket implementation (stdlib only) — so the relay can run on free HTTP hosting
├─ freebird_collab_addon.zip  Distribution zip of the add-on (v0.11.1; install via Preferences > Add-ons > Install from Disk)
├─ freebird_plugin/
│   └─ freebird_collab_menu.py   Plugin that adds COLLAB buttons to the Freebird VR menu (Freebird XR only)
├─ relay/collab_relay.py     Relay server (no dependencies; auto-detects TCP and WebSocket on the same port)
├─ relay/cloudflare/         Cloudflare Workers + Durable Objects relay (free, fixed URL, always on)
├─ relay/quick_tunnel.bat/.py  Starts the relay + a cloudflared quick tunnel on the host PC in one go (no account needed)
├─ relay/relay_check.py      CLI to check that a relay is reachable
├─ tests/test_sync.py        Automated sync test with two Blender instances (no VR needed)
├─ tests/test_data.py        Object data sync test (Edit Mode vertex edits, all types, deletion, payload size)
├─ tests/test_grease_pencil.py  Grease Pencil sync test (create, draw, edit, materials, bidirectional)
├─ tests/test_materials.py   Material sync test (create/delete/rename, slots, Principled values, image textures, reconnect, simultaneous edits, bidirectional)
├─ tests/test_material_nodes.py  Shader Node Tree sync test (add/delete/move nodes, properties, links, Mix Shader/Emission/Glass, ColorRamp, Image Texture references, Node Groups excluded, simultaneous edits, bidirectional)
├─ tests/test_pose.py        Pose Mode bone transform sync test (Location/Rotation/Scale bidirectional, multiple bones, bones added while connected, reconnect, no traffic when idle)
├─ tests/test_armature.py    Armature / Bone structure sync test (new Armature while connected, add/delete/rename bones, head/tail/roll, parent/connected, held changes during Edit Mode, reconnect, Pose interaction)
├─ tests/test_skinning.py    Vertex Group / Skinning sync test (Automatic Weights, Weight Paint, add/rename/delete groups, Armature Modifier, matching mesh deformation on the other side, held changes during Weight Paint, reconnect)
├─ tests/test_glb.py         GLB import sync test during a session (bidirectional, hierarchy, multiple meshes, editing after import)
├─ tests/test_relay_default.py Relay URL default test (room-code-only join, custom URL, empty-field fallback, Reset, Direct unaffected)
├─ tests/test_glb_real.py    Import sync test with real GLB files (set paths via COLLAB_GLBS)
├─ docs/research.md          Research notes, architecture, risks
├─ docs/internet-relay.md    For people who want to host their own relay (Cloudflare Workers / quick tunnel / Python relay; most users can skip this)
├─ docs/machida-merge-v0.4.md Review of merged external patch proposals (adopted / rejected / reasons), list of sync targets
├─ docs/glb-import-v0.5.md   GLB import sync during a session (root cause, hierarchy/material support, unsupported items)
├─ docs/textures-v0.6.md     Base Color texture + UV transfer (no image resending, unsupported items)
├─ docs/textures-v0.6.1-sanze.md Comparison of texture transfer with two real GLB files
├─ docs/material-sync-v0.7.md Material sync (scope, messages, loop prevention, real-machine test steps)
├─ docs/pose-sync-v0.8.md    Pose Mode bone transform sync (scope, loop prevention, reconnect, real-machine test steps)
├─ docs/armature-sync-v0.9.md Armature / Bone structure sync (scope, Edit Mode conflict handling, ordering with Pose, real-machine test steps)
├─ docs/skinning-sync-v0.10.md Vertex Group / Skinning sync (scope, handling during Weight Paint, Armature Modifier reference resolution, real-machine test steps)
└─ docs/material-node-sync-v0.11.md Shader Node Tree sync (payload, diffs, Image Texture references, unsupported nodes, backward compatibility, real-machine test steps)
```

## Tests

No VR headset is needed to run the tests.

```
pip install bpy==5.0.1          # use Blender as the pip "bpy" module
python3 tests/test_sync.py direct   # LAN direct
python3 tests/test_sync.py relay    # tcp relay
python3 tests/test_sync.py ws       # websocket relay
python3 tests/test_sync.py wss      # websocket over TLS (local self-signed terminator)
python3 tests/test_data.py direct   # object data sync (same modes as above)
python3 tests/test_grease_pencil.py direct  # Grease Pencil create/draw/edit sync
python3 tests/test_materials.py direct  # material sync (same modes as above; "unit" = single-process part only)
python3 tests/test_material_nodes.py direct  # shader node tree sync
python3 tests/test_glb.py direct    # GLB import during a session (same modes as above)
python3 tests/test_pose.py direct   # Pose Mode bone transform sync
python3 tests/test_armature.py direct  # Armature / Bone structure sync
python3 tests/test_skinning.py direct  # Vertex Group / Skinning sync
python3 tests/test_relay_default.py    # Relay URL preset (room-code-only join, custom URL, Reset, Direct unchanged; "live" = also Check Relay)
```

`tests/test_sync.py` launches a host and a guest process (plus a relay) and automatically verifies the full flow: Create → Join → sharing the master scene → two-way sync of moving a Cube → new objects → selection / tool presence → saving on the host. All of these pass.

## Support FreebirdCollabo 🐔

FreebirdCollabo is free and open source.
If you'd like to support continued development, you can do so through the [Amazon Wishlist](https://www.amazon.jp/hz/wishlist/ls/2MNMSWIJ1FCB6?ref_=wl_share).

It's entirely optional — bug reports, feedback, and sharing the project with others are just as appreciated. Thank you!
