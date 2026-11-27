# Setup

## Contents

- [What runs where](#what-runs-where)
- [On the main PC](#on-the-main-pc)
- [On the server](#on-the-server)
- [Connecting them](#connecting-them)
- [A first real swap](#a-first-real-swap)
- [Without shipping](#without-shipping)

## What runs where

- **Main PC** (a discrete graphics card, NVIDIA, AMD or Intel): finds candidates, encodes into `OUT_DIR`, ships.
- **Server** (where the library lives): runs `lombo-shrink swap` when the PC asks over ssh.

The two only talk over ssh and scp, key login, on your own network. Do not expose ssh to the internet for this.
The PC needs the library only to **read** it. A read-only network share is the safest way; the PC never writes
there.

## On the main PC

1. Install Python 3.12+ and an ffmpeg with ffprobe whose build lists your encoder (`ffmpeg -encoders`):
   `hevc_nvenc` (NVIDIA), `hevc_amf` (AMD), `hevc_qsv` (Intel), `hevc_videotoolbox` (macOS), or `libx265` (any
   processor, slow). Put them on `PATH` or set `LOMBO_SHRINK_FFMPEG` and `LOMBO_SHRINK_FFPROBE`.
2. `pip install .` in this folder.
3. `.env`: copy `.env.example`. Set `LIBRARY_DIR`, `OUT_DIR`, and, when shipping, `SHIP_TARGET` (`user@host`).
   Environment variables must be exported or loaded by your shell or service manager; the program does not read
   `.env` by itself (compose and `docker --env-file` do).
4. `lombo-shrink doctor`. Fix every `[FAIL]` line first (see [troubleshooting](troubleshooting.md)).

Windows: keep the PC awake is handled for you while a run is going. If scripts are blocked, none are used.
Run it from a normal terminal; map the library to a drive letter and use it as `LIBRARY_DIR`.

## On the server

1. Install Python 3.12+ and `pip install .` (or run the container from `compose.example.yaml`). Install `ffprobe`
   if you can: with it the server re-checks that the staged video is H.265 before swapping.
2. Set, in the environment the ssh command will see: `LOMBO_SHRINK_LIBRARY_DIR` (or `BRANCHES` with mergerfs),
   `LOMBO_SHRINK_STAGE_DIR`, and `LOMBO_SHRINK_DRY_RUN=false` (the swap refuses to apply otherwise). Optionally
   `LOMBO_SHRINK_JELLYFIN_URL` and key, so Jellyfin is asked to rescan and to read each swapped
   file again after a swap.
3. Make the account that runs it able to write the library folders (same owner as the media files). The image
   does not run as root: it defaults to user `1000:1000`, and `compose.example.yaml` sets `user:` to the owner of
   your media files. With plain `docker run`, pass `--user <uid>:<gid>` of that owner. Root is not needed: when the
   swap cannot copy the original file's owner it says so in the result and carries on.
4. With mergerfs: `LOMBO_SHRINK_BRANCHES=/mnt/disk1/media:/mnt/disk2/media` (the library folder on each disk).

Because ssh runs non-interactive shells, put the settings in a small wrapper script on the server and point
`LOMBO_SHRINK_REMOTE_COMMAND` at it, for example `/usr/local/bin/lombo-shrink-server` containing
`export LOMBO_SHRINK_...` lines and `exec lombo-shrink "$@"`.

## Connecting them

1. On the PC, create a key and install it on the server (`ssh-keygen`, `ssh-copy-id user@server`). Test
   `ssh -o BatchMode=yes user@server true`; it must not ask for anything.
2. Set `LOMBO_SHRINK_REMOTE_STAGE_DIR` on the PC to the same folder as `LOMBO_SHRINK_STAGE_DIR` on the server.
3. `lombo-shrink doctor` on the PC now has a `server` line: it checks that ssh works and the command exists.

## A first real swap

On the server: `lombo-shrink session open 2` (the swap refuses without it, and it expires by itself).

On the PC: set `LOMBO_SHRINK_LIMIT=1` and `LOMBO_SHRINK_DRY_RUN=false`, run `lombo-shrink run`. Watch the lines:
`shipping to the server...`, then `swapped into the library`. Play the title in Jellyfin. Only then raise the limit.
Keep `LOMBO_SHRINK_KEEP_ORIGINALS=true` on the server for the first runs: originals go to a `.lombo-shrink-holding` folder
inside the library (same disk) that nothing clears; delete it yourself when you are happy.

Close the session when done: `lombo-shrink session close`.

A dry run of the server side at any time: `lombo-shrink swap --manifest M --result R` (no `--apply`).

## Without shipping

Leave `SHIP_TARGET` empty. Converted files stay in `OUT_DIR`; `lombo-shrink review` lists them. Once you set up the server
side, the next `run` ships the files that are already converted (no re-encode); or copy files into the library
yourself after checking them.
