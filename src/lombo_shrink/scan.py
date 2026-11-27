"""The candidate list: every video under the library that would be worth converting, biggest first.

This replaces the private admin panel the original read its list from. It only reads the library.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core, media
from .config import Config

SKIP_DIRS = {"convert-holding", "lombo-shrink-holding"}


def skipped_paths(path: str) -> tuple[str, ...]:
    """The skip file: one library path or folder per line, blank lines and # comments ignored. A file that is set
    but cannot be read stops the scan: converting what was meant to be left out is the wrong guess."""
    if not path:
        return ()
    with open(path, encoding="utf-8-sig") as f:
        lines = (line.strip().replace("\\", "/").strip("/") for line in f)
        return tuple(line for line in lines if line and not line.startswith("#"))


def listed(rel: str, entries: tuple[str, ...]) -> bool:
    """Is this file one of the entries, or inside one of those folders?"""
    return any(rel == e or rel.startswith(e + "/") for e in entries)


def scan(cfg: Config, progress: Callable[[str], None] | None = None, *, verbose: bool = False) -> list[dict[str, Any]]:
    """Walk the library, probe each video and keep the ones `core.skip_reason` lets through."""
    root = Path(cfg.library_dir)
    found: list[dict[str, Any]] = []
    left_out = 0
    skip = skipped_paths(cfg.skip_file)
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(dirpath) / name
            if not name.lower().endswith(core.VIDEO_EXTENSIONS) or "sample" in name.lower() or name.startswith("."):
                continue
            if path.is_symlink() or listed(path.relative_to(root).as_posix(), skip):
                continue
            st = path.stat()
            if cfg.skip_hardlinked and st.st_nlink >= 2:
                continue  # a torrent still seeds it: replacing the name would store the data twice
            info = media.probe(cfg.ffprobe, str(path))
            if info is None:
                if progress:
                    progress(f"unreadable, left out: {path}")
                continue
            reason = core.skip_reason(info, st.st_size, allow_hevc=cfg.allow_hevc, min_bpp=cfg.min_bpp)
            if reason:
                left_out += 1
                if verbose and progress:
                    progress(f"left out: {path.relative_to(root).as_posix()} ({reason})")
                continue
            video = core.real_video(info)[0]
            found.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": st.st_size,
                    "codec": video.get("codec_name"),
                    "title": path.stem,
                }
            )
    if left_out and not verbose and progress:
        progress(f"{left_out} files left out (run with -v to see why)")
    return core.sort_candidates(found)
