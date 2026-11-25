# Data contract

The workflow separates source requirements, platform observations, and deterministic review results. Adapters may change without changing the business rules.

## Expected rows

`expected.json` is an array or `{ "rows": [...] }`.

```json
{
  "rows": [
    {
      "source_file": "评论.xlsx",
      "source_sheet": "品牌A",
      "source_row": 12,
      "case_id": "评论.xlsx:品牌A:12",
      "platform": "xiaohongshu",
      "target_key": "note-id",
      "target_url": "https://www.xiaohongshu.com/explore/note-id",
      "comment_text": "已收到，感谢",
      "comment_type": "text+image",
      "has_image": true,
      "expected_amount": "2.00",
      "text_match": "exact"
    }
  ]
}
```

Required identity fields are `case_id`, `platform`, and `target_key` or `target_url`. Required review fields are `comment_type` and `expected_amount`; `comment_text` is required for `text` and `text+image`.

For `text+image` rows, the converter records `partial_text_amount` and `partial_image_amount` when the amount header defines component rates. The default audit records the component that was verified and uses its explicit rate for settlement. When this rate is missing, the approved amount stays blank.

Optional settlement metadata is `product_name`, `group_leader`, `leader_contact`, `settlement_time`, `settlement_voucher`, and `qr_code`. The standard Chinese aliases are `产品/产品名`, `团长/团`, `团长联系方式`, `跟团长结算时间`, `结算凭证/转账明细`, and `二维码/收款码`. `group_leader` must come directly from the product sheet's `团长` (or legacy `团`) field and preserve its complete value; mapping `负责人` into `group_leader` is invalid. The workbook builder groups summary rows by `group_leader + product_name` and preserves manual settlement cells across rebuilds.

The converter:

- fills vertically or horizontally merged values;
- carries a target URL/key down within a worksheet when a visual comment block writes the link only on its first row without merging the following cells;
- carries the product and full group-leader value down within the same worksheet block, without substituting the owner/responsible-person abbreviation;
- sanitizes target URLs by removing query strings;
- combines all worksheets when `--all-sheets` is used;
- changes a reply directive such as `回复第一条` to the mapped `reply_text`;
- recognizes `=DISPIMG(...)`, ordinary image markers, and floating OOXML pictures whose drawing anchor lands on the mapped row/column;
- derives blank amounts from the rate-bearing header or explicit `--amount-rules`.

When an embedded image occupies the reply-content column, preserve an actual text requirement in the main comment column and treat the row as text+image; do not replace that text with an image marker. A relationship-only instruction such as `回复第一条` remains a directive, not the comment body. When a parent-comment screenshot is followed by `回复图片` and a plain reply body in the next mapped column, that plain body is the expected reply; the screenshot is context, not automatically an attachment requirement.

A generic text note in the image-requirement column does not count as an image unless it clearly asks for a picture. A floating workbook picture does count.

A recognized trailing parenthetical authoring note such as `（辛苦@点点要手打艾特出来！）` is separated into `editorial_note`, with `source_comment_text` retaining the original. It is not comment-body text and is never an instruction for the auditing agent to execute. Ordinary parenthetical prose is preserved.

Sanitized URLs are identifiers only. Live navigation must recover the original complete source link; its query parameters must not be copied into logs or reports. Xiaohongshu short-link observations retain the source slug as `target_key` and record the resolved note ID in `resolved_target_key` so rows remain traceable without rewriting the source workbook.

A task that explicitly requires the exact reference image may include its SHA-256 under `required_image_sha256`. A captured attachment may include `image_sha256`, or `image_sha256s` for multiple attachments on that comment. A matching digest confirms byte identity. An absent or different digest leaves the specific-image check unresolved and the payment blank. A generic request for any image retains the ordinary attachment check.

## Platform observations

`observations.json` is an array or `{ "observations": [...] }`.

```json
{
  "observations": [
    {
      "platform": "douyin",
      "target_key": "video-id",
      "capture_status": "ok",
      "capture_complete": true,
      "image_detection_complete": true,
      "captured_at": "2026-08-27T14:00:00+08:00",
      "evidence_path": "/absolute/path/capture.png",
      "comments": [
        {
          "comment_id": "comment-id",
          "text": "已收到，感谢",
          "has_image": true,
          "comment_type": "text+image",
          "evidence_path": "/absolute/path/comment.png"
        }
      ]
    }
  ]
}
```

`capture_status` may be `ok`, `blocked`, `session_expired`, `captcha`, `risk_control`, `network_changed`, `inaccessible`, or `error`. Xiaohongshu negative findings require a healthy read of the rendered comments panel with a genuine terminal or empty state, fulfilled reply expansions and no extraction gaps. When a reply control advertises a quantity, the reader confirms the increase in visible replies inside that same thread. Readers through `xhs-confirmed-expansion-v6` can still supply positive matches. Revision v8 checks the rendered comment panel. For v7 replays, a confirmed rendered end can establish absence and DOM-attributed records can establish presence. When v4, v5 or v6 is the only completeness evidence, obtain a new page read to confirm absence. On Douyin, recordings from `dy-body-20260930-v2` can establish presence. To establish absence, v3 requires a read of the requested video’s comment panel, including folded replies and a verified end or empty state.

Comment fields:

