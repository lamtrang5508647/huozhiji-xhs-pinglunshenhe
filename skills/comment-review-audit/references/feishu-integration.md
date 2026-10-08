# Optional Feishu / OpenClaw integration

Only apply when the user uses a configured Feishu bot. Independent local/API audits do not require these settings, a bot identity or any Feishu credentials.

## Intake

In a Feishu direct chat, `请审核`, `审核一下`, `核对评论`, `评论审核`, or an XLSX carrying the known audit columns authorizes the audit workflow. Treat an attachment and adjacent instruction from the same sender as one request, including either event order. Reuse the staged attachment; do not ask for a new upload when it is already available.

For image-heavy XLSX, OpenClaw's default 30 MB inbound-media limit may be too small. This deployment needs `channels.feishu.mediaMaxMb` at least 100 MB. On `[feishu attachment unavailable]`, check the limit and message ID before asserting no upload or requesting the same file again. Distinguish size/download-permission failure from missing attachment. After a rejected binary's limit is corrected, ask for one fresh upload because that old binary is not locally recoverable.

## Delivery

Deliver each workbook separately to its verified original sender, only when authorized. Check final size and bot app identity before upload. An inbound limit does not increase Feishu IM's outbound upload limit. For oversize outputs, preserve images and use supported private Drive multipart upload if available; verify file and sender permission before sending a link.

Do not broaden access, strip images, switch bot identity, or describe a text-only fallback as a successful attachment. Save the actual receipt or blocker and read back its message ID to confirm the attachment name/private URL. Check an uncertain send receipt before retrying.

## Deployment

Only for an explicitly selected existing deployment, run `scripts/sync_openclaw_skill.sh` after tests. Verify skill Ready/visible/no missing requirements, the configured attachment limit and channel health. If sync fails, keep canonical source and report the prior deployed version separately. Do not print or modify App Secret. Bot health is not a condition for publishing or using the independent toolkit.
