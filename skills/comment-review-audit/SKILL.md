---
name: comment-review-audit
description: Audit required text/image comments on Xiaohongshu or Douyin against XLSX requirements, reconcile table-defined amounts, and return audited workbooks with settlement summaries. Use for 评论审核/核对评论 requests, local audit tables, or programmatic comment-review workflows. Feishu is an optional integration, not a requirement.
---

# Comment Review Audit

Use this skill to reconcile comment requirements in CSV/XLSX/JSON tables against comments visible on Douyin or Xiaohongshu. Produce an auditable workbook, not only a prose answer.

## Host-independent use

The audit engines run independently of Codex, OpenClaw and Feishu. The repository provides the Python SDK `huozhiji_audit` and `huozhiji-audit` CLI. Core conversion, comparison and portable XLSX export require only Python 3.11+. If only this skill directory is installed, run the scripts below directly.

Windows, macOS and Linux share the same core. Before Windows deployment or live capture, read [references/windows-operations.md](references/windows-operations.md); use native Windows locking/ACLs, UTF-8 subprocesses and a dedicated Local AppData profile. Do not require WSL or a Mac host.

For offline evidence-based review, convert the table, compare supplied observations, then export. This does not constitute live platform verification. Read [references/programmatic-use.md](references/programmatic-use.md) for the independent script interface and its limitations.

Feishu intake/delivery and OpenClaw synchronization apply only when that integration is selected and configured. Read [references/feishu-integration.md](references/feishu-integration.md) only for that mode. Never require a bot or request bot credentials for local/API use.

## Operating boundary

- Keep platform work read-only. Never like, comment, publish, delete, follow, or change platform data.
- Never bypass login, CAPTCHA, security review, frequency limits, or risk controls. Stop, checkpoint, and report the blocker.
- Use one authorized account, one ordinary persistent Chrome profile, and one stable network route per platform session. Do not use incognito for batches, rotate accounts after a warning, or run concurrent captures.
- Never expose cookies, tokens, passwords, QR contents, proxy addresses, or full network identifiers. Store only non-secret fingerprints and local evidence paths.
- A missing comment is a failure only after a healthy and complete capture. Login gates, incomplete pagination, unknown image state, and inaccessible targets are `待人审`, never `失败`.

## Required workflow

1. Read [references/data-contract.md](references/data-contract.md) before preparing data or changing classifications. Read [references/platform-operations.md](references/platform-operations.md) before live capture.
2. Convert each source workbook to the expected-row contract. Use `--all-sheets` and preserve `source_file`, `source_sheet`, and `source_row`. The converter recognizes floating XLSX pictures by drawing anchor, not only cell values.

   ```bash
   python3 scripts/table_to_expected.py \
     --input /path/review.xlsx --output /path/expected.json --all-sheets \
     --map target_url=文章链接 --map comment_text=评论文字需求 \
     --map reply_text='需要回复评论内容【左边填写回第几条】' \
     --map image_requirement=评论图片需求 \
     --map expected_amount='金额【文字1.5，文字加图2元，单图1.5】'
   ```

   If cells are blank but the header defines rates, the converter derives amounts from the comment type. Supply `--amount-rules text=1.5,text+image=2,image=1.5` only when the workbook does not carry usable rules.
3. Establish the persistent desktop environment before opening batch targets:
   - Only for live capture. Offline comparison of supplied evidence does not require a browser/account.
   - Find ordinary Chrome automatically or set `COMMENT_AUDIT_CHROME`; do not assume a workstation-specific path exists.
   - Mac/Linux profiles: `~/.comment-review-audit/xhs-profile` and `douyin-profile`.
   - Windows profiles: `%LOCALAPPDATA%\HuozhijiAudit\xhs-profile` and `douyin-profile`.
   - The included profile lock prevents concurrent sessions: POSIX uses `fcntl`/`0700`, Windows uses `msvcrt`/protected directory ACLs. Stop if permissions or locking fail; never modify a general-purpose Chrome directory.
   - Use the visible login helpers and allow the user enough time to scan. Do not repeatedly open login windows.
   - Persisted Chrome cookies take precedence over older CLI cookie files. After owner verification, use the same profile; do not overwrite its refreshed session.
4. Run exactly one canary target first. Continue only when it returns `capture_status: ok`, the target identity is correct, comments are visible, and the completeness/image signals are credible.
   - Xiaohongshu: `xhs_batch_capture.py --headed --expand-dom --only-note-id ...`
   - Douyin: `douyin_batch_capture.py --only-target-key ...`
