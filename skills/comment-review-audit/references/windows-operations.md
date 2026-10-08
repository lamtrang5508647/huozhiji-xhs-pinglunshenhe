# Windows environment

Use native Python 3.11+ and ordinary installed Google Chrome, not WSL or incognito. For the full repository, follow `docs/windows.md` and its PowerShell installers. Neither Feishu nor OpenClaw is required.

For a standalone installed skill directory, run scripts with `py -3 -X utf8 scripts/<name>.py ...`. Install live dependencies in that same interpreter/environment: `py -3 -m pip install "playwright>=1.45,<2" "xhs-cli==0.1.4"`. Do not assume a Mac uv tool path or mix another interpreter's compiled dependencies into this environment. Offline scripts need only the standard library.

Default profiles are `%LOCALAPPDATA%\HuozhijiAudit\xhs-profile` and `douyin-profile`. Mac/Linux retain `~/.comment-review-audit/<platform>-profile`. Use the same dedicated directory throughout login and capture. `COMMENT_AUDIT_CHROME` overrides executable discovery.

Windows uses `msvcrt` byte locks and a protected directory ACL for the current user, SYSTEM and Administrators. POSIX uses `fcntl` and mode `0700`. Fail closed if permissions cannot be established; Windows `chmod(0700)` is not a privacy guarantee. Never point `--profile` to a general-purpose/shared directory or change permissions outside the dedicated profile. Existing custom profile children with independent ACLs need an administrator check.

Preserve owner/group when updating a profile's ACL. Windows PowerShell helpers isolate their own module lookup to inbox modules: a PowerShell 7 parent can otherwise pass incompatible module paths to a 5.1 child. Do not change the owner's global `PSModulePath` or silently disable permission/network checks to hide a module-loading failure.

Windows route/proxy checks use read-only PowerShell and save only a hash. An unavailable monitor is not proof of stable networking. Stop and checkpoint on a detected change; do not rotate accounts, browsers or networks to clear warnings.

Keep owner-assisted verification in an interactive terminal. Preserve the existing page until explicit owner confirmation and a healthy-page check. Do not infer login from a dependency check, saved cookie or hidden window. After restoration, verify one fresh target before batching.

CLI children use UTF-8. Programs reading JSON must explicitly decode UTF-8. Quote paths containing spaces and pass subprocess arguments as arrays. If Excel/WPS holds an output workbook open, ask the owner to close that file; never kill the office application.

Regression/Chrome smoke tests do not authenticate real accounts. Distinguish tested runtime support from a Windows-account audit, which still requires a healthy canary.
