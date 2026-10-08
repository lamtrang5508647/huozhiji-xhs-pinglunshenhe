# Windows 使用说明（0.3.0）

使用原生 Windows + Python 3.11+，不需要 WSL、Mac、飞书机器人或 Excel。在线采集另需正常安装的 Google Chrome，扫码和安全验证仍由账号本人完成。

## 安装

安装 Python 和 Git，下载本仓库后，在仓库根目录打开 PowerShell。离线版安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\install-windows.ps1"
```

需要小红书在线采集时，加 `-Live`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\install-windows.ps1" -Live
```

安装器使用项目内 `.venv`，不改系统 Python；启动器不要求激活环境。`ExecutionPolicy Bypass` 仅作用于这次进程，不修改全局策略。受企业策略限制时遵循管理员要求，不能绕过组织限制。指定解释器可加 `-Python "C:\路径\python.exe"`。

## 启动和测试

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\huozhiji-audit.ps1" doctor
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\huozhiji-audit.ps1" review --expected examples/expected.json --observations examples/observations.json
```

示例只比较模拟证据，不访问平台。`doctor` 检查依赖和平台后端，不证明已经登录。

离线审核已有证据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\huozhiji-audit.ps1" audit --source "D:\审核\评论.xlsx" --observations "D:\审核\observations.json" --output "D:\审核\评论-审核版.xlsx"
```

在线小红书任务（先单条试审，再按默认低速串行批量）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\huozhiji-audit.ps1" run --source "D:\审核\评论.xlsx" --work-dir "D:\审核\outputs\job-001" --output-name "评论-最终审核版.xlsx" --wait-for-confirmation
```

保持交互终端和验证窗口打开，完成扫码/验证后按 Enter。未解除验证不会继续打开目标。中断后使用原命令、相同源表和工作目录恢复；新任务换新的工作目录。抖音使用 `capture-douyin` 采集，再调用统一 `audit`，目前不是 `run` 的一键编排。

也可直接用 `.\.venv\Scripts\python.exe -m huozhiji_audit ...`；其他程序调用时传参数数组、读取 UTF-8 JSON，避免拼接 shell 字符串。

## 会话与安全

- Chrome 自动查找 Program Files、Program Files (x86)、Local AppData；非默认位置设置 `$env:COMMENT_AUDIT_CHROME = 'C:\路径\chrome.exe'`。
- 默认会话保存在 `%LOCALAPPDATA%\HuozhijiAudit\xhs-profile` 和 `douyin-profile`，不放仓库。每位使用者用自己的账号，不能共享 cookie。
- Windows 原生 `msvcrt` 非阻塞锁禁止两个任务同时使用同一 profile；冲突时不要删除 Chrome 锁文件。
- 专用会话目录使用 ACL 限制为当前用户、SYSTEM 和管理员。不要把 `--profile` 指向日常 Chrome 目录、共享目录、盘根或用户主目录；已有自定义目录内部的独立共享权限应由管理员检查，不承诺保护目录就能撤销所有历史共享。
- 网络检查读取路由、系统代理设置及代理环境变量，只保存哈希。检测到变化即保存断点并停止；检查不可用不等于环境正常。没有代理切换、账号轮换或绕过风控功能。
- 超时清理按本任务的 PID 结束子进程树，不使用关闭所有 Chrome 的命令。
- Excel/WPS 占用输出文件时，关闭该文件再重试，不强行关闭办公软件。中文、空格和表情路径纳入回归测试。

## 验证边界

自动测试在 Windows、macOS、Linux 的 Python 3.11/3.14 执行：离线审核、金额/汇总/图片保留、干净 wheel 安装、会话锁、Windows ACL、超时清理、真实 Chrome `about:blank` 启动和持久 profile 重开。不使用真实账号、不访问真实评论，不能代替 Windows 用户登录后的单条试审，也不能保证平台不触发风控。

14天存储维护仍需先 dry-run、确认精确输出目录后再 `--apply`；成品 Excel 不清理。可选 OpenClaw 同步脚本是 POSIX 运维工具，不是 Windows 核心依赖。