5. Capture the remaining unique targets serially. The standard runner uses its low-risk profile by default: a quiet session-settle wait, three slow warm-up targets, 12–18 second target intervals, at least 4 seconds of rest, 60–90 second breaks every eight targets, and one extra same-page DOM expansion for an incomplete first pass. It is designed to reduce risk controls and incomplete captures without changing accounts, networks, or login state. `--normal-pace` is an explicit legacy opt-out only when the owner approves the trade-off.
   - Xiaohongshu legacy target interval: 6–10 seconds; 25–45 second break every 12 processed targets. Use `--risk-averse` for the default low-risk profile described above.
   - Douyin target interval: 5–8 seconds; 20–35 second break every 10 processed targets.
   - A scheduled long break replaces the ordinary target pause, and the final target never sleeps after checkpointing. Use `--pace-mode fixed` only when slower legacy pacing is required.
   - Both adapters checkpoint after every target and automatically resume only complete, healthy evidence from the same parser revision captured within 24 hours. Incomplete captures and unknown image states are never skipped by `--skip-existing-ok` (now a compatibility flag). Use a separate work directory for a new audit, or `--refresh` for an explicit fresh recheck. If every selected target is reusable, return without opening Chrome; filtering completed targets before pacing also avoids idle waits at the tail.
6. Merge observations and run the deterministic comparator. For table-defined payout amounts, use `--amount-mode expected-only`. Do not enable `--allow-duplicates` by default; duplicate exact candidates require review.

   ```bash
   python3 scripts/review_comments.py \
     --expected /path/expected.json \
     --observations /path/observations.json \
     --amount-mode expected-only \
     --output /path/review-report.json
   ```

7. Inspect every `失败` and `待人审` row plus all `文字成功`/`图片成功` rows. The required user-facing classifications are:
   - General triage: `成功`, `失败`, `待人审`.
   - Text+image detail: `图字成功`, `文字成功`, `图片成功`, `失败`, `待人审`.
   - `文字成功` and `图片成功` use the source table's explicit text/image component rate and count in general triage `成功`. If the table does not define a component rate, they remain `待人审` with a blank amount. Full success receives the combined table amount, confirmed failure receives `0.00`, and unresolved `待人审` remains blank.
8. Build the audited workbook. Preserve the original sheets and embedded images, write the row category to column G and the approved amount to column H, and add/refresh `审核汇总` in the required settlement template: `团长 / 产品名 / 涉及工作表数 / 总条数 / 成功数 / 失败数 / 需人审数 / 成功率 / 报销金额 / 涉及工作表 / 团长联系方式 / 跟团长结算时间 / 结算凭证 / 二维码`. The summary `团长` must directly use the complete value from the product sheet's `团长` field (legacy `团` is accepted); never substitute the `负责人` abbreviation. Group rows by source `团长` + `产品`; preserve previously entered values in the last four manual columns when rebuilding.

   Standalone hosts use `python3 scripts/build_audited_workbook.py --source ... --expected ... --report ... --output ...`. It checks row backfill, Decimal totals, formula caches, source-media preservation and ZIP integrity. It does not render previews or recalculate arbitrary Excel formulas. Unsupported protected/merged/drawing layouts produce an error rather than silently deleting data.

   When the host supplies the spreadsheet runtime and visual verification is needed, use the optional artifact engine:

   ```bash
   node scripts/build_audited_workbook.mjs \
     --source /path/review.xlsx --expected /path/expected.json \
     --report /path/review-report.json --output /path/review-audited.xlsx \
     --preview-dir /path/evidence/previews
   ```

   For the artifact engine, follow the available spreadsheet skill: inspect/render the source before editing, render every output sheet, scan formula errors, and reconcile summary totals with the JSON report. The summary formulas in either engine remain traceable to per-row results; do not claim portable cached-value checks are full visual/formula verification.
   If a cropped preview appears blank, compare the exported cell values, styles and actual merge ranges, then render a view beginning at column A before diagnosing missing data or changing layout. A preview alone does not prove merged cells or failed writes.
   For standard Xiaohongshu XLSX jobs, prefer the one-command resumable runner below. It converts the workbook, reuses a prior healthy canary, captures each unique note once, checkpoints every target, compares rows, builds the workbook, and writes `job-result.json`. The default engine is portable; add `--workbook-engine artifact` only when the host runtime/preview capability is available. Re-running the same command resumes healthy completed targets instead of opening them again.

   ```bash
   python3 scripts/run_comment_review_job.py \
     --source /path/incoming.xlsx \
     --work-dir /path/job-output \
     --output-name incoming-最终审核版.xlsx
   ```

   For owner-assisted desktop work, add `--wait-for-confirmation` and launch in an interactive PTY. A startup or target challenge checkpoints immediately and stays on the actual page until the owner confirms; send Enter only after that confirmation. A still-blocked page keeps waiting. Exit code `4` means the helper validated the owner's completed verification, so the runner may resume; exit `3` is an unresolved stop and must not be automatically retried. For unattended runs, the default exits with a checkpoint; open `xhs_visible_login.py --url <original-source-link> --wait-for-confirmation` once in the same profile to restore it.
   A post-stop canary must be freshly captured. An incomplete canary blocks the batch. Reusing a complete canary is only permitted for a healthy uninterrupted same-job resume.
   `job-result.json` is rewritten to `in_progress` at run start, then `blocked`/`error` on interruption. `complete` requires exact row identities, valid categories, monetary invariants, a nonempty workbook and reconciled summary. It contains source/output SHA-256 fingerprints; a changed source requires a new job directory. These checks protect against accidental delivery of a previous run's result.

   A nonzero capture exit means an unresolved stop or execution error, not a completed audit. Keep the checkpoint, notify the owner, and rerun only after the blocker is resolved. Do not deliver a prior result when the current manifest is not `complete`. Local/API jobs return their file paths and JSON directly; do not send external messages or require a configured bot. For explicitly authorized Feishu delivery, use its optional integration reference and verify the receipt.