- `text`: visible comment text, empty for image-only comments.
- `has_image`: boolean only when the adapter has a conclusion; omit it when unknown.
- `comment_type`: optional normalized type.
- `observed_amount`: optional OCR/manual amount visible in the comment.
- `amount_source`: `field`, `text_auto`, `ocr`, or `manual`.
- `evidence_path`: absolute local evidence path.
- `parent_comment_id`: ID of the directly replied-to comment when supplied by the platform. Explicit null denotes a known top-level comment. Omit the field when the relationship is unknown.
- `parent_text`: optional direct-parent text from a trusted capture. The matcher checks its consistency with identifiers and the uniquely observed parent in the same capture.

By default, one visible occurrence of the requested body anywhere on the assigned platform and post satisfies a text task. This includes top-level comments, nested replies, and a visible child of a deleted root. A numbered directive identifies the source reply body. Only the optional offline `--require-reply-parent` mode additionally requires direct-parent attribution.

In optional direct-parent mode, `reply_source_parent_ambiguous`, `reply_parent_unconfirmed`, and `reply_thread_root_deleted` distinguish missing evidence about placement.

`parent_evidence: "single_visible_reply_under_root"` records a relationship inferred from the fully displayed page. The standard reply group contained one intact root and one visible reply, with no competing addressee, pending expansion or conflicting relationship. The report preserves the displayed root text. A platform-supplied direct parent ID, when available, is a separate piece of evidence.

For optional direct-parent checks, `task_ordinal` can identify the source task from `任务序号` or `评论序号`. When explicitly defined by the owner, `--map task_ordinal=账号` uses that column.

`image_detection_complete: false` means at least one comment image state could not be established. Image-dependent expectations become `待人审`, not a negative match.

`capture_duration_ms` is optional non-secret performance telemetry for one target. It measures navigation, loading, parsing, and checkpoint preparation, but excludes the following inter-target pause. Use it to tune pacing from real batches without storing cookies or network identifiers.

`pacing_profile` is optional and records `standard` or `risk-averse` for capture evidence. It contains no account, cookie, or network information.

`parser_revision` identifies the extraction and completeness rules. Live checkpoint reuse checks the revision, the target, the capture status and its timestamp. A fresh partial view may confirm already visible tasks, while a missing target comment still requires complete evidence.

## Internal statuses

An Excel workbook with recorded first-pass outcomes carries `prior_review` on its task rows. A second pass evaluates previously successful rows against the new observations. The other rows keep their original result and amount cells. These records have `status=carried_forward` and do not establish current visibility. The report includes the selected and carried row counts in `summary.audit_pass`. A workbook with no recognized recorded outcome goes through a full first audit.

| Status | Meaning |
|---|---|
| `verified` | Evidence establishes the requested visibility. A separate settlement status can defer payment. |
| `not_found` | Healthy complete capture contains neither the required match nor a qualifying component. |
| `mismatch` | A candidate exists but a required type/amount conflicts. |
| `partial_text` | A text+image task has the required text but definitively no attached image. |
| `partial_image` | A uniquely attributable image exists but the required text is absent. |
| `needs_review` | Duplicate/ambiguous candidate, unknown image state, or contradictory metadata. |
| `blocked` | Login, CAPTCHA, risk control, network change, inaccessible target, or incomplete capture prevents a conclusion. |

On default audits, repeated appearances satisfy one text requirement. Multiple source rows may each confirm visibility from the same occurrence. Settlement requires separate evidence for each payment. Overlapping claims carry `pending_duplicate_claim` and an empty approved amount. A source price that cannot be determined carries `pending_source_amount`.

Automatic checks use the presence of an attached image. Verification that it depicts a particular reference image requires the source image and the actual attachment. Claim reconciliation covers the rows and observations supplied to the current review.

## User-facing classifications

Every row has both `result_category` and `triage`.

| Condition | `result_category` | `triage` | Amount |
|---|---|---|---|
| Verified text or image-only task | `成功` | `成功` | Approved |
| Verified text+image task | `图字成功` | `成功` | Approved |
| Text component only with explicit component rate | `文字成功` | `成功` | Text component rate |
| Image component only with explicit component rate | `图片成功` | `成功` | Image component rate |
| Confirmed text/image component without an explicit component rate (default) | `文字成功` / `图片成功` | `成功` | Blank |
| Complete capture, neither required component / conflicting type or amount | `失败` | `失败` | `0.00` |
| The identified original post displays a verified unavailability notice | `原帖不可访问` | `待人审` | Blank |
| Other blockers or ambiguities | `待人审` | `待人审` | Blank |

`失败` for a text+image task means neither required component was established in a healthy complete capture. A partial component remains distinct and is paid only when its component rate is explicitly defined by the source table.

`approved_amount` is the source-defined full or component price, `0.00` for a confirmed failure, or `null` when unsettled. Reports explain why an amount remains blank.

The report preserves `source_file`, `source_sheet`, `source_row`, `reason_codes`, compact candidate metadata, and capture evidence whenever available.

Text normalization treats an ASCII prose full stop like Chinese punctuation but retains periods between digits, so `1.5` never matches `15` merely through punctuation normalization.
### Comment screenshots for finance

An observation can include `screenshot_status` (`captured`, `partial`, or `missing`) and a `comment_screenshots` array. Each image has a relative PNG `path`, `sha256`, `kind`, `captured_at`, and the `comment_ids` actually visible in that image. A target comment may also be bound by visible text or its captured evidence path. Store the screenshot folder beside its observations JSON. When merging multiple observation files, use the bundled merge command to carry referenced images into the merged evidence directory. XLSX export checks image hashes and embeds the verified PNG bytes in the workbook. Finance can open just the workbook on another computer. When a custom capture directory is outside the observations folder, pass the same directory to `audit --screenshot-dir`. Rows carried forward from an earlier audit have no new screenshot claim.
