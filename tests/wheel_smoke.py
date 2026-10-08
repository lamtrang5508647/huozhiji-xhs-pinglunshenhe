"""Install a wheel in a clean environment, outside the checkout, on any OS."""
import os
import argparse
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--wheel", type=Path)
arguments = parser.parse_args()
wheels = [arguments.wheel.resolve()] if arguments.wheel else list((ROOT / "dist").glob("*.whl"))
if len(wheels) != 1:
    raise SystemExit("Expected exactly one wheel in dist; use a clean build directory.")
env = os.environ.copy()
env.pop("PYTHONPATH", None)
env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
with tempfile.TemporaryDirectory(prefix="audit-wheel-") as directory:
    outside = Path(directory) / "中文 空格 安装测试"
    outside.mkdir()
    environment = outside / "venv"
    venv.create(environment, with_pip=True)
    binary = environment / ("Scripts" if os.name == "nt" else "bin")
    python = binary / ("python.exe" if os.name == "nt" else "python")
    cli = binary / ("huozhiji-audit.exe" if os.name == "nt" else "huozhiji-audit")
    def run(arguments):
        subprocess.run(arguments, cwd=outside, env=env, check=True, timeout=180)
    run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])])
    run([str(cli), "doctor"])
    for adapter in ("run", "login", "capture-xhs", "capture-douyin", "maintain"):
        run([str(cli), adapter, "--help"])
    # This script resides in tests, not the source package: imports must use the wheel.
    run([str(python), str(ROOT / "tests" / "test_portable_api.py")])
print("Installed-wheel SDK, CLI, adapters and Chinese paths passed.")