9. Keep final `.xlsx` files. Maintain intermediate JSON/screenshots/previews with `maintain_audit_storage.py`; run dry-run first, then `--apply` only on the managed audit output root. The default policy compresses raw evidence older than 14 days, prunes compressed evidence archives after another 14 days, and never touches final workbooks.

## Accuracy rules

- Text uses Unicode compatibility normalization, case folding, whitespace removal, and common punctuation removal. Exact matching is default; use row-level `text_match: contains` only when the business rule explicitly permits it.
- Comment type is `text`, `image`, or `text+image`. Contradictory metadata and unknown image presence require review.
- XLSX floating pictures count only when their drawing anchor lands in the mapped image-requirement column and row.
- Douyin image detection uses explicit attachment markers plus conservative size checks that exclude avatars and emoji. Detection errors set `image_detection_complete: false`; they never become “no image.”
- Douyin may stop scrolling when the visible terminal appears or the loaded comment count reaches the page-declared count. Finding an expected comment alone never stops capture because duplicates and missing rows still require complete evidence.
- `not_found` is valid only for a healthy complete list. `blocked` and `needs_review` never become `失败` merely to reduce the manual queue.
- Amount arithmetic uses decimal values. The workbook writes the table amount for `verified`, the explicit component rate for settled partial matches, `0.00` for confirmed `失败`, and blank for `待人审` or partial matches without a component rate.

## Continuous improvement

For every real false positive, false negative, or excess manual-review case:

1. Save a minimal redacted fixture that reproduces the exact signal.
2. Add a failing offline regression test before changing the rule.
3. Change only the comparator or platform adapter responsible for the error.
4. Run all offline tests and syntax compilation. For environment changes, test Windows/macOS/Linux, clean wheel installation, Unicode paths, profile locks/permissions and ordinary Chrome startup. A blank-page smoke test is not real account/comment verification. Verify workbook row/summary/media invariants; when using the artifact engine, also run its formula scan and visual verification.
5. Update the data contract when a status or reason code changes.
6. Publish the validated SDK/CLI/skill source together and test installation outside the checkout. Rebuild the selected distributable. OpenClaw synchronization is optional: run `scripts/sync_openclaw_skill.sh` only for a configured deployment that the user wants updated; its health checks are not a prerequisite for independent use.

If a selected integration's synchronization fails, keep the validated source as canonical and report that deployment separately. Never modify or print the Feishu App Secret during synchronization.

Never “improve” success rate by weakening completeness, image, duplicate, or risk-control safeguards.

## Included resources

- `scripts/table_to_expected.py`: converts CSV/TSV/JSON/XLSX and detects merged cells plus floating XLSX pictures.
- `scripts/xhs_visible_login.py`, `scripts/xhs_persistent.py`, `scripts/xhs_batch_capture.py`: persistent Xiaohongshu login and paced checkpoint capture.
- `scripts/douyin_visible_login.py`, `scripts/douyin_batch_capture.py`: persistent Douyin login, comment/image parsing, lazy loading, pacing, and checkpoint capture.
- `scripts/capture_pacing.py`: shared adaptive target pacing that counts page work, retains a minimum rest, and avoids tail sleeps.
- `scripts/capture_resume.py`: shared freshness, parser-revision and completeness checks; plans pending targets before opening a browser.
- `scripts/profile_lock.py`, `scripts/session_environment.py`: single-session profile protection and non-secret network stability fingerprinting.
- `scripts/runtime_environment.py`: UTF-8 child execution and bounded cleanup of only the adapter's own process tree.
- `scripts/merge_observations.py`: combines observations, with later captures replacing the same platform/target.
- `scripts/run_comment_review_job.py`: one-command, resumable Xiaohongshu XLSX audit orchestration with canary reuse and a machine-readable completion manifest.
- `scripts/review_comments.py`: deterministic comparison and result classification.
- `scripts/audit_validation.py`, `scripts/build_audited_workbook.py`: host-independent monetary invariants and loss-conscious OOXML export.
- `scripts/build_audited_workbook.mjs`: preserves source workbooks, backfills G/H, and creates formula-driven `审核汇总` plus audit detail.
- `scripts/maintain_audit_storage.py`: dry-run-first 14-day intermediate evidence maintenance; preserves final spreadsheets.
- `scripts/sync_openclaw_skill.sh`: force-syncs the validated source into OpenClaw agent `main` and verifies Skill readiness plus Feishu channel health without sending a message.
- `scripts/verify_feishu_intake.py`: rejects deployments whose Feishu inbound-media limit would drop image-heavy audit workbooks.
- `scripts/test_*.py`: offline regression suite.
