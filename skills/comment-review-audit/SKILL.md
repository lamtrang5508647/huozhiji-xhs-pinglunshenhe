---
name: comment-review-audit
description: Audit required text/image comments on Xiaohongshu or Douyin against XLSX requirements, reconcile table-defined amounts, and return audited workbooks with settlement summaries. Use for 评论审核/核对评论 requests or Feishu audit-template attachments; associate adjacent messages from the same sender.
---

# Comment Review Audit

Use this skill to reconcile comment requirements in CSV/XLSX/JSON tables against comments visible on Douyin or Xiaohongshu. Produce an auditable workbook, not only a prose answer.

## Mandatory Feishu routing

- In a Feishu direct chat, the phrases `请审核`, `审核一下`, `核对评论`, `评论审核`, or an XLSX carrying the known audit columns are sufficient authorization to run the complete audit workflow.
- Treat an attachment and an adjacent instruction from the same sender as one request even when Feishu delivers them as separate events or in either order.
- Prefer this skill over a generic spreadsheet skill. The spreadsheet skill is a supporting implementation skill only; it must not replace this audit workflow.
- Do not ask what the sender wants when the workbook matches the audit template. Acknowledge receipt briefly, then start the canary and full resumable audit.
- If the instruction arrives after the attachment has already been inspected, reuse the staged attachment path from the current conversation and continue; do not request another upload unless the binary is actually unavailable.

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
   For Feishu intake, image-heavy workbooks can exceed OpenClaw's default 30 MB inbound-media limit. Keep `channels.feishu.mediaMaxMb` at 100 MB or higher and restart the gateway after changing it. When an inbound event contains `[feishu attachment unavailable]`, do not claim that the sender failed to upload and do not repeatedly ask for identical re-uploads. First check the configured limit and the event's `message_id`; distinguish an oversize/download-permission failure from an absent attachment. After correcting the limit, request one fresh upload because a previously rejected binary is not recoverable from the local staging directory.
3. Establish the persistent desktop environment before opening batch targets:
   - Xiaohongshu profile: `~/.comment-review-audit/xhs-profile`
   - Douyin profile: `~/.comment-review-audit/douyin-profile`
   - Keep the profile directory mode at `0700`; the included profile lock prevents two processes from corrupting one session.
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
8. Build the audited workbook with the spreadsheet runtime supplied by the host. Preserve the original sheets and embedded images, write the row category to column G and the approved amount to column H, and add/refresh `审核汇总` in the required settlement template: `团长 / 产品名 / 涉及工作表数 / 总条数 / 成功数 / 失败数 / 需人审数 / 成功率 / 报销金额 / 涉及工作表 / 团长联系方式 / 跟团长结算时间 / 结算凭证 / 二维码`. The summary `团长` must directly use the complete value from the product sheet's `团长` field (legacy `团` is accepted); never substitute the `负责人` abbreviation. Group rows by source `团长` + `产品`; preserve previously entered values in the last four manual columns when rebuilding.

   ```bash
   node scripts/build_audited_workbook.mjs \
     --source /path/review.xlsx --expected /path/expected.json \
     --report /path/review-report.json --output /path/review-audited.xlsx \
     --preview-dir /path/evidence/previews
   ```

   Follow the spreadsheet skill: inspect/render the source before editing, render every output sheet, scan formula errors, and reconcile summary totals with the JSON report. The summary formulas must remain traceable to per-row results.
   If a cropped preview appears blank, compare the exported cell values, styles and actual merge ranges, then render a view beginning at column A before diagnosing missing data or changing layout. A preview alone does not prove merged cells or failed writes.
   For standard Xiaohongshu XLSX jobs, prefer the one-command resumable runner below. It converts the workbook, reuses a prior healthy canary, captures each unique note once, checkpoints every target, compares rows, builds previews/workbook, and writes `job-result.json`. Re-running the same command resumes healthy completed targets instead of opening them again.

   ```bash
   python3 scripts/run_comment_review_job.py \
     --source /path/incoming.xlsx \
     --work-dir /path/job-output \
     --output-name incoming-最终审核版.xlsx
   ```

   For owner-assisted desktop work, add `--wait-for-confirmation` and launch in an interactive PTY. A startup or target challenge checkpoints immediately and stays on the actual page until the owner confirms; send Enter only after that confirmation. A still-blocked page keeps waiting. Exit code `4` means the helper validated the owner's completed verification, so the runner may resume; exit `3` is an unresolved stop and must not be automatically retried. For unattended runs, the default exits with a checkpoint; open `xhs_visible_login.py --url <original-source-link> --wait-for-confirmation` once in the same profile to restore it.
   A post-stop canary must be freshly captured. An incomplete canary blocks the batch. Reusing a complete canary is only permitted for a healthy uninterrupted same-job resume.
   `job-result.json` is rewritten to `in_progress` at run start, then `blocked`/`error` on interruption. `complete` requires exact row identities, valid categories, monetary invariants, a nonempty workbook and reconciled summary. It contains source/output SHA-256 fingerprints; a changed source requires a new job directory. These checks protect against accidental delivery of a previous run's result.

   A nonzero capture exit means the platform requested a stop (login, CAPTCHA, risk control, network change, or repeated navigation failure). Keep the checkpoint, notify the owner, and rerun the same command only after the normal authorized session is restored. Do not send the output unless `job-result.json` has `status: complete` and final workbook visual verification passes.
   Deliver each workbook separately to its verified original sender through the configured audit bot. Check file size and bot app ID before upload, not after a failed send. A larger inbound-media limit does not raise Feishu IM's outbound upload limit. For an oversized final workbook, preserve its original images; use supported Drive multipart upload and private access for that sender if available, then verify the uploaded file and recipient permission before sending its link. Do not broaden public access, silently strip images, use another app identity, or call a text-only fallback a successful attachment delivery. Save the actual message receipt or explicit delivery blocker. Read back the receipt's message ID: confirm file name for attachments, or the verified private URL for text/rich-post link delivery. On an uncertain send outcome, check that receipt before retrying to avoid duplicate deliveries.
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
4. Run all offline tests, syntax compilation, workbook formula scan, and visual verification.
5. Update the data contract when a status or reason code changes.
6. Treat deployment as part of the iteration: after validation, rebuild the distributable ZIP and run `scripts/sync_openclaw_skill.sh`. An iteration is not complete until the script confirms that `comment-review-audit` is Ready, visible to OpenClaw agent `main`, has no missing requirements, the Feishu inbound attachment limit is at least 100 MB, and the Feishu channel is connected and working.

