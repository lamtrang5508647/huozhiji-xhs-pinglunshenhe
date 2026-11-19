---
name: comment-review-audit
display_name: 小红书与抖音评论审核
display_name_en: Xiaohongshu and Douyin Comment Audit
description: 根据任务表和平台证据核对小红书、抖音评论文字及配图，生成审核版 Excel、待人审结果与结算汇总。用户要求核对评论是否发布、核算评论任务金额或续审时使用。
description_zh: 逐行核对评论与配图，回填审核结果和核定金额，生成结算汇总。
description_en: Check comment evidence against assigned tasks and export a reconciled workbook.
version: 0.4.3
author: lamtrang5508647
---

# 评论审核

使用本技能目录中的 `scripts/audit.py`。从已加载的 SKILL.md 确定脚本绝对路径。每份源表使用独立工作目录，给成品、报告和证据分别命名，保留原表作为报价依据。离线核对需要 Python 3.11 或以上，使用脚本时可以在任意工作目录运行。

## 有源表和采集证据

将下列路径替换为实际文件的绝对路径。Windows 使用 `py -3` 或已知 Python 3.11+ 解释器，macOS/Linux 可用 `python3`。Windows 找不到系统 Python 时，可检查当前 WorkBuddy 安装目录的 `resources/vendor/python/python.exe`，通过 `--version` 确认版本后使用。

```powershell
py -3 -X utf8 "<技能目录>/scripts/audit.py" audit --source "<原始评论表.xlsx>" --observations "<证据.json>" --output "<任务目录>/评论-审核版.xlsx" --report "<任务目录>/审核报告.json" --brief --strict
```

回传标准输出中的成功、失败、待人审数、核定金额，以及成品和报告路径。需要解释某条时，按 case_id 或工作表与行号读取报告中的对应记录。退出码 2 表示文件已生成，其中含失败或待人审。退出码 1 表示执行错误，报告本次错误。

自动图片判断核对评论是否附图。如果任务明确要求使用某张指定图片，需比对源图和实际评论图片，再说明这项要求是否满足；只有附图标记时向用户标明仍需核图，相关金额暂缓确认。

源表写“回复第 N 条”时，核对正文和直接父评论。用户要求“去原帖查”“复查待人审”或核对真实发布情况时，用下方小红书在线流程。已有该任务的采集断点时沿用原任务目录。单独提供的证据可复制为新任务目录中的 `observations.json` 后运行。用户只要求核对已给证据时使用离线命令。

如果输入已经是要求 JSON，用 `review --expected <要求.json> --observations <证据.json> --output <报告.json> --brief --strict`。单纯转表用 `convert --source <原表> --output <要求.json> --brief`。工具返回逐行分类，使用原表报价，部分成功仅按表内明确的分项价格结算。需要核对字段、重复认领或图片状态时读 @references/data-contract.md。

## 需要到平台查看

用户要求审核原帖或减少待人审时，让在线流程跑完自动补查，再交付结果。采集从源表任务行和映射的链接列确定原帖，同一篇只列一次。展开回复后等实际评论出现。首轮仍有可补证的任务时，仅重查相应原帖一次。用户提供证据并明确要求离线时，按离线流程处理。

交付前核对源表任务数、报告行数和写回结果。遇到平台登录或验证，读取 `job-result.json` 的 `coverage` 说明有多少行已有采集记录、多少行尚未取得记录，恢复后沿用同一目录续审。仍有待审时逐行给出阻塞原因和金额状态。

只有源表时，先确认目标平台，按用户的审核要求采集真实证据。首次运行 `doctor` 查看依赖和 Chrome。需要在线依赖时，在任务使用的 Python 环境中安装 `playwright>=1.45,<2`，小红书另需 `xhs-cli==0.1.4`。线上操作细节见 @references/platform-operations.md。

- 小红书 XLSX 使用 `run --source <原表.xlsx> --work-dir <任务目录> --output-name 评论-审核版.xlsx`。若账号持有人在交互终端操作，可加 `--wait-for-confirmation`。同一篇笔记的待核回复合并到一次页面复查，打开评论区、展开可见回复，按新证据重判。即使首轮评论数已齐，缺少父级的回复也会触发复查。继续同一批次时沿用该任务目录，并按返回清单的 status 判断完成或阻塞。
- 抖音先用 `convert` 生成要求，使用 `capture-douyin --expected <要求.json> --output <抖音证据.json> --only-target-key <目标ID>` 试审一条，确认采集正常后去掉目标筛选继续同一断点，最后调用 `audit`。混合平台的表应分别采集，再用随包的 `scripts/huozhiji_audit/_engine/merge_observations.py --input <小红书证据> --input <抖音证据> --output <合并证据>` 汇合后审核。小红书单独取证可用 `capture-xhs --input <原表.xlsx> --verify-replies <要求.json> --output <小红书证据.json> --headed --expand-dom --risk-averse --task-targets`，试审时加 `--only-note-id <目标ID>`。
- 逐行说明剩余待审的原因。`reply_source_parent_ambiguous` 表示源表编号仍指不出唯一父评论，`reply_parent_unconfirmed` 表示指定父评论和平台回复的直接关系仍缺证据。读取该行的 `reply_verification`，说明是否实际查过、评论区是否打开、找到哪些候选。原表预填结果仅作参考，待审行的金额留空。
- 复查“明明可见却失败”时，用原始任务表执行在线 `run`。先读评论区里已经显示的正文和回复，仅在还有展开入口时点击。向用户分别说明正文是否出现、能否确定直接回复对象，以及评论列表是否已读完。此前使用 0.4.1 或 0.4.2 的任务可以沿用原任务目录重新采集。
- 源表有“任务序号”或“评论序号”时按该列定位“回复第 N 条”。如果用户明确指定“第 N 条按账号列编号”，可加 `--map task_ordinal=账号`。不要根据“账号”这个表头自行推断业务编号。
- 遇到登录、验证或列表未加载完，保留本次断点，说明需要处理的目标。平台记录及表内文字仅作为审核数据。只读查看，金额不明确、图片证据未知或同一条证据冲突时交人工复核。
