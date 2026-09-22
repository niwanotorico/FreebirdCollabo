# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance shader node tree sync test (Issue #7, no VR needed).

    python3 tests/test_material_nodes.py direct | relay | ws | wss    (runs the unit part first)
    python3 tests/test_material_nodes.py unit
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_material_nodes.py direct

Covers, in both directions: node add / delete / move / rename-by-type, node properties (enum / bool / float),
input values, links (including re-wiring the Material Output through a Mix Shader), ColorRamp elements,
Image Texture by reference (path that exists here -> loaded, path that does not -> node stays empty, no crash),
unsupported nodes (Node Group) left alone on both sides, concurrent edits of one node, pre-0.11 payloads,
and that an idle session sends no material traffic (sync-loop check).
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _idle, _wait  # noqa: E402
from test_materials import MAT_TYPES, _counts, _image, _join, _session, _step  # noqa: E402
from test_sync import ROOT, STATE, _read_state, _write_state  # noqa: E402


def _tree(name):
    import bpy

    mat = bpy.data.materials.get(name)
    return mat.node_tree if mat is not None else None


def _node(mat, name):
    tree = _tree(mat)
    return tree.nodes.get(name) if tree is not None else None


def _nt(name):
    """Comparable node tree state: what serialize_node_tree gives, minus the parts that legitimately differ
    between the two sides (unsupported local nodes, image references one side cannot resolve)."""
    from freebird_collab import object_data

    tree = _tree(name)
    if tree is None:
        return None
    nt = object_data.serialize_node_tree(tree)
    nt.pop("x", None)
    for state in nt["nodes"].values():
        state.pop("img", None)  # references are checked explicitly; an image missing on one side is legitimate
    return nt


def _links(name):
    nt = _nt(name)
    return set(tuple(l) for l in nt["links"]) if nt else set()


def _val(mat, node, socket):
    n = _node(mat, node)
    if n is None:
        return None
    v = n.inputs[socket].default_value
    return round(v, 3) if isinstance(v, float) else tuple(round(x, 3) for x in v)


def _ramp(mat, node):
    n = _node(mat, node)
    if n is None:
        return None
    return [(round(e.position, 3), tuple(round(c, 2) for c in e.color)) for e in n.color_ramp.elements]


def _tree_ok(name, want):
    got = _nt(name)
    return got is not None and got["nodes"] == want["nodes"] and got["links"] == want["links"]


