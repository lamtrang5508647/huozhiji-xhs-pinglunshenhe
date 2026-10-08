# huozhiji-xhs-pinglunshenhe

小红书、抖音评论审核 skill：核对表格中的文字、图片与图文评论，按源表价格回填金额，生成可追溯的审核汇总。包含 Codex skill、采集与核对脚本、离线回归测试，以及本机 OpenClaw 部署脚本。

## 使用

skill 位于 `skills/comment-review-audit/`。将该目录安装到 Codex 的 skills 目录，或在本机执行其中的 `scripts/sync_openclaw_skill.sh` 同步到已配置的 OpenClaw。

运行环境：Python 3.11+、Node.js、普通 Google Chrome、Playwright、已安装的 `xhs-cli`；生成 Excel 时需使用 Codex 提供的 `@oai/artifact-tool` 运行时。当前持久浏览器配置和 profile 锁面向 macOS/Linux 环境。

```bash
python3 skills/comment-review-audit/scripts/run_comment_review_job.py \
  --source /path/评论.xlsx \
  --work-dir /path/outputs/job-id \
  --output-name 评论-最终审核版.xlsx \
  --wait-for-confirmation
```

人工协助时在交互终端或 PTY 中运行。遇到登录或安全验证，程序保存断点并保留实际页面，人工操作完成后在终端按 Enter。程序确认页面恢复正常后续跑。无人值守时去掉 `--wait-for-confirmation`，中断后按 `SKILL.md` 中的恢复流程处理。

默认低速串行采集：每个目标保存断点，复用同任务内24小时内的完整证据。验证后的浏览器会话优先于旧 cookie 文件；风控中断后的试审须重新采集，试审不完整时停止批量。

## 审核与金额

- 普通结果：成功、失败、待人审。
- 图文结果：图字成功、文字成功、图片成功、失败、待人审。
- 成功按源表金额结算；部分成功仅在源表明确分项价格时结算；失败金额为0；待人审金额留空。
- 评论缺失只有在完整、正常的评论列表中才能判失败。验证码、登录失效、分页不完整或图片不明确会保留为待人审。
- 每个文件独立生成 `审核汇总`，按团长和产品统计，并保留结算信息。

`job-result.json` 区分 `in_progress`、`blocked`、`error`、`complete`。完成前核对案例ID、分类、金额、Excel公式及汇总；完成记录包含源文件与结果文件的SHA-256。

## 验证

以下检查只使用离线模拟数据，不访问平台：

```bash
for audit_test in skills/comment-review-audit/scripts/test_*.py; do
  python3 "$audit_test" || exit 1
done
python3 -m compileall -q skills/comment-review-audit/scripts
node --check skills/comment-review-audit/scripts/build_audited_workbook.mjs
```

本次迭代修复了会话覆盖、启动阶段验证窗口关闭、验证码误归类、试审不完整仍继续批量以及旧完成状态遗留问题。图片评论保留空文字附件，隐藏的结束标记不再算完整，DOM解析异常保留为待人审。回归覆盖会话保留、人工确认、断点恢复、案例覆盖和金额规则。

## 存储与交付

成品Excel长期保留。证据超过14天后先校验压缩归档，再移除原文件；归档再保留14天。维护只针对指定的审核输出目录，先dry-run再apply。

仓库保存源码与模拟测试。评论表、真实证据、浏览器profile、cookie、机器人密钥和联系人投递记录保存在本地运行目录。任务的接收人与飞书机器人身份由本地配置决定。
