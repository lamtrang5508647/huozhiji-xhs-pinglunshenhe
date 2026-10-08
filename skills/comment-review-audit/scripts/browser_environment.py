"""Find ordinary Chrome without depending on one workstation's absolute path."""
import os
import shutil
import sys
from pathlib import Path


def find_chrome(fallback=None):
    explicit = os.environ.get("COMMENT_AUDIT_CHROME")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError("COMMENT_AUDIT_CHROME does not point to a file")
        return str(path.resolve())
    candidates = [fallback] if fallback else []
    if sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    candidates.extend(filter(None, (shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "chrome.exe"))))
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(variable):
            candidates.append(str(Path(os.environ[variable]) / "Google/Chrome/Application/chrome.exe"))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Ordinary Chrome was not found; set COMMENT_AUDIT_CHROME")
