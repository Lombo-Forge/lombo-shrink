"""The swap replaces real files, so every refusal and every success path is tested here, in tmp_path only."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import make_bins, unix_only, video_probe
from lombo_shrink import core, swap
from lombo_shrink.cli import main
from lombo_shrink.config import Config, from_env

SRC = b"o" * 100
NEW = b"n" * 40
MTIME = 1_700_000_000


class Setup:
    def __init__(self, tmp_path: Path, **extra: str):
        self.lib = tmp_path / "lib"
        self.stage = tmp_path / "stage"
        (self.lib / "m").mkdir(parents=True)
        self.orig = self.lib / "m" / "Foo.mkv"
        self.orig.write_bytes(SRC)
        os.utime(self.orig, (MTIME, MTIME))
        (self.stage / "m").mkdir(parents=True)
        self.staged = self.stage / "m" / "Foo.hevc.mkv"
        self.staged.write_bytes(NEW)
        self.tmp = tmp_path
        self.env = {
            "LOMBO_SHRINK_LIBRARY_DIR": str(self.lib),
            "LOMBO_SHRINK_STAGE_DIR": str(self.stage),
            "LOMBO_SHRINK_FFPROBE": "/no/such/ffprobe",
            "LOMBO_SHRINK_MARGIN_GB": "0",
            "LOMBO_SHRINK_DRY_RUN": "false",
            "LOMBO_SHRINK_SESSION_FILE": str(tmp_path / "session.json"),
            **extra,
        }

    def cfg(self) -> Config:
        return from_env(self.env)

    def entry(self, **kw: object) -> core.Entry:
        data = {
            "path": "m/Foo.mkv",
            "staged": "m/Foo.hevc.mkv",
            "sha256": hashlib.sha256(NEW).hexdigest(),
            "out_bytes": len(NEW),
            "src_bytes": len(SRC),
            "src_mtime": MTIME,
            **kw,
        }
        return core.parse_manifest([data])[0]


@pytest.fixture
def s(tmp_path):
    return Setup(tmp_path)


def test_dry_run_checks_everything_and_changes_nothing(s):
    r = swap.swap_one(s.cfg(), s.entry(), apply=False)
    assert (r.status, "dry run" in r.note) == ("swapped", True)
    assert s.orig.read_bytes() == SRC
    assert s.staged.exists()


def test_apply_replaces_the_original_and_keeps_mode_and_time(s):
    s.orig.chmod(0o640)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert r.status == "swapped", r
    assert s.orig.read_bytes() == NEW
    assert s.orig.stat().st_mode & 0o777 == 0o640
    assert int(s.orig.stat().st_mtime) == MTIME
    assert not s.staged.exists()
    assert not (s.stage / "m").exists()  # empty staging folders are tidied
    assert [p.name for p in (s.lib / "m").iterdir()] == ["Foo.mkv"]  # no temp file left


def test_repeat_after_a_lost_answer_is_already_swapped(s):
    swap.swap_one(s.cfg(), s.entry(), apply=True)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert (r.status, r.reason, r.ok) == ("skipped", "already swapped", True)


def test_crash_after_copy_is_finished_safely(s):
    (s.lib / "m" / "Foo.mkv").write_bytes(NEW)  # the new file is already in place
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert r.status == "swapped"
    assert "already in place" in r.note
    assert not s.staged.exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"sha256": "0" * 64}, "staged sha256 differs"),
        ({"out_bytes": 41}, "staged size differs"),
        ({"src_bytes": 99}, "size differs from src_bytes"),
        ({"src_mtime": MTIME + 60}, "modified time differs"),
        ({"path": "m/Nope.mkv"}, "no library folder holds the file"),
    ],
)
def test_refusals_leave_the_library_untouched(s, change, reason):
    r = swap.swap_one(s.cfg(), s.entry(**change), apply=True)
    assert r.status == "failed"
    assert reason in r.reason
    assert s.orig.read_bytes() == SRC
    assert s.staged.exists()


def test_missing_staged_file_and_missing_original(s):
    s.staged.unlink()
    assert swap.swap_one(s.cfg(), s.entry(), apply=True).reason == "staged file is missing"
    s.staged.write_bytes(NEW)
    s.orig.rename(s.lib / "m" / "Other.mkv")
    # the original is gone but the folder has a different file: nothing holds Foo.mkv
    assert "no library folder holds" in swap.swap_one(s.cfg(), s.entry(), apply=True).reason


def test_symlinks_are_refused(s):
    s.orig.unlink()
    target = s.tmp / "elsewhere.mkv"
    target.write_bytes(SRC)
    s.orig.symlink_to(target)
    assert swap.swap_one(s.cfg(), s.entry(), apply=True).reason == "refusing a symlink in the library"
    assert target.read_bytes() == SRC
    s.orig.unlink()
    s.orig.write_bytes(SRC)
    os.utime(s.orig, (MTIME, MTIME))
    s.staged.unlink()
    outside = s.tmp / "outside.mkv"
    outside.write_bytes(NEW)
    s.staged.symlink_to(outside)
    assert "not a regular file" in swap.swap_one(s.cfg(), s.entry(), apply=True).reason
    assert outside.exists()


def test_folder_symlinked_out_of_the_library_is_refused(s):
    other = s.tmp / "other"
    other.mkdir()
    (other / "Foo.mkv").write_bytes(SRC)
    os.utime(other / "Foo.mkv", (MTIME, MTIME))
    (s.lib / "link").symlink_to(other)
    entry = s.entry(path="link/Foo.mkv")
    assert swap.swap_one(s.cfg(), entry, apply=True).reason == "folder is outside the library"
    assert (other / "Foo.mkv").read_bytes() == SRC


def test_not_enough_room(s):
    s.env["LOMBO_SHRINK_MARGIN_GB"] = "100000"
    assert (
        swap.swap_one(s.cfg(), s.entry(), apply=True).reason == "not enough free space on the disk holding the original"
    )
    assert s.orig.read_bytes() == SRC


def test_mp4_original_becomes_mkv(s):
    mp4 = s.lib / "m" / "Foo.mp4"
    s.orig.rename(mp4)
    r = swap.swap_one(s.cfg(), s.entry(path="m/Foo.mp4"), apply=True)
    assert r.status == "swapped"
    assert "container changed" in r.note
    assert not mp4.exists()
    assert (s.lib / "m" / "Foo.mkv").read_bytes() == NEW


def test_mp4_with_a_different_mkv_already_there_is_refused(s):
    mp4 = s.lib / "m" / "Foo.mp4"
    mp4.write_bytes(SRC)
    os.utime(mp4, (MTIME, MTIME))
    r = swap.swap_one(s.cfg(), s.entry(path="m/Foo.mp4"), apply=True)
    assert "already exists next to the original" in r.reason
    assert s.orig.read_bytes() == SRC


def test_keep_originals_moves_them_to_the_holding_folder(tmp_path):
    s = Setup(tmp_path, LOMBO_SHRINK_KEEP_ORIGINALS="true")
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    held = tmp_path / "lib" / ".lombo-shrink-holding" / "m" / "Foo.mkv"
    assert r.status == "swapped"
    assert held.read_bytes() == SRC
    assert s.orig.read_bytes() == NEW
    assert "kept in" in r.note
    # a second original with the same name does not overwrite the first
    s.orig.write_bytes(SRC)
    os.utime(s.orig, (MTIME, MTIME))
    s.staged.parent.mkdir(parents=True, exist_ok=True)
    s.staged.write_bytes(NEW)
    swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert len(list(held.parent.iterdir())) == 2


def test_keep_originals_never_crosses_a_mount(tmp_path, monkeypatch):
    """A library that is its own mount: a rename out of it fails with EXDEV, so the held copy must stay inside."""
    s = Setup(tmp_path, LOMBO_SHRINK_KEEP_ORIGINALS="true")
    real_replace = os.replace

    def replace(a, b):
        if Path(a).is_relative_to(s.lib) != Path(b).is_relative_to(s.lib):
            raise OSError(18, "Invalid cross-device link")
        real_replace(a, b)

    monkeypatch.setattr(os, "replace", replace)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert r.status == "swapped", r
    assert (s.lib / swap.HOLDING_DIR / "m" / "Foo.mkv").read_bytes() == SRC
    assert s.orig.read_bytes() == NEW


def test_original_changed_during_the_copy_is_not_overwritten(s, monkeypatch):
    real_copy = swap.shutil.copyfile

    def copy_then_upgrade(src, dst):
        real_copy(src, dst)
        s.orig.write_bytes(b"upgraded by sonarr")

    monkeypatch.setattr(swap.shutil, "copyfile", copy_then_upgrade)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert (r.status, "changed while the copy" in r.reason) == ("failed", True)
    assert s.orig.read_bytes() == b"upgraded by sonarr"
    assert [p.name for p in s.orig.parent.iterdir()] == ["Foo.mkv"]  # temp file removed


def test_original_vanishing_during_the_copy_fails_cleanly(s, monkeypatch):
    real_copy = swap.shutil.copyfile

    def copy_then_delete(src, dst):
        real_copy(src, dst)
        s.orig.unlink()

    monkeypatch.setattr(swap.shutil, "copyfile", copy_then_delete)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert (r.status, "could not be re-checked" in r.reason) == ("failed", True)
    assert not list(s.orig.parent.iterdir())


def test_hard_linked_original_is_replaced_by_name_only(s):
    seed = s.tmp / "seed.mkv"
    os.link(s.orig, seed)
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert r.status == "swapped"
    assert "hard-linked" in r.note
    assert seed.read_bytes() == SRC  # the torrent's data is untouched
    assert s.orig.read_bytes() == NEW


def test_the_file_on_two_disks_is_refused(tmp_path):
    s = Setup(tmp_path)
    disk2 = tmp_path / "disk2"
    (disk2 / "m").mkdir(parents=True)
    (disk2 / "m" / "Foo.mkv").write_bytes(SRC)
    s.env["LOMBO_SHRINK_BRANCHES"] = f"{s.lib}:{disk2}"
    r = swap.swap_one(s.cfg(), s.entry(), apply=True)
    assert "exists on 2 disks" in r.reason


def test_the_new_file_goes_on_the_same_disk_as_the_original(tmp_path):
    s = Setup(tmp_path)
    disk2 = tmp_path / "disk2"
    (disk2 / "m").mkdir(parents=True)
    other = disk2 / "m" / "Bar.mkv"
    other.write_bytes(SRC)
    os.utime(other, (MTIME, MTIME))
    s.env["LOMBO_SHRINK_BRANCHES"] = f"{s.lib}:{disk2}"
    r = swap.swap_one(s.cfg(), s.entry(path="m/Bar.mkv"), apply=True)
    assert r.status == "swapped"
    assert other.read_bytes() == NEW
    assert not (s.lib / "m" / "Bar.mkv").exists()


@unix_only
def test_codec_is_rechecked_when_ffprobe_exists(tmp_path):
    s = Setup(tmp_path)
    _, h264_probe = make_bins(tmp_path / "bin1", video_probe(), video_probe())
    s.env["LOMBO_SHRINK_FFPROBE"] = h264_probe
    assert "not hevc" in swap.swap_one(s.cfg(), s.entry(), apply=True).reason
    _, hevc_probe = make_bins(tmp_path / "bin2", video_probe("hevc"), video_probe("hevc"))
    s.env["LOMBO_SHRINK_FFPROBE"] = hevc_probe
    assert swap.swap_one(s.cfg(), s.entry(), apply=True).status == "swapped"


def test_run_swap_needs_a_session_and_refreshes_jellyfin(s):
    entries = [s.entry()]
    refreshed = []
    r = swap.run_swap(s.cfg(), entries, apply=True, session_open=False, refresh=lambda: refreshed.append(1))
    assert "no open conversion session" in r[0].reason
    assert s.orig.read_bytes() == SRC
    r = swap.run_swap(s.cfg(), entries, apply=True, session_open=True, refresh=lambda: refreshed.append(1))
    assert r[0].status == "swapped"
    assert refreshed == [1]


def test_a_failing_refresh_is_a_note_not_a_failed_swap(s):
    def boom() -> None:
        raise RuntimeError("down")

    r = swap.run_swap(s.cfg(), [s.entry()], apply=True, session_open=True, refresh=boom)
    assert r[0].status == "swapped"
    assert "refresh failed (down)" in r[0].note


def test_post_swap_command_runs_once_with_the_swapped_paths(s, tmp_path):
    out = tmp_path / "hook.out"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\nprintf %s "$LOMBO_SHRINK_SWAPPED_JSON" > {out}\n')
    script.chmod(0o755)
    s.env["LOMBO_SHRINK_POST_SWAP_COMMAND"] = str(script)
    entries = [s.entry()]
    swap.run_swap(s.cfg(), entries, apply=False, session_open=True)
    assert not out.exists()  # a dry run never starts it
    r = swap.run_swap(s.cfg(), entries, apply=True, session_open=True)
    assert r[0].status == "swapped"
    assert "post-swap" not in r[0].note
    assert json.loads(out.read_text()) == [entries[0].path]


def test_a_failing_post_swap_command_is_a_note_not_a_failed_swap(s, tmp_path):
    script = tmp_path / "bad.sh"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(0o755)
    s.env["LOMBO_SHRINK_POST_SWAP_COMMAND"] = str(script)
    r = swap.run_swap(s.cfg(), [s.entry()], apply=True, session_open=True)
    assert r[0].status == "swapped"
    assert "post-swap command failed (exit 3)" in r[0].note
    s.env["LOMBO_SHRINK_POST_SWAP_COMMAND"] = str(tmp_path / "missing")
    assert swap._post_swap(s.cfg(), ["/x"]) == "post-swap command failed (FileNotFoundError)"


def write_manifest(s: Setup, **kw: object) -> Path:
    m = s.tmp / "manifest.json"
    m.write_text(
        json.dumps(
            [
                {
                    "path": "m/Foo.mkv",
                    "staged": "m/Foo.hevc.mkv",
                    "sha256": hashlib.sha256(NEW).hexdigest(),
                    "out_bytes": len(NEW),
                    "src_bytes": len(SRC),
                    "src_mtime": MTIME,
                    **kw,
                }
            ]
        )
    )
    return m


def test_cli_swap_dry_by_default_and_apply_needs_both_switches(tmp_path, capsys):
    s = Setup(tmp_path)
    m, result = write_manifest(s), tmp_path / "result.json"
    args = ["swap", "--manifest", str(m), "--result", str(result)]
    assert main(args, s.env) == 0  # dry run needs no session
    assert json.loads(result.read_text())["dry_run"] is True
    assert s.orig.read_bytes() == SRC
    # --apply while DRY_RUN is true on this machine: refused
    assert main([*args, "--apply"], {**s.env, "LOMBO_SHRINK_DRY_RUN": "true"}) == 1
    assert "Refusing to apply" in capsys.readouterr().err
    assert s.orig.read_bytes() == SRC
    # --apply without an open session: refused per file
    assert main([*args, "--apply"], s.env) == 1
    assert json.loads(result.read_text())["files"][0]["status"] == "failed"
    assert s.orig.read_bytes() == SRC
    # with a session it swaps
    assert main(["session", "open", "1"], s.env) == 0
    assert main([*args, "--apply"], s.env) == 0
    assert s.orig.read_bytes() == NEW
    assert json.loads(result.read_text())["files"][0]["status"] == "swapped"


def test_cli_swap_rejects_a_bad_manifest(tmp_path, capsys):
    s = Setup(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text('[{"path": "../../etc/passwd"}]')
    assert main(["swap", "--manifest", str(bad), "--result", str(tmp_path / "r.json")], s.env) == 2
    assert "unsafe" in capsys.readouterr().err
    assert main(["swap", "--manifest", str(tmp_path / "missing.json"), "--result", "r"], s.env) == 2
    no_lib = {k: v for k, v in s.env.items() if k != "LOMBO_SHRINK_LIBRARY_DIR"}
    assert main(["swap", "--manifest", str(write_manifest(s)), "--result", "r"], no_lib) == 2
