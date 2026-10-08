# Platform operations and session resilience

This procedure improves stability without evading platform controls.

## Environment preparation

1. Use ordinary installed Google Chrome with one dedicated persistent profile per platform. Do not use incognito, temporary profiles, or repeated browser switching for batches.
2. Keep only one login/capture process on a profile. A `browser_profile_in_use` error means another process still owns it; close or finish that process instead of deleting Chrome lock files.
3. Use one explicitly authorized account. Never paste cookies, tokens, passwords, QR payloads, or verification codes into chat or reports.
4. Start and finish a batch on the same normal network route and proxy state. The adapters store only a hash of route/proxy state. If it changes, they checkpoint and stop with `network_changed`.
5. Close competing automation for the same platform/account. Keep the computer awake and avoid changing VPN/hotspot during capture.

## Login and canary

- Navigate with the original source link, including its access query parameters. `expected.json.target_url` is a redacted identity, not a navigation URL. An error obtained from that redacted link does not prove platform risk control or target unavailability. Recheck the original source link before classification; never print its token.
- Recognize both `xhslink.com` and `xhslink.cn` short links. Resolve them through the same ordinary authorized browser session and retain the original short-link key plus the resolved note key in evidence. Stop on verification/login rather than switching accounts or routes.
- Open one visible login window and wait up to the configured timeout. Do not repeatedly close/reopen it while the user scans.
- For owner-assisted login/verification use `xhs_visible_login.py --wait-for-confirmation` in a persistent interactive terminal. Keep that window open without refreshing; only send Enter after the owner says verification is complete. The helper checks the existing page before saving the session. A challenge encountered during startup must stay visible, not disappear in exception cleanup.
- Use `--url <original-source-link>` to open the required note rather than a generic homepage. Pass the URL privately as an argument; do not echo query tokens. The profile's current `web_session` takes precedence over saved CLI cookies; only an empty profile is bootstrapped from the CLI cookie file.
- Use the standard runner with `--wait-for-confirmation` in a PTY when the owner is available. On a challenge, the capture process holds the real page and profile lock until confirmation. The runner resumes only on exit `4` (confirmed and validated). Noninteractive stops exit `3` and write a blocked manifest; owner absence never becomes confirmation. Do not manually reopen Chrome against a profile still owned by the capture process.
- Douyin owner-assisted verification similarly uses `douyin_visible_login.py --url <target> --wait-for-confirmation`. Do not close its window based on a short period without a login label; CAPTCHA and risk markers also block session acceptance.
- Xiaohongshu: a saved session file alone does not prove login. A visible `手机号登录`, `输入验证码`, `登录后推荐更懂你的笔记`, or `登录后查看更多评论` gate is `session_expired`.
- Douyin: visible scan/code/password login gates are `session_expired`; visible security verification is `captcha`.
- After login, run one known target. Confirm target identity, visible comments, capture count, completeness, and image handling before batch mode.

## Batch loop

Use navigate → observe → load comments → observe → checkpoint. Do not click social actions.

