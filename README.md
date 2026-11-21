<p align="center"><img src="assets/banner.svg" alt="LomboShrink" width="100%"></p>

<p align="center">
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/Lombo-Forge/lombo-shrink"></a>
  <a href="https://github.com/Lombo-Forge/lombo-shrink/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-shrink/ci.yml?branch=main&label=CI"></a>
  <a href="https://github.com/Lombo-Forge/lombo-shrink/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-shrink/codeql.yml?branch=main&label=CodeQL"></a>
  <a href="https://github.com/Lombo-Forge/lombo-shrink/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Lombo-Forge/lombo-shrink?include_prereleases"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <a href="https://ko-fi.com/datlombardo"><img alt="Support on Ko-fi" src="https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi&logoColor=white"></a>
</p>

# LomboShrink

Re-encodes the H.264 files in your library to H.265 on your main PC with a discrete graphics card
(NVIDIA, AMD or Intel), then swaps the smaller files into the server's library, only after checking
that nothing was lost. Works with any Jellyfin library; it needs no Jellyfin to run.

```
$ lombo-shrink plan
would convert movies/Big Film (2016)/Big Film (2016).mkv (21931 MB) with nvenc CQ 26
would convert movies/Other Film (2012)/Other Film (2012).mkv (9120 MB) with nvenc CQ 26

Dry run: 2 files with nvenc. Nothing was encoded. Set LOMBO_SHRINK_DRY_RUN=false to convert.
```

## Contents