If synchronization fails, keep the validated source as the canonical version, report that the Feishu bot is still on the prior version, and retry only after the local OpenClaw gateway or channel is healthy. Never modify or print the Feishu App Secret during synchronization.

Never “improve” success rate by weakening completeness, image, duplicate, or risk-control safeguards.

## Included resources

- `scripts/table_to_expected.py`: converts CSV/TSV/JSON/XLSX and detects merged cells plus floating XLSX pictures.
- `scripts/xhs_visible_login.py`, `scripts/xhs_persistent.py`, `scripts/xhs_batch_capture.py`: persistent Xiaohongshu login and paced checkpoint capture.
- `scripts/douyin_visible_login.py`, `scripts/douyin_batch_capture.py`: persistent Douyin login, comment/image parsing, lazy loading, pacing, and checkpoint capture.
- `scripts/capture_pacing.py`: shared adaptive target pacing that counts page work, retains a minimum rest, and avoids tail sleeps.
- `scripts/capture_resume.py`: shared freshness, parser-revision and completeness checks; plans pending targets before opening a browser.
- `scripts/profile_lock.py`, `scripts/session_environment.py`: single-session profile protection and non-secret network stability fingerprinting.
- `scripts/merge_observations.py`: combines observations, with later captures replacing the same platform/target.
- `scripts/run_comment_review_job.py`: one-command, resumable Xiaohongshu XLSX audit orchestration with canary reuse and a machine-readable completion manifest.
- `scripts/review_comments.py`: deterministic comparison and result classification.
- `scripts/build_audited_workbook.mjs`: preserves source workbooks, backfills G/H, and creates formula-driven `审核汇总` plus audit detail.
- `scripts/maintain_audit_storage.py`: dry-run-first 14-day intermediate evidence maintenance; preserves final spreadsheets.
- `scripts/sync_openclaw_skill.sh`: force-syncs the validated source into OpenClaw agent `main` and verifies Skill readiness plus Feishu channel health without sending a message.
- `scripts/verify_feishu_intake.py`: rejects deployments whose Feishu inbound-media limit would drop image-heavy audit workbooks.
- `scripts/test_*.py`: offline regression suite.