- Xiaohongshu low-risk profile (the runner default): settle the authorized session for 8–15 seconds, use three 20–35 second warm-up pauses, then adaptive 12–18 second target intervals with a 4 second rest floor and 60–90 second breaks every eight targets. Use `--headed --expand-dom`; if the first same-page DOM pass is incomplete, permit one extra DOM expansion before classifying it as incomplete. The profile never changes accounts, cookies, profiles, or network routes.
- Xiaohongshu legacy profile: adaptive 6–10 second target interval; a 25–45 second break every 12 processed targets. It is available only with an explicit `--normal-pace` runner opt-out.
- Douyin defaults: adaptive 5–8 second target interval; a 20–35 second break every 10 processed targets. Scroll the last rendered comment to trigger lazy loading.
- Adaptive pacing subtracts healthy page-processing time from the target interval but keeps at least 1.5 seconds of rest. Incomplete/error targets use the full interval. A long scheduled break replaces the ordinary pause, and no pause is taken after the final checkpoint.
- Checkpoint after every target. On restart, skip only prior healthy results; retry blocked/incomplete targets individually.
- Resume is scoped to the same job checkpoint and expires after 24 hours. A missing or different `parser_revision` requires re-capture, not silently restamping old evidence. Bump the platform's entry in `capture_resume.PARSER_REVISIONS` after extraction/completeness changes. `--refresh` explicitly rechecks selected targets; use a fresh job directory for new audit submissions. Neither resume nor faster offline processing changes platform pacing or stop conditions.
- After an unresolved stop, recheck the triggering target as a fresh canary; a cached earlier successful target cannot validate the restored session. Require healthy complete evidence before the rest of the batch. Source changes are isolated in a new job directory.
- Treat an authoritative visible terminal (`THE END`, `没有更多评论`, `暂时没有更多评论`) as complete. Preserve a warning if the platform counter is stale.
- Treat Douyin `抢首评` as a complete zero-comment result only when no login/security gate is present.
- Douyin may stop lazy loading once rendered comments reach the page-declared count. Do not stop merely because all expected texts have appeared; full capture is still required for duplicate detection and reliable failures.
- Preserve duplicate candidates. Do not deduplicate solely by visible text; duplicate exact matches are reviewed separately.

## Comment body extraction

Xiaohongshu DOM text extraction reads `.content .note-text`, not the entire comment item's label text. The sibling `回复` and `.nickname` nodes are UI metadata. Do not regex-strip user-authored prose, including comments whose actual body starts with “回复”. Re-capture legacy affected observations with the structured extractor while keeping the original evidence; never silently rewrite their text.

Retain image-only DOM comments even when their text is empty. A terminal marker must be visible to establish completeness; a hidden marker is insufficient. If any rendered comment cannot be parsed, mark the list incomplete and image detection incomplete, including when the displayed counter is stale. Parser revision `xhs-body-20261008-v3` applies these checks; older observations remain evidence but are not reusable by the current parser.

Douyin similarly reads the observed comment-body span (`.FduGc_lz > .Sh1Da424`) separately from sibling `.comment-item-tag` badges such as “作者赞过”. Preserve emoji-image `alt` text in the body, excluding avatars and labels. Recheck a real target when the DOM changes; never delete matching words from user-authored prose to remove a badge.

## Images

- Xiaohongshu uses API image fields and visible `.comment-picture` attachments when expanding DOM comments.
- Douyin checks explicit comment-attachment markers and then conservative image dimensions while excluding avatar/emoji labels.
- If image inspection throws, times out, or is unsupported, set `image_detection_complete: false` and omit `has_image`. Never write `has_image: false` merely because detection failed.
- OCR is supporting evidence, not proof. Multiple OCR amount candidates remain `待人审`.

## Stop conditions

Stop immediately on login expiration, CAPTCHA, risk/frequency warning, security review, network change, inaccessible navigation twice in succession, or profile lock conflict. Save the last machine-readable observation and do not continue opening links.

For Xiaohongshu, `website-login/error`, `300012`, `安全限制`, or `IP存在风险` is `risk_control`. For Douyin, `访问频繁`, `操作频繁`, `系统繁忙`, `存在风险`, or a security verification gate is a stop.

Do not respond to risk control by rotating accounts, changing browsers, switching to incognito, rapidly retrying, or changing network mid-batch. Resume only after the user has restored a normal authorized session, then rerun one canary before the remaining blocked targets.

## Evidence and storage

- Record capture time, `capture_duration_ms`, platform, sanitized target key/URL, completeness, image-detection state, warnings, and a local evidence path.
- Keep final audited `.xlsx` files. Keep raw JSON/screenshots/previews in the managed audit output root.
- Run storage maintenance in dry-run mode first. On apply, raw evidence older than 14 days is verified in a ZIP before originals are removed; those compressed archives are pruned after the archive-retention window (14 days by default). Final spreadsheet files are always preserved.
- Never point storage maintenance at `/`, the home directory, or an unrelated workspace root.
