# Troubleshooting

## Contents

- [Doctor lines](#doctor-lines)
- [While running](#while-running)
- [The swap](#the-swap)
- [Reporting a bug](#reporting-a-bug)

## Doctor lines

| You see | It means | Fix |
|---|---|---|
| `[FAIL] settings: LOMBO_SHRINK_X ...` (exit 2) | A setting is missing or invalid. | Fix that variable. |
| `[FAIL] library: LOMBO_SHRINK_LIBRARY_DIR must be an existing folder` | The path is wrong or the drive is not mapped. | Check the path as this machine sees it. |
| `[FAIL] output folder: OUT_DIR is inside the library` | Converted files would land in the library. | Point `OUT_DIR` at a local drive. |
| `[FAIL] ffmpeg: cannot run ...` | ffmpeg not found. | Set `LOMBO_SHRINK_FFMPEG` to its full path. |
| `[FAIL] encoder: ffmpeg lists no ...` | This ffmpeg build lacks the encoder. | Use a build that lists it (`ffmpeg -encoders`), or choose another `ENCODER`. |
| `[FAIL] encoder: hevc_nvenc failed: ...` | Listed, but the card or driver cannot run it. | Update the driver; check another program is not using all encoders; try `ENCODER=x265`. |
| `[FAIL] server: cannot reach ...` | ssh failed or `lombo-shrink` is not installed there. | Run `ssh -o BatchMode=yes user@host true`; set `REMOTE_COMMAND`. |
| `[FAIL] Jellyfin: ... HTTP 401` | The key was refused. | Create a new key (Dashboard, API Keys). |

## While running

- **Everything is `skipped`**: read the reason in the log (`convert-log.jsonl`) or `REPORT.txt`. `low bitrate` means
  the source is already small; lower `MIN_BPP` only if you accept a worse picture.
- **`rejected: saving 12.0% is below the 20% bar`**: the file did not shrink enough. That is the rule working.
- **`failed: encode stalled`**: the output stopped growing for `STALL_MINUTES`. A slow network read can cause it;
  raise the value or copy the source to the PC first.
- **Nothing happens and `Stopped: STOP file found`**: delete the `STOP` file in `OUT_DIR`.
- Check the log: `OUT_DIR/convert-log.jsonl`, one JSON line per file.

## The swap

| Reason in the result | Meaning |
|---|---|
| `no open conversion session` | Run `lombo-shrink session open` on the server. |
| `staged sha256 differs` / `staged size differs` | The upload was damaged or replaced. Nothing changed; run again. |
| `original changed: size differs` / `modified time differs` | The library file is not the one that was converted (replaced by an upgrade, for example). Nothing changed; it will be converted again later if still a candidate. |
| `staged first video stream is ..., not hevc` | The staged file is not H.265. Nothing changed. |
| `not enough free space on the disk holding the original` | Free some space or lower `MARGIN_GB`. |
| `no library folder holds the file` / `exists on 2 disks` | Path not found under `LIBRARY_DIR`/`BRANCHES`, or a duplicate across disks to resolve first. |
| `refusing a symlink` / `folder is outside the library` | A link leads out of the library; swap refuses on purpose. |
| `Refusing to apply: ... DRY_RUN is true` | The server has `DRY_RUN=true`. Set it to false there. |

A swap that said it failed never touches the original. After a swap from `.mp4` to `.mkv`, rescan the title in
Sonarr or Radarr.

## Reporting a bug

Include `lombo-shrink doctor` output and the relevant log lines, with hostnames, addresses and keys removed.
