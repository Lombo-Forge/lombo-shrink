---
name: install-into-my-stack
description: Help a user set up LomboShrink on their main PC and their media server, then verify with doctor. Use when someone asks to install it, set it up, or start converting their library to H.265.
---

# Install LomboShrink into the user's setup

Ask one question at a time and use the answers; never assume a layout.

1. **Which machine has the discrete graphics card, and what is it?** NVIDIA (`nvenc`), AMD (`amf`), Intel
   (`qsv`), or a Mac (`videotoolbox`). Say plainly that only NVIDIA settings were measured; the others are
   unverified and `doctor` will say whether the encoder works.
2. **Does that machine have Python 3.12+ and an ffmpeg that lists the encoder** (`ffmpeg -encoders`)? If not,
   help them get one (their choice of build). Then `pip install .`.
3. **Where is the library, as the PC sees it?** A mapped drive or mount, read-only is best. That is
   `LOMBO_SHRINK_LIBRARY_DIR`. Where should converted files go (a fast local drive with space)? `OUT_DIR`.
4. **Write `.env`** from `.env.example` (git-ignored; never print secrets). Keep `DRY_RUN=true`.
5. **Run `lombo-shrink doctor`**, then `lombo-shrink plan`. Walk through the list with them.
6. **First real file:** `LIMIT=1`, `DRY_RUN=false`, `lombo-shrink run`. Have them play the result.
7. **Swapping into the server's library is a separate, optional step.** Ask: is it mergerfs (then set `BRANCHES`)?
   Do they have key-only ssh to the server? Follow `docs/setup.md`: install on the server, `DRY_RUN=false` there,
   `session open`, `SHIP_TARGET` on the PC, `KEEP_ORIGINALS=true` for the first runs, one file, check in Jellyfin.
8. Never run the swap against their real library on your own: only when they say so, with their session open.

If `doctor` fails, read the `troubleshoot` skill.
