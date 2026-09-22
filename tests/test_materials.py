# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance material sync test (Issue #2, no VR needed).

    python3 tests/test_materials.py direct | relay | ws | wss    (runs the unit part first)
    python3 tests/test_materials.py unit
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_materials.py direct

Covers, in both directions: material create / rename / delete, slot assignment and slot count,
Base Color / Metallic / Roughness / Alpha and other Principled BSDF values, render method,
image texture add / swap / remove, materials riding on a new object, guest reconnect,
and that an idle session sends no material traffic at all (sync-loop check).
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _idle, _wait  # noqa: E402
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, STATE, _read_state, _write_state  # noqa: E402

MAT_TYPES = ("mat", "mat_ren", "mat_del", "obj_mats", "img")


def _bsdf(name):
    import bpy

    mat = bpy.data.materials.get(name)
    if mat is None or mat.node_tree is None:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def _val(name, socket):
    node = _bsdf(name)
    if node is None:
        return None
    v = node.inputs[socket].default_value
    return round(v, 3) if isinstance(v, float) else tuple(round(x, 3) for x in v)


def _set(name, **values):
    node = _bsdf(name)
    for socket, v in values.items():
        node.inputs[socket.replace("_", " ")].default_value = v


def _slots(obname):
    import bpy

    ob = bpy.data.objects.get(obname)
    return [s.material.name if s.material else None for s in ob.material_slots] if ob else None


def _image(name, rgba):
    import bpy

    img = bpy.data.images.new(name, 8, 8, alpha=True)
    img.pixels = list(rgba) * 64
    img.filepath_raw = os.path.join(tempfile.gettempdir(), f"collab_mat_{os.getpid()}_{name}.png")
    img.file_format = "PNG"
    img.save()
    img.pack()
    return img


def _tex_image(name, socket):
    """Image feeding a Principled input (through whatever nodes), or None."""
    from freebird_collab import object_data

    node = _bsdf(name)
    texnode = object_data._image_node(node.inputs[socket]) if node else None
    return texnode.image if texnode else None


def _tex_pixel(name, socket):
    img = _tex_image(name, socket)
    return tuple(round(x, 2) for x in img.pixels[0:4]) if img is not None and img.size[0] == 8 else None


def _digests():
    """{material name: digest} of every material that is in use (what a rejoining guest must match)."""
    import bpy
    from freebird_collab import object_data

    return {m.name: object_data.material_digest(m) for m in bpy.data.materials if m.users and not m.is_grease_pencil}


def _counts(s):
    return {t: s.stats["tx_by_type"].get(t, [0, 0])[0] for t in MAT_TYPES}


def _session(mode, role):
    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    if role == "host":
        if mode == "direct":
            assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
        else:
            assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    return s


def _join(s, mode):
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=_read_state()["room"], relay_url=RELAY_URL)
    _wait(lambda: s._scene_loaded_from_host, 30, s, "scene from host")


