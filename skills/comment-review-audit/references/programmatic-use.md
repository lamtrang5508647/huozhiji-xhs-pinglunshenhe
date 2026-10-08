# Independent execution

The skill scripts are usable without an agent host, Feishu or OpenClaw:

```bash
python3 scripts/table_to_expected.py --input source.xlsx --all-sheets --output expected.json
python3 scripts/review_comments.py --expected expected.json --observations observations.json --amount-mode expected-only --output report.json
python3 scripts/build_audited_workbook.py --source source.xlsx --expected expected.json --report report.json --output audited.xlsx
```

Use the original source workbook, not a prior audited H column as new pricing. The portable writer leaves the original unchanged, patches authorized G/H cells, preserves source media/cell-image definitions, creates the 14-column settlement summary and formula-linked detail, and checks exported values against validated results.

The writer checks formula cached values and sets Excel to recalculate on open. It is not a general formula engine or visual renderer. Protected sheets, signed workbooks, unsafe cross-column merges, or manual drawings/formulas that cannot be safely moved cause an explicit error; never quietly discard them.

The full repository additionally provides installable `huozhiji_audit` Python APIs, JSON CLI and program integration documentation. The shared core has no third-party runtime requirements. Live browser capture separately requires normal Chrome, Playwright and, for Xiaohongshu, the supported `xhs-cli` client. `COMMENT_AUDIT_CHROME` overrides executable discovery; browser profile locking currently targets macOS/Linux.

Offline `review` and `audit` inspect only supplied evidence. They do not independently prove the producer's completeness declarations, authenticate an account or retrieve platform data. Missing or incomplete evidence remains manual review. A service wrapper must authenticate evidence producers and isolate input/output paths; do not expose arbitrary shell commands or filesystem paths publicly.