- [Install in a minute](#install-in-a-minute)
- [Commands](#commands)
- [How a file is kept safe](#how-a-file-is-kept-safe)
- [Settings](#settings)
- [What is not verified](#what-is-not-verified)
- [FAQ](#faq)
- [Contributing and licence](#contributing-and-licence)

## Install in a minute

On the main PC (Windows, Linux or macOS) you need Python 3.12+ and an ffmpeg that has your graphics
card's H.265 encoder (check with `ffmpeg -encoders`). Nothing else is installed.

1. `pip install .` in this folder (or `pipx install .`).
2. Copy `.env.example` to `.env`, set `LOMBO_SHRINK_LIBRARY_DIR` to the library as the PC sees it
   (a mapped network drive is fine; the scripts only read it) and `LOMBO_SHRINK_OUT_DIR` to a folder
   on a fast local drive. The program does not read `.env` itself: load it in your shell first (for example
   `set -a; . ./.env; set +a`) or set the variables another way.
3. Run the checks and look at the plan:
   ```sh
   lombo-shrink doctor      # tests the encoder by encoding one second of generated video
   lombo-shrink plan
   ```
4. Convert one file first: set `LOMBO_SHRINK_LIMIT=1` and `LOMBO_SHRINK_DRY_RUN=false`, run
   `lombo-shrink run`, and play `<OUT_DIR>/<source name>.hevc.mkv` in your player.

That much only reads the library and writes into `OUT_DIR`. To swap files into the server's library,
follow [docs/setup.md](docs/setup.md) (install on the server too, set up key-only ssh, open a session).

## Commands

| Command | What it does |
|---|---|
| `lombo-shrink doctor` | Checks settings, ffmpeg, that the chosen encoder really encodes, the library, and the server if one is set. Exit code 1 if anything fails. |
| `lombo-shrink candidates [--out FILE]` | Lists the files worth converting (JSON, biggest first). Only reads. |
| `lombo-shrink plan [--list FILE]` | Shows what `run` would convert. Never changes anything. |
| `lombo-shrink run [--list FILE]` | Converts into `OUT_DIR` (needs `DRY_RUN=false`), and ships each accepted file if `SHIP_TARGET` is set. |
| `lombo-shrink review` | Lists converted files still waiting on this PC. Only reads. |
| `lombo-shrink swap --manifest M --result R [--apply]` | Server side: checks, and with `--apply` performs, the swap. |
| `lombo-shrink session open [HOURS] / close / status` | Server side: the lock that allows `swap --apply`. It expires by itself. |

To stop a run cleanly, create a file named `STOP` in `OUT_DIR`.

## How a file is kept safe

- The library is never written by the PC. Results go to `OUT_DIR` under a `.partial.mkv` name and get
  their final name only after every check passed.
- Files are skipped, not converted, when they are already H.265, interlaced, HDR or Dolby Vision, have
  more than one video stream, use a subtitle format mkv cannot copy, or already have a very low bitrate.
- A result is accepted only if it is at least 20% smaller, has the same streams, chapters, size, frame
  rate, language and default flags, the same number of video packets, the same length (1.5 s), and decodes
  with no error from start to end. Otherwise the original stays.
- The swap re-checks the sha256 and size of the staged file, that the original is still the file that was
  converted (same size and modified time), that the staged video is H.265, and that the disk has room. The
  new file is written next to the original, checked again, then renamed into place. A copy that does not
  match never replaces anything.
- Everything that changes things defaults to a dry run, and `swap --apply` additionally needs
  `DRY_RUN=false` on the server and an open session.

More in [docs/how-it-works.md](docs/how-it-works.md).

## Settings

Everything is an environment variable, listed with defaults in [.env.example](.env.example).

## What is not verified

Written plainly, as of the first build:

- **NVIDIA (`nvenc`) has been run on a real graphics card:** an RTX 3080 (driver 610.88) under WSL2 Ubuntu, with a
  static ffmpeg build. `doctor` passed, and two real 21-minute episodes were converted at about 5.8 times real
  time, 78% and 79% smaller, with all audio and subtitle tracks kept and a clean full decode. That run found the
  Ctrl+C bug fixed in 0.1.1. Playback in a media player was not checked. **AMD (`amf`), Intel (`qsv`) and macOS
  (`videotoolbox`) are best-effort mappings, never run on that hardware.** `doctor` tells you at once if an
  encoder does not work.
- **The swap was tested only in temporary folders**, against fake `ffprobe`, `ssh` and `scp` stand-ins, not
  against a real mergerfs pool, a real server over ssh, or a real Jellyfin.
- **Native Windows was not tried** (the NVIDIA run was under WSL2): mapped network drives, paths with spaces and non-English
  characters, keeping the PC awake, the STOP file and Ctrl+C there are untested.
- The per-file quality probe (VMAF) of the original scripts is **not included**: a fixed CQ is used.
- There is no standalone ship command. Files converted earlier and still on the PC are shipped by the next
  `run` (that file must still be in the candidate list); that retry was tested only with stand-ins.
- A library file that changes in the instant between the last re-check and the rename is not caught.
- `KEEP_ORIGINALS` holds originals in `.lombo-shrink-holding` inside the library; tested with a simulated mount
  boundary, not a real separate mount.
- Sonarr and Radarr are not told about a swap (only Jellyfin is, if configured). After a swap from
  another container such as `.mp4`, rescan the title in the arr app.

## FAQ

**Is it safe to try?** `plan` and `candidates` only read. `run` writes only into `OUT_DIR`. Nothing touches the library until you set up the server side, open a session and set `DRY_RUN=false` there.

**Why not Tdarr?** A plain script is easier to audit: it keeps audio and subtitles as they are, refuses anything odd, and puts every decision in one small file.

**Will every device play H.265?** Most phones, TVs and Apple devices do; a device that cannot gets a transcode from Jellyfin, as with any H.265. Output is 10-bit, about 19% smaller than 8-bit at the same quality in the measurements, but needs Main 10 decoding.

**What about originals that a torrent still seeds?** They are left out of the list by default (`SKIP_HARDLINKED`). If one is swapped anyway, only the library name changes; the torrent's data is untouched.

## Contributing and licence

Contribution, security and conduct rules come from the [Lombo-Forge organisation](https://github.com/Lombo-Forge/.github). Licensed under [GPL-3.0](LICENSE). Credits are in [NOTICE.md](NOTICE.md). Not affiliated with or endorsed by the Jellyfin project.