def _step(s, flag, what=None):
    _wait(lambda: _read_state().get(flag), 40, s, what or flag)


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    blend = os.path.join(tempfile.gettempdir(), "collab_mat_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    s = _session(mode, "host")
    _wait(lambda: s.uid is not None, 10, s, "welcome")
    _write_state(room=s.room)
    _step(s, "guest_ready")
    cube = bpy.data.objects["Cube"]

    # 1. create + assign + Principled values (host -> guest)
    mat = bpy.data.materials.new("HostMat")
    cube.data.materials.clear()
    cube.data.materials.append(mat)
    _set("HostMat", Base_Color=(0.8, 0.1, 0.1, 1.0), Roughness=0.2, Metallic=0.9, Alpha=0.5, Emission_Strength=2.0)
    mat.surface_render_method = "BLENDED"
    _step(s, "g1", "guest saw HostMat")

    # 2. values guest -> host
    _wait(lambda: _val("HostMat", "Base Color") == (0.1, 0.2, 0.9, 1.0) and _val("HostMat", "Roughness") == 0.7
          and _val("HostMat", "Metallic") == 0.1 and _val("HostMat", "Alpha") == 1.0, 30, s, "guest's HostMat values")
    # 3. material created + appended as a second slot by the guest
    _wait(lambda: _slots("Cube") == ["HostMat", "GuestMat"] and _val("GuestMat", "Roughness") == 0.33,
          30, s, "GuestMat in slot 2")
    n_before = len(bpy.data.materials)
    _write_state(h3=True)

    # 4. rename host -> guest
    bpy.data.materials["GuestMat"].name = "Renamed"
    _step(s, "g4", "guest saw rename")
    # 5. rename guest -> host
    _wait(lambda: "HostMat" not in bpy.data.materials and _slots("Cube") == ["HostMat2", "Renamed"], 30, s, "guest's rename")
    assert len(bpy.data.materials) == n_before, "rename must not create or drop a material"
    assert _val("HostMat2", "Roughness") == 0.7

    # 6. image textures host -> guest (Base Color sRGB + Roughness Non-Color)
    tree = bpy.data.materials["Renamed"].node_tree
    for socket, img in (("Base Color", _image("TexA", (1.0, 0.0, 0.0, 1.0))), ("Roughness", _image("TexR", (0.5, 0.5, 0.5, 1.0)))):
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        if socket == "Roughness":
            img.colorspace_settings.name = "Non-Color"
        tree.links.new(node.outputs["Color"], _bsdf("Renamed").inputs[socket])
    _step(s, "g6", "guest saw textures")
    # 7. guest swaps the Base Color image
    _wait(lambda: _tex_pixel("Renamed", "Base Color") == (0.0, 0.0, 1.0, 1.0), 30, s, "guest's texture swap")
    assert _tex_image("Renamed", "Base Color").name == "TexB"
    assert len([n for n in tree.nodes if n.type == "TEX_IMAGE"]) == 2, "swap must reuse the node"
    # 8. host removes the Base Color texture again
    sock = _bsdf("Renamed").inputs["Base Color"]
    tree.links.remove(sock.links[0])
    sock.default_value = (0.2, 0.9, 0.2, 1.0)
    _step(s, "g8", "guest saw texture removal")

    # 9. slot count down (host), then slot reassignment (guest)
    cube.data.materials.pop(index=1)
    _step(s, "g9", "guest saw slot removal")
    _wait(lambda: _slots("Cube") == ["Renamed"], 30, s, "guest's slot reassignment")
    # 10. delete
    bpy.data.materials.remove(bpy.data.materials["HostMat2"])
    _step(s, "g10", "guest saw delete")
    # 11. new object from the guest, material riding on obj_add
    _wait(lambda: _slots("GuestBall") == ["BallMat"] and _val("BallMat", "Metallic") == 1.0
          and _val("BallMat", "Roughness") == 0.05, 30, s, "GuestBall with BallMat")

    # 12. guest leaves; host keeps editing; guest rejoins and must match
    _write_state(h12=True)
    _wait(lambda: not s.peers, 30, s, "guest left")
    _set("Renamed", Roughness=0.61, Metallic=0.25)
    _set("BallMat", Base_Color=(0.9, 0.8, 0.1, 1.0))
    bpy.context.view_layer.update()
    _write_state(host_digests=_digests())
    _step(s, "g12", "guest rejoined and matches")
    _wait(lambda: _val("BallMat", "Alpha") == 0.4, 30, s, "guest edit after rejoin")
    _set("Renamed", Metallic=0.11)
    _step(s, "g12b", "guest saw edit after rejoin")

    # 12c. both sides edit the same material at the same moment: different sliders must both survive,
    #      the same slider must end up equal on both sides (no swapped values)
    go = time.time() + 1.5
    _write_state(go=go)
    _wait(lambda: time.time() >= go, 5, s, "go")
    _set("BallMat", Roughness=0.9, Metallic=0.5)
    _idle(s, 3.0)
    assert _val("BallMat", "Roughness") == 0.9 and _val("BallMat", "Coat Weight") == 0.3, "concurrent edits lost"
    _write_state(host_final=_digests())
    _step(s, "g12c", "guest converged after concurrent edit")

    # 13. idle: nothing material-related may go out (no echo / ping-pong)
    before = _counts(s)
    _idle(s, 5.0)
    assert _counts(s) == before, f"idle sent material traffic: {before} -> {_counts(s)}"
    assert before["mat_ren"] == 1 and before["mat_del"] == 1, before
    print("HOST material tx:", before)
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST MATERIALS PASS")


def run_guest(mode):
    import bpy

    s = _session(mode, "guest")
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    _join(s, mode)
    _write_state(guest_ready=True)

    # 1
    _wait(lambda: _slots("Cube") == ["HostMat"] and _val("HostMat", "Base Color") == (0.8, 0.1, 0.1, 1.0)
          and _val("HostMat", "Roughness") == 0.2 and _val("HostMat", "Metallic") == 0.9
          and _val("HostMat", "Alpha") == 0.5 and _val("HostMat", "Emission Strength") == 2.0
          and bpy.data.materials["HostMat"].surface_render_method == "BLENDED", 30, s, "HostMat")
    assert tuple(round(x, 3) for x in bpy.data.materials["HostMat"].diffuse_color) == (0.8, 0.1, 0.1, 1.0)
    _write_state(g1=True)
    # 2
    _set("HostMat", Base_Color=(0.1, 0.2, 0.9, 1.0), Roughness=0.7, Metallic=0.1, Alpha=1.0)
    # 3
    gm = bpy.data.materials.new("GuestMat")
    _set("GuestMat", Roughness=0.33)
    bpy.data.objects["Cube"].data.materials.append(gm)
    _step(s, "h3")
    n_before = len(bpy.data.materials)
    # 4
    _wait(lambda: "GuestMat" not in bpy.data.materials and _slots("Cube") == ["HostMat", "Renamed"], 30, s, "host's rename")
    assert len(bpy.data.materials) == n_before and bpy.data.materials["Renamed"] == gm
    _write_state(g4=True)
    # 5
    bpy.data.materials["HostMat"].name = "HostMat2"
    # 6
    _wait(lambda: _tex_pixel("Renamed", "Base Color") == (1.0, 0.0, 0.0, 1.0)
          and _tex_pixel("Renamed", "Roughness") == (0.5, 0.5, 0.5, 1.0), 30, s, "textures from host")
    assert _tex_image("Renamed", "Roughness").colorspace_settings.name == "Non-Color"
    assert _tex_image("Renamed", "Base Color").colorspace_settings.name == "sRGB"
    _write_state(g6=True)
    # 7
    from freebird_collab import object_data

    object_data._image_node(_bsdf("Renamed").inputs["Base Color"]).image = _image("TexB", (0.0, 0.0, 1.0, 1.0))
    # 8
    _wait(lambda: not _bsdf("Renamed").inputs["Base Color"].is_linked
          and _val("Renamed", "Base Color") == (0.2, 0.9, 0.2, 1.0), 30, s, "texture removal")
    assert _tex_pixel("Renamed", "Roughness") == (0.5, 0.5, 0.5, 1.0), "the other texture must survive"
    _write_state(g8=True)
    # 9
    _wait(lambda: _slots("Cube") == ["HostMat2"], 30, s, "slot removal")
    _write_state(g9=True)
    bpy.data.objects["Cube"].material_slots[0].material = bpy.data.materials["Renamed"]
    # 10
    _wait(lambda: "HostMat2" not in bpy.data.materials, 30, s, "delete")
    _write_state(g10=True)
    # 11
    bpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=4, location=(3, 0, 0))
    ball = bpy.context.active_object
    ball.name = "GuestBall"
    bm = bpy.data.materials.new("BallMat")
    ball.data.materials.append(bm)
    _set("BallMat", Metallic=1.0, Roughness=0.05)
    # 12
    _step(s, "h12")
    _idle(s, 0.5)
    s.leave()
    bpy.ops.wm.read_factory_settings(use_empty=False)  # a guest that comes back with an unrelated scene
    t0 = time.time()
    while not _read_state().get("host_digests") and time.time() - t0 < 30:
        time.sleep(0.05)
    _join(s, mode)
    want = _read_state()["host_digests"]
    assert _digests() == want, f"after rejoin: {_digests()} != {want}"
    assert _val("Renamed", "Roughness") == 0.61 and _slots("Cube") == ["Renamed"] and _slots("GuestBall") == ["BallMat"]
    _write_state(g12=True)
    _set("BallMat", Alpha=0.4)
    _wait(lambda: _val("Renamed", "Metallic") == 0.11, 30, s, "host edit after rejoin")
    _write_state(g12b=True)
    # 12c
    _step(s, "go")
    go = _read_state()["go"]
    _wait(lambda: time.time() >= go, 5, s, "go")
    _set("BallMat", Metallic=0.6, Coat_Weight=0.3)
    _step(s, "host_final")
    assert _val("BallMat", "Roughness") == 0.9 and _val("BallMat", "Coat Weight") == 0.3, "concurrent edits lost"
    assert _digests() == _read_state()["host_final"], f"diverged: metallic {_val('BallMat', 'Metallic')}"
    _write_state(g12c=True)
    # 13
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 4.0)
    assert _counts(s) == before, f"idle sent material traffic: {before} -> {_counts(s)}"
    print("GUEST material tx:", before)
    _step(s, "host_done")
    s.leave()
    print("GUEST MATERIALS PASS")


