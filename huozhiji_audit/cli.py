"""Stable JSON CLI for local programs; browser adapters are opt-in subprocesses."""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

from . import _engine
from .api import audit_workbook, convert_table, review
from ._engine import table_to_expected
from ._engine.runtime_environment import child_environment, configure_stdio
from . import __version__


def script_path(name):
    spec = importlib.util.find_spec(f"{_engine.__name__}.{name}")
    if spec is None or spec.origin is None:
        raise RuntimeError(f"adapter_not_installed:{name}")
    return Path(spec.origin)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def emit(value, output=None):
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
    sys.stdout.write(encoded)


def doctor():
    from ._engine.browser_environment import find_chrome
    # Standalone adapter modules intentionally use sibling imports. Avoid importing
    # profile_lock here, where the installed package has a different module path.
    try:
        chrome = find_chrome()
    except FileNotFoundError:
        chrome = None
    playwright = importlib.util.find_spec("playwright") is not None
    xhs = importlib.util.find_spec("xhs_cli") is not None
    return {"schema_version": "1.0", "version": __version__, "os": platform.system(),
            "python": platform.python_version(), "offline_ready": True,
            "profile_lock_backend": "msvcrt" if sys.platform == "win32" else "fcntl",
            "network_backend": "powershell" if sys.platform == "win32" else "ip" if sys.platform.startswith("linux") else "scutil",
            "chrome": chrome, "playwright": playwright, "xhs_cli": xhs,
            "live_xhs_dependencies_ready": bool(chrome and playwright and xhs),
            "live_session_verified": False, "feishu_required": False, "codex_required": False,
            "note": "Dependency checks do not verify login or comment accessibility."}


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser(description="Independent comment audit CLI; stdout is JSON, diagnostics are stderr.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local dependencies without opening a browser")
    convert = commands.add_parser("convert", help="Convert a source table to expected.json")
    convert.add_argument("--source", required=True)
    convert.add_argument("--output")
    convert.add_argument("--sheet")
    compare = commands.add_parser("review", help="Compare expected.json with supplied platform evidence")
    compare.add_argument("--expected", required=True)
    compare.add_argument("--observations", required=True)
    compare.add_argument("--output")
    compare.add_argument("--strict", action="store_true", help="Return 2 if any row failed or needs manual review")
    audit = commands.add_parser("audit", help="Offline XLSX audit against supplied evidence; never opens Chrome")
    audit.add_argument("--source", required=True)
    audit.add_argument("--observations", required=True)
    audit.add_argument("--output", required=True, help="Distinct audited XLSX destination")
    audit.add_argument("--report", help="Optional JSON report destination")
    audit.add_argument("--strict", action="store_true")
    for command in (convert, audit):
        command.add_argument("--map", action="append", default=[])
        command.add_argument("--amount-rules")
    for name, help_text in (("run", "Live resumable Xiaohongshu XLSX audit"), ("login", "Open an owner-assisted Xiaohongshu verification window"),
                            ("capture-xhs", "Capture Xiaohongshu evidence"), ("capture-douyin", "Capture Douyin evidence"),
                            ("maintain", "Dry-run-first evidence retention; workbooks are preserved")):
        delegated = commands.add_parser(name, help=help_text, add_help=False)
        delegated.add_argument("arguments", nargs=argparse.REMAINDER)
    if argv is None:
        argv = sys.argv[1:]
    # Delegate before argparse consumes unknown adapter flags, including --help.
    adapters = {"run": "run_comment_review_job", "login": "xhs_visible_login", "capture-xhs": "xhs_batch_capture",
                "capture-douyin": "douyin_batch_capture", "maintain": "maintain_audit_storage"}
    if argv and argv[0] in adapters:
        script = script_path(adapters[argv[0]])
        env = child_environment()
        if any(argument in ("--help", "-h") for argument in argv[1:]):
            return subprocess.call([sys.executable, str(script), *argv[1:]], env=env)
        if argv[0] == "maintain":
            return subprocess.call([sys.executable, str(script), *argv[1:]], env=env)
        # Child progress and prompts go to stderr. stdin is preserved for owner
        # confirmation; stdout remains a single machine-readable completion.
        code = subprocess.call([sys.executable, str(script), *argv[1:]], env=env, stdout=sys.stderr)
        manifest = None
        if argv[0] == "run":
            directory = next((value.split("=", 1)[1] for value in argv if value.startswith("--work-dir=")), None)
            if "--work-dir" in argv and argv.index("--work-dir") + 1 < len(argv):
                directory = argv[argv.index("--work-dir") + 1]
            if directory:
                path = Path(directory) / "job-result.json"
                if path.is_file():
                    manifest = read_json(path)
        if manifest is not None and (code == 0 or manifest.get("status") in ("blocked", "error")):
            emit(manifest)
        else:
            emit({"status": "complete" if code == 0 else "blocked" if code in (3, 4) else "error", "exit_code": code})
        return code
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            emit(doctor())
            return 0
        if args.command == "convert":
            emit(convert_table(args.source, mapping=table_to_expected.parse_maps(args.map),
                               amount_rules=table_to_expected.parse_amount_rules(args.amount_rules), sheet=args.sheet), args.output)
            return 0
        if args.command == "review":
            result = review(read_json(args.expected), read_json(args.observations))
            emit(result, args.output)
        else:
            result = audit_workbook(args.source, read_json(args.observations), args.output,
                                    mapping=table_to_expected.parse_maps(args.map),
                                    amount_rules=table_to_expected.parse_amount_rules(args.amount_rules))
            emit(result, args.report)
        counts = result["summary"]["triage_counts"]
        return 2 if args.strict and (counts["失败"] or counts["待人审"]) else 0
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile, ET.ParseError, ArithmeticError) as exc:
        emit({"status": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
