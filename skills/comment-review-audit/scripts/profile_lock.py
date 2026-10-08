#!/usr/bin/env python3
"""Single-process lock for a persistent browser profile."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Optional

from runtime_environment import child_environment, powershell_command

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def default_profile(platform: str) -> Path:
    if platform not in ("xhs", "douyin"):
        raise ValueError("unsupported_profile_platform")
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "HuozhijiAudit" / f"{platform}-profile"
    return Path.home() / ".comment-review-audit" / f"{platform}-profile"


def prepare_profile(profile: Path) -> None:
    if profile == Path(profile.anchor) or profile == Path.home().resolve():
        raise ValueError("browser_profile_requires_dedicated_directory")
    profile.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(profile, 0o700)
        return
    # chmod(0700) does NOT provide Windows privacy. Restrict this dedicated
    # directory's DACL; never change a parent directory or delete Chrome locks.
    script = """
try {
# Keep a valid owner/group descriptor; an empty DirectorySecurity object is
# not a complete descriptor for Windows PowerShell's Set-Acl provider.
$acl = Get-Acl -LiteralPath $env:HUOZHIJI_PROFILE_DIRECTORY
$acl.SetAccessRuleProtection($true, $false)
foreach ($existing in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($existing) }
$ids = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User,
         [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
         [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
foreach ($id in $ids) {
  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($id,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)
  $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $env:HUOZHIJI_PROFILE_DIRECTORY -AclObject $acl
} catch {
  # Fixed error identifiers only: no user paths, account SIDs or raw messages.
  [Console]::Error.WriteLine($_.Exception.GetType().Name)
  [Console]::Error.WriteLine($_.FullyQualifiedErrorId.Split(',')[0])
  exit 1
}
"""
    try:
        result = subprocess.run(powershell_command(script),
                                env=child_environment({"HUOZHIJI_PROFILE_DIRECTORY": str(profile)}),
                                capture_output=True, encoding="utf-8", timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("browser_profile_permissions_failed") from None
    if result.returncode != 0:
        import re
        codes = [line for line in result.stderr.splitlines() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,100}", line)]
        reason = ":".join(codes[:2]) or "powershell_failed"
        raise RuntimeError(f"browser_profile_permissions_failed:{reason}")


class ProfileLock:
    def __init__(self, profile: str | Path):
        self.profile = Path(profile).expanduser().resolve()
        self._handle: Optional[BinaryIO] = None

    def __enter__(self) -> "ProfileLock":
        if self._handle is not None:
            raise RuntimeError(f"browser_profile_in_use:{self.profile}")
        prepare_profile(self.profile)
        handle = (self.profile / ".comment-review-audit.lock").open("a+b")
        try:
            if sys.platform == "win32":
                handle.seek(0)
                # msvcrt supports locking beyond EOF; no writes race with another holder.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError(f"browser_profile_in_use:{self.profile}") from exc
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
