---
name: troubleshoot
description: Diagnose LomboShrink problems from doctor output, the convert log and swap results. Use when doctor shows FAIL, files are skipped or rejected, an encode stalls, or a swap is refused.
---

# Troubleshoot LomboShrink

Start with `lombo-shrink doctor`; each line says what failed. The full table of lines and fixes is in
`docs/troubleshooting.md`; read it rather than guessing.

Where to look:

- `OUT_DIR/convert-log.jsonl`: one JSON line per file with `status` and `msg` (the exact reason).
- `OUT_DIR/REPORT.txt`: the last run in plain words, with reasons grouped.
- The swap result file (`--result`): per file `status`, `reason`, `note`.

Rules of thumb:

- `skipped` and `rejected` are the program doing its job. Explain the reason; do not lower a bar to force a file through.
- `failed` is retried on the next run. A stall usually means a slow read from the network drive.
- A swap that reports `failed` has not touched the original. Never delete staging files or originals by hand to "unstick" it
  without asking the user first.
- When the user reports a bug, ask for `doctor` output with hostnames, addresses and keys removed.