def run_unit():
    """Single process: serialize -> apply onto a fresh material must reproduce the state
    (alpha from the Base Color texture, normal map chain, value-only updates, foreign links kept)."""
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data as od

    src = bpy.data.materials.new("Src")
    tree, bsdf = src.node_tree, _bsdf("Src")
    col = tree.nodes.new("ShaderNodeTexImage")
    col.image = _image("UColor", (0.2, 0.4, 0.6, 0.5))
    tree.links.new(col.outputs["Color"], bsdf.inputs["Base Color"])
    tree.links.new(col.outputs["Alpha"], bsdf.inputs["Alpha"])
    nrm = tree.nodes.new("ShaderNodeTexImage")
    nrm.image = _image("UNormal", (0.5, 0.5, 1.0, 1.0))
    nrm.image.colorspace_settings.name = "Non-Color"
    nmap = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(nrm.outputs["Color"], nmap.inputs["Color"])
    tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    noise = tree.nodes.new("ShaderNodeTexNoise")
    tree.links.new(noise.outputs["Fac"] if "Fac" in noise.outputs else noise.outputs[0], bsdf.inputs["Roughness"])
    bsdf.inputs["Metallic"].default_value = 0.75
    item = od.serialize_material(src)
    assert item["tex"]["out"] == "Color" and item["tx"]["Alpha"]["out"] == "Alpha", item
    assert item["tx"]["Normal"]["cs"] == "Non-Color" and item["l"] == ["Roughness"], item
    assert "Roughness" not in item["p"] and item["p"]["Metallic"] == 0.75

    for img in (col.image, nrm.image):  # what store_image() does on the receiving side
        img["collab_id"] = od.image_id(od.image_bytes(img)[0])
    src.name = "SrcAside"
    dst, missing = od.apply_material(item)
    assert dst != src and dst.name == "Src" and not missing
    got = od.serialize_material(dst)
    assert got == item, f"{got} != {item}"  # 0.11+: the noise node travels too (nt), so even "l" matches
    # a pre-0.11 peer sends no "nt": the receiver then has no noise node and Roughness stays a plain value
    legacy = {k: v for k, v in item.items() if k != "nt"} | {"n": "SrcLegacy"}
    leg, missing = od.apply_material(legacy)
    assert not missing and not _bsdf("SrcLegacy").inputs["Roughness"].is_linked
    assert od.serialize_material(leg)["p"]["Roughness"] == 0.5 and _tex_image("SrcLegacy", "Normal") is not None
    d = _bsdf("Src")
    assert d.inputs["Alpha"].links[0].from_node == d.inputs["Base Color"].links[0].from_node, "one node for colour + alpha"
    assert d.inputs["Normal"].links[0].from_node.type == "NORMAL_MAP"

    # second apply is a no-op (nothing may be rebuilt: that would wake the change detector for nothing)
    n_nodes = len(dst.node_tree.nodes)
    before = od.material_digest(dst)
    od.apply_material(od.serialize_material(src) | {"n": "Src"})
    assert len(dst.node_tree.nodes) == n_nodes and od.material_digest(dst) == before

    # delta: only what changed travels
    a = od.serialize_material(dst)
    d.inputs["Metallic"].default_value = 0.25
    dst.surface_render_method = "BLENDED"
    delta = od.material_delta(a, od.serialize_material(dst))
    assert delta == {"n": "Src", "rm": "BLENDED", "p": {"Metallic": 0.25}, "nt": {  # 0.11+: node-level delta too
        "d": 1, "nodes": {"Principled BSDF": {"i": {"Metallic": 0.25}, "t": "ShaderNodeBsdfPrincipled"}}}}, delta

    # unknown texture id -> reported missing, nothing linked
    item2 = {"n": "Lonely", "c": [1, 1, 1, 1], "p": {}, "tex": {"id": "0" * 40, "name": "nope", "ext": "png"}}
    mat2, missing = od.apply_material(item2)
    assert missing == ["0" * 40] and not _bsdf("Lonely").inputs["Base Color"].is_linked

    # pre-0.7 wire format (name + colour only) still works
    mat3, _ = od.apply_material({"n": "Old", "c": [0.3, 0.2, 0.1, 1.0]})
    assert _val("Old", "Base Color") == (0.3, 0.2, 0.1, 1.0)

    # slots: grow, shrink, empty slot, OBJECT link
    cube = bpy.data.objects["Cube"]
    od.apply_slots(cube, [["Src", "DATA"], [None, "DATA"], ["Old", "OBJECT"]])
    assert od.serialize_slots(cube) == [["Src", "DATA"], [None, "DATA"], ["Old", "OBJECT"]]
    od.apply_slots(cube, [["Old", "DATA"]])
    assert od.serialize_slots(cube) == [["Old", "DATA"]]
    print("UNIT MATERIALS PASS")