def _build_host_graph(mat):
    """Noise -> ColorRamp -> Base Color, TexCoord -> Mapping -> Noise, Noise -> Bump -> Normal."""
    tree = mat.node_tree
    bsdf = tree.nodes["Principled BSDF"]
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    noise.location = (-600, 200)
    noise.noise_dimensions = "4D"
    noise.inputs["Scale"].default_value = 7.5
    noise.inputs["W"].default_value = 1.25
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.name = "Ramp"
    ramp.location = (-300, 200)
    ramp.color_ramp.elements.new(0.5)
    ramp.color_ramp.elements[1].color = (1.0, 0.0, 0.0, 1.0)
    ramp.color_ramp.elements[2].color = (0.0, 0.0, 1.0, 1.0)
    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.name = "Coord"
    coord.location = (-1100, 200)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.name = "Map"
    mapping.location = (-900, 200)
    mapping.vector_type = "TEXTURE"
    mapping.inputs["Scale"].default_value = (2.0, 2.0, 1.0)
    bump = tree.nodes.new("ShaderNodeBump")
    bump.name = "Bump"
    bump.location = (-300, -300)
    bump.invert = True
    bump.inputs["Strength"].default_value = 0.35
    tree.links.new(coord.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    blend = os.path.join(tempfile.gettempdir(), "collab_nodes_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    s = _session(mode, "host")
    _wait(lambda: s.uid is not None, 10, s, "welcome")
    _write_state(room=s.room)
    _step(s, "guest_ready")
    cube = bpy.data.objects["Cube"]

    # 1. procedural graph host -> guest
    mat = bpy.data.materials.new("NodeMat")
    cube.data.materials.clear()
    cube.data.materials.append(mat)
    _build_host_graph(mat)
    _write_state(h1=_nt("NodeMat"))
    _step(s, "g1", "guest has the graph")

    # 2. guest re-wires the output through Mix Shader + Emission, moves a node, edits the ramp
    _step(s, "g2", "guest finished its edits")
    _wait(lambda: _node("NodeMat", "Mix") is not None and _node("NodeMat", "Emit") is not None
          and ("Mix", "Shader", "Material Output", "Surface") in _links("NodeMat")
          and _ramp("NodeMat", "Ramp") is not None and _ramp("NodeMat", "Ramp")[1][1] == (0.0, 1.0, 0.0, 1.0)
          and tuple(_node("NodeMat", "Noise").location) == (-650.0, 250.0), 30, s, "guest's rewiring")
    assert ("Principled BSDF", "BSDF", "Material Output", "Surface") not in _links("NodeMat"), "old link must go"
    assert _val("NodeMat", "Emit", "Strength") == 4.0
    assert _tree_ok("NodeMat", _read_state()["g2"]), "host and guest graphs differ after step 2"
    _write_state(h2=True)

    # 3. host deletes Bump, changes ramp interpolation, adds Voronoi / Wave / Glass / Transparent
    tree = mat.node_tree
    tree.nodes.remove(tree.nodes["Bump"])
    tree.nodes["Ramp"].color_ramp.interpolation = "CONSTANT"
    vor = tree.nodes.new("ShaderNodeTexVoronoi")
    vor.name = "Voro"
    vor.feature = "SMOOTH_F1"
    vor.voronoi_dimensions = "2D"
    wave = tree.nodes.new("ShaderNodeTexWave")
    wave.name = "Wave"
    wave.wave_type = "RINGS"
    wave.rings_direction = "SPHERICAL"
    glass = tree.nodes.new("ShaderNodeBsdfGlass")
    glass.name = "Glass"
    glass.inputs["IOR"].default_value = 1.33
    trans = tree.nodes.new("ShaderNodeBsdfTransparent")
    trans.name = "Trans"
    mix2 = tree.nodes.new("ShaderNodeMixShader")
    mix2.name = "Mix2"
    tree.links.new(wave.outputs["Fac"], mix2.inputs["Fac"])
    tree.links.new(glass.outputs["BSDF"], mix2.inputs[1])
    tree.links.new(trans.outputs["BSDF"], mix2.inputs[2])
    tree.links.new(mix2.outputs["Shader"], tree.nodes["Mix"].inputs[2])  # replaces Emission in the guest's mix
    tree.links.new(vor.outputs["Distance"], tree.nodes["Noise"].inputs["Distortion"])
    _write_state(h3=_nt("NodeMat"))
    _step(s, "g3", "guest saw deletion / props / new nodes")

    # 4. image texture by reference: a file that exists on both sides (same machine here) and one that does not
    img = _image("RefTex", (0.0, 1.0, 0.0, 1.0))  # saved to a temp file both processes can read
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.name = "RefTex"
    tex.image = img
    tex.interpolation = "Closest"
    tex.extension = "CLIP"
    tree.links.new(tex.outputs["Color"], tree.nodes["Emit"].inputs["Color"])
    ghost = bpy.data.images.new("Ghost", 4, 4)
    ghost.filepath_raw = os.path.join(tempfile.gettempdir(), "collab_nodes_does_not_exist", "ghost.png")
    ghost.source = "FILE"
    gtex = tree.nodes.new("ShaderNodeTexImage")
    gtex.name = "GhostTex"
    gtex.image = ghost
    tree.links.new(gtex.outputs["Color"], tree.nodes["Glass"].inputs["Color"])
    _write_state(h4=_nt("NodeMat"), ref_path=img.filepath_raw)
    _step(s, "g4", "guest resolved the image reference")

    # 5. unsupported nodes: a Node Group on the host must not reach the guest, and the guest's own group survives
    grp = bpy.data.node_groups.new("HostGroup", "ShaderNodeTree")
    gn = tree.nodes.new("ShaderNodeGroup")
    gn.name = "HostGrp"
    gn.node_tree = grp
    tree.nodes["Noise"].inputs["Detail"].default_value = 3.0  # something else to sync in the same message
    _step(s, "g5", "guest saw the change next to the group node")
    _wait(lambda: _val("NodeMat", "Wave", "Scale") == 9.0, 30, s, "guest edit after its own group node")
    assert _node("NodeMat", "GuestGrp") is None, "guest's group node must not be built here"
    assert _node("NodeMat", "HostGrp") is not None

    # 6. concurrent edit of one node, different inputs: both survive
    go = time.time() + 1.5
    _write_state(go=go)
    _wait(lambda: time.time() >= go, 5, s, "go")
    tree.nodes["Noise"].inputs["Scale"].default_value = 11.0
    _idle(s, 3.0)
    assert _val("NodeMat", "Noise", "Scale") == 11.0 and _val("NodeMat", "Noise", "Roughness") == 0.8, "concurrent edits lost"
    _write_state(h6=_nt("NodeMat"))
    _step(s, "g6", "guest converged")

    # 7. idle: no material traffic
    before = _counts(s)
    _idle(s, 5.0)
    assert _counts(s) == before, f"idle sent material traffic: {before} -> {_counts(s)}"
    print("HOST node tx:", before)
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST NODES PASS")


def run_guest(mode):
    import bpy

    s = _session(mode, "guest")
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    _join(s, mode)
    _write_state(guest_ready=True)

    # 1
    _step(s, "h1")
    _wait(lambda: _tree_ok("NodeMat", _read_state()["h1"]), 30, s, "host's graph")
    noise = _node("NodeMat", "Noise")
    assert noise.noise_dimensions == "4D" and _val("NodeMat", "Noise", "Scale") == 7.5 and _val("NodeMat", "Noise", "W") == 1.25
    assert _node("NodeMat", "Map").vector_type == "TEXTURE" and _val("NodeMat", "Map", "Scale") == (2.0, 2.0, 1.0)
    assert _node("NodeMat", "Bump").invert and _val("NodeMat", "Bump", "Strength") == 0.35
    assert _ramp("NodeMat", "Ramp") == [(0.0, (0.0, 0.0, 0.0, 1.0)), (0.5, (1.0, 0.0, 0.0, 1.0)), (1.0, (0.0, 0.0, 1.0, 1.0))]
    assert tuple(noise.location) == (-600.0, 200.0)
    _write_state(g1=True)

    # 2
    tree = _tree("NodeMat")
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.name = "Emit"
    emit.inputs["Strength"].default_value = 4.0
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = "Mix"
    mix.inputs["Fac"].default_value = 0.25
    out = tree.nodes["Material Output"]
    tree.links.remove(out.inputs["Surface"].links[0])
    tree.links.new(tree.nodes["Principled BSDF"].outputs["BSDF"], mix.inputs[1])
    tree.links.new(emit.outputs["Emission"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    noise.location = (-650, 250)
    tree.nodes["Ramp"].color_ramp.elements[1].color = (0.0, 1.0, 0.0, 1.0)
    bpy.context.view_layer.update()
    _idle(s, 1.0)
    _write_state(g2=_nt("NodeMat"))
    _step(s, "h2")

    # 3
    _step(s, "h3")
    _wait(lambda: _tree_ok("NodeMat", _read_state()["h3"]), 30, s, "host's step 3")
    assert _node("NodeMat", "Bump") is None
    assert tree.nodes["Ramp"].color_ramp.interpolation == "CONSTANT"
    assert tree.nodes["Voro"].feature == "SMOOTH_F1" and tree.nodes["Voro"].voronoi_dimensions == "2D"
    assert tree.nodes["Wave"].wave_type == "RINGS" and tree.nodes["Wave"].rings_direction == "SPHERICAL"
    assert _val("NodeMat", "Glass", "IOR") == 1.33 and tree.nodes["Trans"].type == "BSDF_TRANSPARENT"
    assert ("Mix2", "Shader", "Mix", "Shader_001") in _links("NodeMat") and ("Emit", "Emission", "Mix", "Shader_001") not in _links("NodeMat")
    _write_state(g3=True)

    # 4
    _step(s, "h4")
    _wait(lambda: _node("NodeMat", "RefTex") is not None and _node("NodeMat", "RefTex").image is not None
          and _node("NodeMat", "GhostTex") is not None, 30, s, "image reference")
    ref = _node("NodeMat", "RefTex")
    assert ref.interpolation == "Closest" and ref.extension == "CLIP"
    assert os.path.normcase(bpy.path.abspath(ref.image.filepath)) == os.path.normcase(_read_state()["ref_path"]), ref.image.filepath
    assert _node("NodeMat", "GhostTex").image is None, "an image that is not here stays empty (reference only)"
    _wait(lambda: _tree_ok("NodeMat", _read_state()["h4"]), 30, s, "host's step 4")
    _write_state(g4=True)

    # 5
    _wait(lambda: _val("NodeMat", "Noise", "Detail") == 3.0, 30, s, "host edit next to a group node")
    assert _node("NodeMat", "HostGrp") is None, "host's group node must not be built here"
    ggrp = bpy.data.node_groups.new("GuestGroup", "ShaderNodeTree")
    gnode = tree.nodes.new("ShaderNodeGroup")
    gnode.name = "GuestGrp"
    gnode.node_tree = ggrp
    tree.nodes["Wave"].inputs["Scale"].default_value = 9.0
    _write_state(g5=True)

    # 6
    _step(s, "go")
    go = _read_state()["go"]
    _wait(lambda: time.time() >= go, 5, s, "go")
    tree.nodes["Noise"].inputs["Roughness"].default_value = 0.8
    _step(s, "h6")
    assert _val("NodeMat", "Noise", "Scale") == 11.0 and _val("NodeMat", "Noise", "Roughness") == 0.8, "concurrent edits lost"
    assert _tree_ok("NodeMat", _read_state()["h6"]), "diverged after concurrent edit"
    assert _node("NodeMat", "GuestGrp") is not None, "local group node must survive remote updates"
    _write_state(g6=True)

    # 7
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 4.0)
    assert _counts(s) == before, f"idle sent material traffic: {before} -> {_counts(s)}"
    print("GUEST node tx:", before)
    _step(s, "host_done")
    s.leave()
    print("GUEST NODES PASS")


def run_unit():
    """Single process: serialize -> apply must reproduce the graph; deltas only carry what changed;
    unsupported nodes and links survive; a pre-0.11 payload (no nt) still applies."""
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data as od

    src = bpy.data.materials.new("Src")
    _build_host_graph(src)
    tree = src.node_tree
    grp = bpy.data.node_groups.new("G", "ShaderNodeTree")
    gn = tree.nodes.new("ShaderNodeGroup")
    gn.name = "Grp"
    gn.node_tree = grp
    rgb = tree.nodes.new("ShaderNodeRGB")
    rgb.name = "RGB"
    rgb.outputs[0].default_value = (0.1, 0.2, 0.3, 1.0)
    tree.links.new(rgb.outputs[0], tree.nodes["Principled BSDF"].inputs["Emission Color"])
    item = od.serialize_material(src)
    nt = item["nt"]
    assert nt["x"] == ["Grp"] and "Grp" not in nt["nodes"], nt.get("x")
    assert nt["nodes"]["Noise"]["pr"]["noise_dimensions"] == "4D" and nt["nodes"]["Bump"]["pr"]["invert"] is True
    assert nt["nodes"]["RGB"]["o"]["Color"] == [0.1, 0.2, 0.3, 1.0]
    assert len(nt["nodes"]["Ramp"]["ramp"]["el"]) == 3
    assert ["Ramp", "Color", "Principled BSDF", "Base Color"] in nt["links"]

    dst, missing = od.apply_material(dict(item, n="Dst"))
    assert not missing
    got = od.serialize_material(dst)
    assert got["nt"]["nodes"] == nt["nodes"] and got["nt"]["links"] == nt["links"], "round trip"
    assert "x" not in got["nt"], "the group node is not rebuilt"
    assert dst.node_tree.nodes["Noise"].noise_dimensions == "4D"
    assert dst.node_tree.nodes["Ramp"].color_ramp.elements[1].position == 0.5

    # second apply: no-op
    before = od.material_digest(dst)
    n_nodes = len(dst.node_tree.nodes)
    od.apply_material(dict(od.serialize_material(src), n="Dst"))
    assert od.material_digest(dst) == before and len(dst.node_tree.nodes) == n_nodes

    # delta: only the changed node keys, deletions, links when changed
    tree.nodes["Noise"].inputs["Scale"].default_value = 9.0
    tree.nodes["Bump"].invert = False
    tree.nodes.remove(tree.nodes["Map"])
    new = od.serialize_material(src)
    delta = od.material_delta(item, new)
    d = delta["nt"]
    assert d["d"] == 1 and set(d["nodes"]) == {"Noise", "Bump"} and d["del"] == ["Map"], d
    assert d["nodes"]["Noise"] == {"t": "ShaderNodeTexNoise", "i": {"Scale": 9.0}}, d["nodes"]["Noise"]
    assert d["nodes"]["Bump"] == {"t": "ShaderNodeBump", "pr": {"invert": False}}
    assert ["Coord", "UV", "Map", "Vector"] not in d["links"]
    # the delta applied on top of a receiver that has its OWN unsupported node and its own extra link keeps both
    dgrp = dst.node_tree.nodes.new("ShaderNodeGroup")
    dgrp.name = "LocalGrp"
    dgrp.node_tree = grp
    od.apply_material(dict(delta, n="Dst"))
    got = od.serialize_material(dst)
    assert got["nt"]["nodes"] == new["nt"]["nodes"] and got["nt"]["links"] == new["nt"]["links"]
    assert dst.node_tree.nodes.get("LocalGrp") is not None
    assert od.material_delta(new, new).keys() == {"n"}, "no change -> no delta"

    # full state applied later (host re-send) must still keep the receiver's unsupported node
    od.apply_material(dict(new, n="Dst"))
    assert dst.node_tree.nodes.get("LocalGrp") is not None and "Map" not in dst.node_tree.nodes

    # pre-0.11 payload: no nt -> the Principled path alone, tree otherwise untouched
    legacy = {k: v for k, v in new.items() if k != "nt"} | {"n": "Dst"}
    n_nodes = len(dst.node_tree.nodes)
    od.apply_material(legacy)
    assert len(dst.node_tree.nodes) == n_nodes
    # an unknown node type in the payload is skipped, the rest of the message still applies
    weird = {"n": "Dst", "nt": {"d": 1, "nodes": {"Nope": {"t": "ShaderNodeDoesNotExist", "l": [0, 0]},
                                                  "Noise": {"t": "ShaderNodeTexNoise", "i": {"Scale": 2.0}}}}}
    od.apply_material(weird)
    assert dst.node_tree.nodes.get("Nope") is None and round(dst.node_tree.nodes["Noise"].inputs["Scale"].default_value, 3) == 2.0
    # a node whose type changed under the same name is rebuilt
    od.apply_material({"n": "Dst", "nt": {"d": 1, "nodes": {"Noise": {"t": "ShaderNodeTexVoronoi", "l": [1, 2]}}}})
    assert dst.node_tree.nodes["Noise"].bl_idname == "ShaderNodeTexVoronoi"
    # image reference: unknown path -> empty node, reported once, no exception
    od.apply_material({"n": "Dst", "nt": {"d": 1, "nodes": {"Tex": {"t": "ShaderNodeTexImage", "l": [0, 0],
                       "img": {"name": "nowhere", "path": "/no/such/dir/nowhere.png", "src": "FILE"}}}}})
    assert dst.node_tree.nodes["Tex"].image is None
    print("UNIT NODES PASS")


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
    from test_sync import MODE_URLS, PORT

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
    print(f"\n=== NODES {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
