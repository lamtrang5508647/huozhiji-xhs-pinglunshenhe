"""Cross-platform regressions; fixtures never contact a social platform."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_environment import child_environment, process_group_options, terminate_process_tree, powershell_command
from profile_lock import ProfileLock, default_profile, prepare_profile
import session_environment as network
from browser_environment import find_chrome


class RuntimeTests(unittest.TestCase):
    def test_windows_chrome_detection_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch("browser_environment.sys.platform", "win32"), patch("browser_environment.shutil.which", return_value=None), patch.dict(os.environ, {"PROGRAMFILES": directory, "COMMENT_AUDIT_CHROME": ""}):
                self.assertEqual(Path(find_chrome()), executable)
            with patch.dict(os.environ, {"COMMENT_AUDIT_CHROME": str(executable)}):
                self.assertEqual(find_chrome(), str(executable.resolve()))
            with patch.dict(os.environ, {"COMMENT_AUDIT_CHROME": str(executable.parent / "missing.exe")}):
                with self.assertRaises(FileNotFoundError):
                    find_chrome()

    def test_utf8_children_preserve_chinese_and_emoji(self):
        proc = subprocess.run([sys.executable, "-c", "print('图字成功😀')"],
                              env=child_environment(), capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(proc.stdout.strip(), "图字成功😀")

    def test_windows_defaults_and_process_flags(self):
        with patch("profile_lock.sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}):
            self.assertEqual(str(default_profile("xhs")).replace("\\", "/"),
                             "C:/Users/test/AppData/Local/HuozhijiAudit/xhs-profile")
        with patch("runtime_environment.sys.platform", "win32"), patch("runtime_environment.subprocess.CREATE_NEW_PROCESS_GROUP", 512, create=True):
            self.assertEqual(process_group_options(), {"creationflags": 512})

    @unittest.skipUnless(sys.platform == "win32", "real Windows DACL check")
    def test_windows_profile_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "私有 会话"
            profile.mkdir()
            env = child_environment({"HUOZHIJI_PROFILE_DIRECTORY": str(profile)})
            # No owner/group identifiers enter logs; they are retained in memory.
            descriptor_script = "$acl=Get-Acl -LiteralPath $env:HUOZHIJI_PROFILE_DIRECTORY; Write-Output ($acl.Owner + '|' + $acl.Group)"
            before = subprocess.run(powershell_command(descriptor_script), env=env, capture_output=True,
                                    encoding="utf-8", check=True, timeout=15).stdout
            with ProfileLock(profile):
                script = """
$acl = Get-Acl -LiteralPath $env:HUOZHIJI_PROFILE_DIRECTORY
$allowed = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
if (-not $acl.AreAccessRulesProtected) { throw 'unprotected' }
if ($acl.Access.Count -ne 3) { throw 'unexpected rule count' }
foreach ($rule in $acl.Access) {
  $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  if ($sid -notin $allowed -or $rule.AccessControlType -ne 'Allow') { throw 'unexpected access' }
}
Write-Output 'private'
"""
                result = subprocess.run(powershell_command(script), env=child_environment({"HUOZHIJI_PROFILE_DIRECTORY": str(profile)}),
                                        capture_output=True, encoding="utf-8", check=True, timeout=15)
                self.assertEqual(result.stdout.strip(), "private")
            after = subprocess.run(powershell_command(descriptor_script), env=env, capture_output=True,
                                   encoding="utf-8", check=True, timeout=15).stdout
            self.assertTrue(before == after, "profile initialization changed ownership/group")

    def test_profile_permissions_failure_is_private_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.CompletedProcess([], 1, "", "SetAcl_AclObject\nprivate data must not appear here\n")
            with patch("profile_lock.sys.platform", "win32"), patch("profile_lock.subprocess.run", return_value=result):
                with self.assertRaises(RuntimeError) as caught:
                    prepare_profile(Path(directory) / "profile")
            self.assertEqual(str(caught.exception), "browser_profile_permissions_failed:SetAcl_AclObject")

    def test_profile_lock_across_processes_and_exception_release(self):
        with tempfile.TemporaryDirectory(prefix="audit-") as directory:
            profile = Path(directory) / "中文 空格 会话"
            script = "from profile_lock import ProfileLock; import sys\ntry:\n with ProfileLock(sys.argv[1]): pass\nexcept RuntimeError:\n sys.exit(7)"
            args = [sys.executable, "-c", script, str(profile)]
            env = child_environment({"PYTHONPATH": str(Path(__file__).parent)})
            with self.assertRaises(ValueError):
                with ProfileLock(profile):
                    proc = subprocess.run(args, env=env, capture_output=True, encoding="utf-8", timeout=30)
                    self.assertEqual(proc.returncode, 7, proc.stderr)
                    raise ValueError("synthetic failure")
            subprocess.run(args, env=env, capture_output=True, check=True, timeout=30)

    def test_owned_process_tree_timeout_does_not_kill_other_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived"
            child_code = "import time; from pathlib import Path; time.sleep(5); Path(" + repr(str(marker)) + ").touch()"
            parent_code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', " + repr(child_code) + "]); print('ready', flush=True); time.sleep(60)"
            other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **process_group_options())
            proc = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=child_environment(), **process_group_options())
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                terminate_process_tree(proc)
                self.assertIsNotNone(proc.poll())
                self.assertIsNone(other.poll())
                # An observation interval, not a platform retry.
                subprocess.run([sys.executable, "-c", "import time; time.sleep(5.2)"], check=True)
                self.assertFalse(marker.exists(), "a spawned child survived process-tree cleanup")
            finally:
                terminate_process_tree(proc)
                terminate_process_tree(other)

    def test_windows_network_hash_is_stable_private_and_changes(self):
        state = {"routes": [{"InterfaceIndex": 1, "NextHop": "192.0.2.1", "RouteMetric": 25}],
                 "proxy": {"ProxyEnable": 0}, "connections": "synthetic"}
        with patch("session_environment.sys.platform", "win32"), patch("session_environment._command_output", return_value=json.dumps(state)):
            before = network.network_fingerprint()
            self.assertEqual(before, network.network_fingerprint())
            self.assertEqual(len(before), 64)
            self.assertNotIn("192.0.2.1", before)
        state["proxy"]["ProxyEnable"] = 1
        with patch("session_environment.sys.platform", "win32"), patch("session_environment._command_output", return_value=json.dumps(state)):
            self.assertNotEqual(before, network.network_fingerprint())
        with patch("session_environment.sys.platform", "win32"), patch("session_environment._command_output", return_value=""):
            self.assertEqual(network.network_fingerprint(), "unavailable")


if __name__ == "__main__":
    unittest.main()
