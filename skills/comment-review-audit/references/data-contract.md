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

For `text+image` rows, the converter also records `partial_text_amount` and `partial_image_amount` when the amount header defines component rates. A verified text-only or image-only component is then settled at its component rate; without an explicit component rate it remains `待人审` with a blank amount.

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

`capture_status` may be `ok`, `blocked`, `session_expired`, `captcha`, `risk_control`, `network_changed`, `inaccessible`, or `error`. `capture_complete: true` means the comment list reached an authoritative terminal or its declared count. An empty list proves absence only when capture is healthy and complete.

Comment fields:

- `text`: visible comment text, empty for image-only comments.
- `has_image`: boolean only when the adapter has a conclusion; omit it when unknown.
- `comment_type`: optional normalized type.
- `observed_amount`: optional OCR/manual amount visible in the comment.
- `amount_source`: `field`, `text_auto`, `ocr`, or `manual`.
- `evidence_path`: absolute local evidence path.

`image_detection_complete: false` means at least one comment image state could not be established. Image-dependent expectations become `待人审`, not a negative match.

`capture_duration_ms` is optional non-secret performance telemetry for one target. It measures navigation, loading, parsing, and checkpoint preparation, but excludes the following inter-target pause. Use it to tune pacing from real batches without storing cookies or network identifiers.

`pacing_profile` is optional and records `standard` or `risk-averse` for capture evidence. It contains no account, cookie, or network information.

`parser_revision` identifies the extraction/completeness implementation that generated evidence. Automatic same-job resume requires an exact current revision and an aware `captured_at` within the past 24 hours, healthy complete capture, and known image state for every captured comment. Legacy records remain evidence but are not automatically trusted for a new live resume. Do not retrospectively label them as current-parser output.

## Internal statuses

| Status | Meaning |
|---|---|
| `verified` | Healthy evidence fully matches text/type and the configured amount rule. |
| `not_found` | Healthy complete capture contains neither the required match nor a qualifying component. |
| `mismatch` | A candidate exists but a required type/amount conflicts. |
| `partial_text` | A text+image task has the required text but definitively no attached image. |
| `partial_image` | A uniquely attributable image exists but the required text is absent. |
| `needs_review` | Duplicate/ambiguous candidate, unknown image state, or contradictory metadata. |
| `blocked` | Login, CAPTCHA, risk control, network change, inaccessible target, or incomplete capture prevents a conclusion. |

## User-facing classifications

Every row has both `result_category` and `triage`.

| Condition | `result_category` | `triage` | Amount |
|---|---|---|---|
| Verified text or image-only task | `成功` | `成功` | Approved |
| Verified text+image task | `图字成功` | `成功` | Approved |
| Text component only with explicit component rate | `文字成功` | `成功` | Text component rate |
| Image component only with explicit component rate | `图片成功` | `成功` | Image component rate |
| Text/image component only without explicit component rate | `文字成功` / `图片成功` | `待人审` | Blank |
| Complete capture, neither required component / conflicting type or amount | `失败` | `失败` | `0.00` |
| Any blocker or ambiguity | `待人审` | `待人审` | Blank |

`失败` for a text+image task means neither required component was established in a healthy complete capture. A partial component remains distinct and is paid only when its component rate is explicitly defined by the source table.

Every report row also carries `approved_amount`: the table-defined amount for `成功`/`图字成功`, the explicit component rate for settled partial results, `0.00` for `失败`, and `null` for `待人审` or partial results without a component rate. This prevents a confirmed failure from looking like an unreviewed blank in the settlement column.

The report preserves `source_file`, `source_sheet`, `source_row`, `reason_codes`, compact candidate metadata, and capture evidence whenever available.

Text normalization treats an ASCII prose full stop like Chinese punctuation but retains periods between digits, so `1.5` never matches `15` merely through punctuation normalization.
