# SPDX-License-Identifier: GPL-2.0-or-later
"""Run an existing bpy test role through Blender's --python entry point."""

import os
import runpy
import sys


args = sys.argv[sys.argv.index("--") + 1:]
if not args:
    raise SystemExit("usage: blender ... --python blender_runner.py -- TEST [ARGS...]")

script = os.path.abspath(args[0])
sys.argv = [script, *args[1:]]
runpy.run_path(script, run_name="__main__")
