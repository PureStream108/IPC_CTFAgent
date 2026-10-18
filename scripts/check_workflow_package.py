"""Build and import the built-in workflows outside the source checkout."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ipc-workflow-package-") as temporary:
        target = Path(temporary)
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(target / "dist")], cwd=root, check=True)
        envdir = target / "venv"
        venv.create(envdir, with_pip=True, system_site_packages=True)
        python = envdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheel = next((target / "dist").glob("*.whl"))
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], check=True)
        code = """
import pathlib, sys
from backend.platform.gzctf import workflow as gz
from backend.platform.tsecbench import workflow as ts
from backend.ops.presets import workflow_templates
assert pathlib.Path(gz.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
assert {t['id'] for t in workflow_templates()} == {'gzctf', 'tsecbench'}
assert gz.GZCTFWorkflowInput(base_url='https://ctf.example', game_id=1).workflow_spec().template_id == 'gzctf'
assert ts.TSECBenchWorkflowInput(base_url='https://benchmark.example').workflow_spec().template_id == 'tsecbench'
print('Installed workflow package smoke passed')
"""
        environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        subprocess.run([str(python), "-c", code], cwd=target, env=environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