def _args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def _child_command(mode, role):
    try:
        import bpy

        blender = bpy.app.binary_path
    except ImportError:
        blender = ""
    if blender:
        return [blender, "--background", "--factory-startup", "--python", __file__, "--", mode, role]
    return [sys.executable, __file__, mode, role]


def main():
    args = _args()
    mode = args[0] if args else "direct"
    role = args[1] if len(args) > 1 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)
    if mode == "unit":
        return run_unit()
    rc_u = subprocess.call(_child_command("unit", "-"))
    if rc_u:
        sys.exit(rc_u)
    import glob

    for f in glob.glob(STATE + "*"):
        os.remove(f)
    procs = []
    env = dict(os.environ)
    if mode in MODE_URLS:
        env["COLLAB_RELAY_URL"] = os.environ.get("COLLAB_RELAY_URL") or MODE_URLS[mode]
        if not os.environ.get("COLLAB_RELAY_URL"):
            procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "relay", "collab_relay.py"), str(PORT)]))
            if mode == "wss":
                procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "tls_proxy.py"), str(PORT + 1), str(PORT)]))
                env["SSL_CERT_FILE"] = os.path.join(tempfile.gettempdir(), "collab_test_cert.pem")
            time.sleep(1.0)
    run_mode = "direct" if mode == "direct" else "relay"
    host = subprocess.Popen(_child_command(run_mode, "host"), env=env)
    time.sleep(1.0)
    guest = subprocess.Popen(_child_command(run_mode, "guest"), env=env)
    rc_h, rc_g = host.wait(300), guest.wait(300)
    for p in procs:
        p.terminate()
    print(f"\n=== MATERIALS {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
