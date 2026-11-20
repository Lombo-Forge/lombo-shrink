# How it is built

## Contents

- [The shape](#the-shape)
- [Contracts](#contracts)
- [Why it is split this way](#why-it-is-split-this-way)
- [Safe by default](#safe-by-default)
- [What was kept, cut and generalised](#what-was-kept-cut-and-generalised)
- [Adding an encoder](#adding-an-encoder)

## The shape

```
main PC                                                         server
library --scan.py--> candidates (JSON)
                          |
                    batch.py --media.py (ffprobe/ffmpeg)--> OUT_DIR/*.hevc.mkv + convert-log.jsonl
                          |  core.py decides every step
                      ship.py --ssh/scp--> staging folder + manifest
                                           swap.py (`lombo-shrink swap --apply`) --> library
```

`core.py` never touches the disk, the network, the clock or a subprocess. `media.py`, `scan.py`,
`batch.py`, `ship.py`, `swap.py` and `session.py` do the I/O. `cli.py` is the only place that prints.

## Contracts

**Candidate list** (JSON list, biggest first): `{"path": "movies/X (2019)/X.mkv", "size": 123, "codec": "h264", "title": "X (2019)"}`.
`path` is relative to `LIBRARY_DIR`, with `/` separators.

**Log** (`OUT_DIR/convert-log.jsonl`, one JSON object per line): `ts, path, status, src_bytes, out_bytes, msg, title, cq,
encoder, encode_seconds, src_mtime`. Statuses: `accepted, rejected, skipped, failed, missing`. The first four of
`accepted, rejected, skipped, missing` are final; `failed` is retried on the next run.

**Manifest** (what the PC uploads; a JSON list with one entry per file):
`{"path", "staged", "sha256", "out_bytes", "src_bytes", "src_mtime"}`. `path` is the library path, `staged` is
relative to the server's staging folder, `src_mtime` is seconds since the epoch of the original as it was
converted (the original scripts used a local-time string; epoch seconds cannot be misread across time zones).
Paths must be relative with no `..`.

**Swap result** (`--result FILE`, always written): `{"files": [{"path", "status": "swapped|skipped|failed", "reason", "note"}]}`,
plus `"dry_run": true` on a dry run. Exit 0 only if every entry is swapped or "already swapped".

**Session** (`SESSION_FILE`): `{"opened": <epoch>, "expires": <epoch>}`; other tools may read it to stand down.

## Why it is split this way

Rules are what changes most and breaks most quietly. Because `skip_reason`, `accept_problems`,
`encode_command` and the manifest checks take plain data and return plain data, a test needs no video file and
no graphics card. The I/O layers are small and are tested with shell-script stand-ins for ffmpeg, ffprobe, ssh
and scp (`tests/conftest.py`, `tests/test_batch.py`), and once with a real ffmpeg on generated video.

## Safe by default

- `DRY_RUN` is true until the person sets it to false. `plan`, `candidates` and `review` never change anything.
- `swap` without `--apply` runs every check and changes nothing; `--apply` also needs `DRY_RUN=false` on that
  machine and an open session (`REQUIRE_SESSION`).
- The PC never writes into the library; only `swap` does, and only after every check.
- Nothing derived from input is removed recursively: single files and empty folders only.
- A bad setting stops the app at start-up and names the variable.

## What was kept, cut and generalised

Ported from the proven PowerShell and shell scripts of the private stack this came from, rewritten in Python so
one runner works on Windows, Linux and macOS and the rules can be tested:

- **Kept:** every skip rule, every accept rule, the `.partial` then rename flow, the stall and run-time watchdog,
  GPU decode with a CPU retry, resume from the log (failed is retried), the STOP file, the free-space and size
  limits, the report, and every safety check of the swap (sha256 twice, original unchanged, same-disk write,
  mode/owner/time copied, hold folder, hard-link note, idempotent repeat).
- **Cut:** the admin panel that produced the list (now `scan.py`), a Sonarr/Radarr refresh through that panel,
  the per-file VMAF probe (needs a special ffmpeg build; fixed CQ instead), the SMB share setup, Tdarr history.
- **Generalised:** NVIDIA-only to `nvenc`, `amf`, `qsv`, `videotoolbox`, `x265`; hard-coded drive letters and
  paths to settings; the server name to `SHIP_TARGET`; the server's mover lock to a plain session file;
  library layout to any layout (mergerfs branches optional via `BRANCHES`).

## Adding an encoder

Add its ffmpeg codec name to `ENCODER_CODECS`, its arguments to `encoder_args` in `core.py`, a line to
`ENCODER_CHOICES` in `config.py` and `.env.example`, and a test. Then run `lombo-shrink doctor` on a machine
that has the hardware: `doctor` encodes one second of generated video with it.
