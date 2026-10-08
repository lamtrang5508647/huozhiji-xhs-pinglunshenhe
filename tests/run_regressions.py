"""Portable suite launcher: no Bash, shell globbing, or /tmp assumptions."""
import compileall
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "comment-review-audit" / "scripts"
env = os.environ.copy()
env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env, check=True)
for test in sorted(SCRIPTS.glob("test_*.py")):
    print(f"Running {test.name}", flush=True)
    subprocess.run([sys.executable, str(test)], cwd=ROOT, env=env, check=True)
if not compileall.compile_dir(str(SCRIPTS), quiet=1):
    raise SystemExit("adapter syntax check failed")
if not compileall.compile_dir(str(ROOT / "huozhiji_audit"), quiet=1):
    raise SystemExit("SDK syntax check failed")
print("All offline regressions passed.")
