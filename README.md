# huozhiji-xhs-pinglunshenhe

独立的评论审核工具，也可作为 Codex skill 使用。核对小红书/抖音评论中的文字与图片，按源表金额回填 Excel，并生成 `审核汇总`。

核心只需要 Python 3.11+，不需要飞书机器人、OpenClaw、Codex、Node.js、Excel 或第三方 Python 库。飞书只是可选的任务接收/交付渠道。

0.4.8 支持 Windows、macOS、Linux 共用一套 SDK/CLI，并可打包为 WorkBuddy 本地技能。Windows 安装和启动入口见 [Windows 使用说明](docs/windows.md)。

## WorkBuddy 使用

在线审核按笔记保存评论区原图，交付一份审核版 Excel。财务打开「评论原图」工作表，左侧看截图，右侧逐条对照任务原文、审核结果、金额、原表行号和可点击的原笔记链接。二审只复核原先成功的行。

下载 [WorkBuddy 技能包](https://github.com/lamtrang5508647/huozhiji-xhs-pinglunshenhe/releases/latest/download/comment-review-audit.zip)，在 WorkBuddy 的「专家·技能·连接器 → 技能 → 添加技能 → 上传技能」导入。本地包内含审核脚本，离线审核使用 Python 3.11+。需要从源码生成技能包时，在仓库根目录运行 `python tools/package_workbuddy.py --output dist/comment-review-audit.zip`。导入入口见 [WorkBuddy 官方说明](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)。

可直接告诉 WorkBuddy：“核对每篇笔记下的评论，目标正文出现一次就算成功，父评论和子回复都算，返回审核版 Excel。” 小红书在线入口 `run` 会按笔记保存断点。使用已有证据时，可用 `audit --brief --strict` 获取统计和文件位置。

## 安装与快速测试

```bash
git clone https://github.com/lamtrang5508647/huozhiji-xhs-pinglunshenhe.git
cd huozhiji-xhs-pinglunshenhe
python3 -m pip install .
huozhiji-audit doctor
huozhiji-audit review --expected examples/expected.json --observations examples/observations.json
```

也可不安装，直接在仓库根目录运行 `python3 -m huozhiji_audit ...`。示例全部是模拟数据，不会访问真实笔记。

## 其他程序直接调用

Python 接口不会主动开浏览器或连接机器人：

```python
import json
from huozhiji_audit import convert_table, review, audit_workbook

with open("observations.json", encoding="utf-8") as f:
    evidence = json.load(f)

expected = convert_table("评论.xlsx")
report = review(expected, evidence)
print(report["summary"]["triage_counts"])
report = audit_workbook("评论.xlsx", evidence, "评论-审核版.xlsx")
```

其他语言可调用命令行：

```bash
huozhiji-audit convert --source 评论.xlsx --output expected.json
huozhiji-audit audit --source 评论.xlsx --observations observations.json \
  --output 评论-审核版.xlsx --report review-report.json
```

`convert`、`review`、`audit` 以 JSON 输出结果。`review` 和 `audit` 的 `--strict` 控制退出码，有失败、待审或未结算金额时返回 2 并保留报告。详见[程序接入说明](docs/integration.md)。

`audit` 是离线核对已有证据，不会假装访问过平台。没有证据、分页不完整、登录失效或图片状态未知时保留 `待人审`。

## 在线采集，可选

在线模式额外需要普通 Chrome 和浏览器依赖；持久 profile 锁支持 Windows/macOS/Linux。Windows 使用原生文件锁和目录 ACL，默认会话存放在 Local AppData。

```bash
python3 -m pip install '.[xhs]'
huozhiji-audit doctor
huozhiji-audit run --source 评论.xlsx --work-dir /path/outputs/job-id \
  --output-name 评论-最终审核版.xlsx --wait-for-confirmation
```

如 Chrome 不在默认位置，可设置 `COMMENT_AUDIT_CHROME=/path/to/chrome`。人工协助时在交互终端/PTY运行，遇到验证后保留实际页面，完成扫码/验证后按 Enter；未恢复正常不会自动续跑。

`run` 当前编排小红书 XLSX 任务；抖音提供 `capture-douyin` 适配器，可采集后调用统一 `review`/`audit`，不是同一个一键编排。采集始终只读，先试审一条，再低速串行批量、逐条保存断点。不轮换账号、代理或网络绕过风控，也不能承诺永不触发风控。

默认 Excel 引擎是独立的 Python OOXML 写入器：保留源表和媒体，检查行身份、金额、汇总公式缓存及 ZIP 完整性，Excel打开时重新计算公式。它不是通用公式计算器，也不生成图片预览。源表受保护、越界合并或无法安全重定位的汇总图片会明确报错，不会静默丢掉数据。若宿主具备 `@oai/artifact-tool`，可选 `--workbook-engine artifact` 生成图片预览及执行宿主公式扫描。

## 审核与金额

Excel 的任务行已有首审结论时，WorkBuddy 自动只复核上次成功的行，包括图字成功和已确认的单项成功。上次失败、待审或空白的行保留原状。原表没有任何已识别的审核结论时，全表首审。写回时优先使用表中已有的结果和核定金额列，已有报价或备注则保留，必要时新增列。在线再次确认会重新查看相关原帖，报告注明本次复核和沿用的行数。

小红书二审在当前页面已找到同篇全部目标、配图和独立结算证据时，可跳过其余折叠回复。尚有缺项时继续展开。`run` 的采集默认在连续 300 秒没有进展后返回 `capture_idle_timeout`，任务可从原目录续跑。实际等待账号持有人扫码确认和计划休息时暂停计时。

- 普通结果：成功、失败、待人审。
- 图文结果：图字成功、文字成功、图片成功、失败、待人审。
- 用源表报价结算。已确认可见但缺少报价，或多行共用一条结算证据时，相关金额留空，报告说明原因。
- 评论缺失只有在完整、正常的评论列表中才能判失败。验证码、登录失效、分页不完整或图片不明确会保留为待人审。
- 每条任务只在指定笔记下找正文。顶层评论或任一楼中楼出现一次即满足文字要求，表内“回复第 N 条”不改变这个判定。
- 每个文件独立生成 `审核汇总`，按团长和产品统计，并保留结算信息。

协议详见 [data-contract.md](skills/comment-review-audit/references/data-contract.md)。`job-result.json` 区分 `in_progress`/`blocked`/`error`/`complete`，包含源文件与成品 SHA-256。在线登录是否正常不能只靠 `doctor` 的依赖检查判断。

## 测试、skill 与可选飞书接入

以下检查只使用离线模拟数据，不访问平台：

```bash
python3 tests/run_regressions.py
node --check skills/comment-review-audit/scripts/build_audited_workbook.mjs
```

迭代性能时运行 `python tools/benchmark_audit.py`。它用 12 个工作表、1,200 行任务和 7,200 条模拟评论跑完整离线审核，默认报告 5 次耗时的中位数，以及逐行结果和 Excel 各部件的内容签名。加入 `--checkout <旧版仓库路径>` 可用相同样本复测旧版本。

CI在 Windows、macOS、Linux 的 Python 3.11/3.14 上测试纯 Python 接口、旧适配器、wheel 安装后的入口和真实 Chrome 空白页。不会测试真实账号登录或抓取真实评论。将 `skills/comment-review-audit/` 安装到支持 skill 的程序即可使用。只有使用已有 OpenClaw/飞书集成时才执行 `scripts/sync_openclaw_skill.sh`；不需要它的使用者不用配置机器人密钥。

## 存储与访问

成品 `.xlsx/.xls` 长期保留。`huozhiji-audit maintain --root /精确输出目录 --retention-days 14 --archive-retention-days 14` 默认只做 dry-run；确认后加 `--apply`。旧证据先校验归档再移除，归档再保留14天。工具不会擅自设置定时任务。

仓库仅放源码和模拟测试。真实评论表、证据、profile、cookie、密钥和联系人记录不要提交。公开仓库允许所有人查看/下载；不向外部添加写权限，上游仓库由所有者维护。公开后其他人可以 fork 或复制自己的版本，但不能因此直接修改此仓库。
