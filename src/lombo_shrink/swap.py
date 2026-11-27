"""Server side: put a converted file into the library in place of the original, safely.

This is the dangerous part, so every step is a check and nothing is removed until the new file is verified
in place. Called through `lombo-shrink swap` (the PC runs it over ssh), one manifest at a time.

Per entry, in order (any failure leaves the library untouched for that entry; the next entry still runs):
 1. a conversion session is open (--apply only; see REQUIRE_SESSION)
 2. the staged file exists, is a regular file, and its size and sha256 match the manifest
 3. the original exists, has exactly `src_bytes` and the same modified time (within 2 s) as when converted
 4. if ffprobe is available, the staged file's first video stream is hevc
 5. the disk holding the original has room for the new file plus a margin
 6. the new data is copied next to the original (same folder, so the same disk), checked by sha256 again,
    given the original's mode, owner and modified time, then renamed into place
 7. the original is removed, or with KEEP_ORIGINALS moved to a holding folder that nothing clears
 8. only then is the staging copy removed
Nothing derived from the manifest is ever removed recursively: single files and empty folders only.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import core, media
from .config import Config
from .core import Entry

HOLDING_DIR = ".lombo-shrink-holding"


@dataclass
class SwapResult:
    path: str
    status: str  # swapped, skipped, failed
    reason: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "swapped" or (self.status == "skipped" and self.reason == "already swapped")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _within(path: Path, root: Path) -> bool:
    real_root = root.resolve()
    real = path.resolve()
    return real == real_root or real_root in real.parents


def _find_branch(roots: list[Path], rel: str) -> tuple[Path | None, str]:
    hits = [r for r in roots if (r / rel).is_file() or (r / rel).is_symlink()]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, f"the file exists on {len(hits)} disks (resolve the duplicate first)"
    return None, "no library folder holds the file"


def _cleanup_staged(stage: Path, staged: str) -> None:
    target = stage / staged
    target.unlink(missing_ok=True)
    parent = target.parent
    while parent != stage and stage in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def swap_one(cfg: Config, e: Entry, *, apply: bool) -> SwapResult:
    r = SwapResult(e.path, "failed")
    stage = Path(cfg.stage_dir)
    roots = [Path(b) for b in cfg.branches] or [Path(cfg.library_dir)]
    final_rel = core.final_relative(e.path)

    def fail(reason: str) -> SwapResult:
        r.status, r.reason = "failed", reason
        return r

    def note(text: str) -> None:
        r.note = f"{r.note}; {text}" if r.note else text

    branch, why = _find_branch(roots, e.path)
    if branch is None and final_rel != e.path:
        branch, _ = _find_branch(roots, final_rel)
    if branch is None:
        return fail(why)
    orig, final = branch / e.path, branch / final_rel
    for f in (orig, final):
        if f.is_symlink():
            return fail("refusing a symlink in the library")
    if not _within(final.parent, branch):
        return fail("folder is outside the library")

    sfile = stage / e.staged
    if not sfile.exists():
        if final.is_file() and final.stat().st_size == e.out_bytes and sha256_of(final) == e.sha256:
            r.status, r.reason = "skipped", "already swapped"
            return r
        return fail("staged file is missing")
    if sfile.is_symlink() or not sfile.is_file() or not _within(sfile, stage):
        return fail("staged path is not a regular file inside the staging folder")
    if sfile.stat().st_size != e.out_bytes:
        return fail("staged size differs from the manifest (out_bytes)")
    if sha256_of(sfile) != e.sha256:
        return fail("staged sha256 differs from the manifest")

    if final.is_file() and final.stat().st_size == e.out_bytes and sha256_of(final) == e.sha256:
        if apply:
            _cleanup_staged(stage, e.staged)
            note("new file was already in place; staging cleaned up")
        else:
            note("new file already in place; apply would only clean up staging")
        r.status = "swapped"
        return r

    if not orig.is_file():
        return fail("original is missing from the library")
    ost = orig.stat()
    if ost.st_size != e.src_bytes:
        return fail("original changed: size differs from src_bytes")
    if not core.mtime_matches(ost.st_mtime, e.src_mtime):
        return fail("original changed: modified time differs from src_mtime")
    if e.src_mtime is None:
        note("no src_mtime in the manifest; modified-time check skipped")
    if final != orig and final.exists():
        return fail(f"a different {final.name} already exists next to the original")

    ffprobe = cfg.ffprobe
    if shutil.which(ffprobe) or Path(ffprobe).is_file():
        info = media.probe(ffprobe, str(sfile))
        videos = core.real_video(info) if info else []
        codec = videos[0].get("codec_name") if videos else None
        if codec != "hevc":
            return fail(f"staged first video stream is '{codec or 'unknown'}', not hevc")
    else:
        note("no ffprobe on this host: codec not re-checked (the PC verified decode)")

    margin = max(cfg.margin_gb, dict(cfg.branch_margin_gb).get(str(branch).rstrip("/"), 0))
    if not core.room_ok(shutil.disk_usage(final.parent).free, e.out_bytes, int(margin * 1024**3)):
        return fail("not enough free space on the disk holding the original")

    linked = ost.st_nlink >= 2
    if not apply:
        if linked:
            note("original still hard-linked by a torrent: space is freed when the torrent is removed")
        if final != orig:
            note(f"original is not .mkv: it would be replaced by {final.name}; Sonarr/Radarr must rescan")
        note("dry run: nothing changed")
        r.status = "swapped"
        return r

    tmp = final.parent / f".{final.name}.lombo-shrink-part"
    tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(sfile, tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        if tmp.stat().st_size != e.out_bytes or sha256_of(tmp) != e.sha256:
            tmp.unlink(missing_ok=True)
            return fail("copy on the library disk did not match (sha256)")
        os.chmod(tmp, stat.S_IMODE(ost.st_mode))
        try:
            os.chown(tmp, ost.st_uid, ost.st_gid)
        except OSError:
            note("could not copy the owner (not root)")
        os.utime(tmp, ns=(ost.st_atime_ns, ost.st_mtime_ns))
    except OSError as err:
        tmp.unlink(missing_ok=True)
        return fail(f"copy to the library disk failed: {err}")

    hold: Path | None = None
    if cfg.keep_originals:
        # Inside the branch, so it is always the same filesystem as the file (a rename never crosses devices,
        # even when the library is its own mount). The dot name keeps scans and media servers away from it.
        hold = branch / HOLDING_DIR / e.path
        if hold.exists():
            hold = hold.with_name(f"{hold.name}.{int(time.time())}")
        try:
            hold.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            tmp.unlink(missing_ok=True)
            return fail("could not create the holding folder")

    # shortcut: the original is re-checked just before the rename, but a change in the few microseconds between
    # that check and the rename is not caught (no file locking on shared library mounts).
    try:
        now = orig.stat()
        if now.st_size != ost.st_size or now.st_mtime_ns != ost.st_mtime_ns:
            tmp.unlink(missing_ok=True)
            return fail("original changed while the copy was made (size or modified time moved)")
    except OSError as err:
        tmp.unlink(missing_ok=True)
        return fail(f"original could not be re-checked: {err}")

    try:
        if final == orig:
            if hold:
                os.replace(orig, hold)
                try:
                    os.replace(tmp, final)
                except OSError:
                    os.replace(hold, orig)
                    raise
            else:
                os.replace(tmp, final)
        else:
            os.replace(tmp, final)
            try:
                if hold:
                    os.replace(orig, hold)
                else:
                    orig.unlink()
            except OSError:
                note(f"new file is in place but the old {orig.name} could not be removed")
            note("container changed to .mkv: Sonarr/Radarr must rescan")
    except OSError as err:
        tmp.unlink(missing_ok=True)
        return fail(f"could not put the new file in place (original restored where possible): {err}")

    if linked:
        note("original still hard-linked by a torrent: space is freed when the torrent is removed")
    if hold:
        note(f"original kept in {hold} (nothing clears it automatically)")
    _cleanup_staged(stage, e.staged)
    r.status = "swapped"
    return r


def _post_swap(cfg: Config, paths: list[str]) -> str | None:
    """Run the optional post-swap command once. Returns a short problem text, or None when it ran fine or is unset."""
    if not cfg.post_swap_command:
        return None
    env = {**os.environ, "LOMBO_SHRINK_SWAPPED_JSON": json.dumps(paths)}
    try:
        done = subprocess.run(  # noqa: S603 - no shell; the command is the owner's own setting
            list(cfg.post_swap_command),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"post-swap command failed ({type(err).__name__})"
    return None if done.returncode == 0 else f"post-swap command failed (exit {done.returncode})"


def run_swap(
    cfg: Config,
    entries: list[Entry],
    *,
    apply: bool,
    session_open: bool,
    refresh: Callable[[], None] | None = None,
) -> list[SwapResult]:
    results: list[SwapResult] = []
    for e in entries:
        if apply and cfg.require_session and not session_open:
            results.append(SwapResult(e.path, "failed", "no open conversion session (run `lombo-shrink session open`)"))
        else:
            results.append(swap_one(cfg, e, apply=apply))
    if apply and refresh and any(r.status == "swapped" for r in results):
        try:
            refresh()
        except Exception as err:  # noqa: BLE001 - best effort: a refresh problem never fails a swap
            for r in results:
                if r.status == "swapped":
                    r.note = (
                        f"{r.note}; library refresh failed ({err})" if r.note else f"library refresh failed ({err})"
                    )
    swapped = [r for r in results if r.status == "swapped"]
    if apply and swapped:
        problem = _post_swap(cfg, [r.path for r in swapped])
        if problem:
            for r in swapped:
                r.note = f"{r.note}; {problem}" if r.note else problem
    return results
