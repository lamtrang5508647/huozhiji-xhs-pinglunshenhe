"""Build the self-contained WorkBuddy skill ZIP from the SDK and adapter sources."""
import argparse
import os
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "comment-review-audit"


def package(output: Path) -> None:
    if output.suffix.lower() != ".zip":
        raise ValueError("Expected a .zip destination")
    source = ROOT / "integrations" / "workbuddy"
    files = {"SKILL.md": source / "SKILL.md", "scripts/audit.py": source / "audit.py"}
    for path in (ROOT / "huozhiji_audit").glob("*.py"):
        files[f"scripts/huozhiji_audit/{path.name}"] = path
    for path in (SKILL / "scripts").iterdir():
        if path.suffix in (".py", ".mjs") and not path.name.startswith("test_"):
            files[f"scripts/huozhiji_audit/_engine/{path.name}"] = path
    for name in ("data-contract.md", "platform-operations.md", "windows-operations.md"):
        files[f"references/{name}"] = SKILL / "references" / name
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, path in sorted(files.items()):
                # Stable entries also allow a rebuilt skill to be compared by hash.
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.output)
    print(args.output.resolve())
