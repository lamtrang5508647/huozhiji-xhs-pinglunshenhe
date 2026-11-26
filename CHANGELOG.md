# Changelog

## 0.4.10 - 2026-11-26

- Treat reply-location markers such as `回复序号2` as relationship instructions, not comment bodies.
- Classify a row with an empty reply body and an attached image as image-only, using the source single-image rate.
- Keep directive-only rows with no body or image visible as invalid input instead of silently dropping or matching them.
