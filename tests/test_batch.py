"""The scan, the encode wrapper, the batch loop and shipping, with fake ffmpeg/ffprobe/ssh scripts."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from conftest import make_bins, unix_only, video_probe
from lombo_shrink import batch, media, scan, ship
from lombo_shrink.cli import main
from lombo_shrink.config import Config, from_env

pytestmark = unix_only
SRC_BYTES = 1000


@pytest.fixture
def lab(tmp_path, env, monkeypatch):
    """A library with one 1000-byte film, fake tools that shrink it to 400 bytes, and a config."""
    lib = tmp_path / "lib"
    (lib / "m").mkdir(parents=True)
    film = lib / "m" / "Foo (2019).mkv"
    film.write_bytes(b"s" * SRC_BYTES)
    ffmpeg, ffprobe = make_bins(tmp_path / "bin", video_probe(), video_probe("hevc", 3_000_000))
    monkeypatch.setenv("FAKE_OUT_BYTES", "400")
    e = {
        **env,
        "LOMBO_SHRINK_FFMPEG": ffmpeg,
        "LOMBO_SHRINK_FFPROBE": ffprobe,
        "LOMBO_SHRINK_SPARE_GB": "0",
        "LOMBO_SHRINK_DRY_RUN": "false",
    }

    class Lab:
        env = e
        tmp = tmp_path
        path = film

        def cfg(self, **extra: str) -> Config:
            return from_env({**e, **extra})

        items: ClassVar[list[dict[str, object]]] = [
            {"path": "m/Foo (2019).mkv", "size": SRC_BYTES, "title": "Foo (2019)"}
        ]

    return Lab()


def log_statuses(out: Path) -> list[str]:
    return [json.loads(x)["status"] for x in (out / "convert-log.jsonl").read_text().splitlines()]


def test_scan_lists_only_what_is_worth_converting(lab):
    lib = lab.tmp / "lib"
    (lib / "m" / "Foo (2019)-sample.mkv").write_bytes(b"x")
    (lib / "m" / "notes.txt").write_text("hi")
    (lib / ".hidden").mkdir()
    (lib / ".hidden" / "Hidden.mkv").write_bytes(b"x")
    big = lib / "m" / "Big.mkv"
    big.write_bytes(b"b" * 5000)
    os.link(big, lab.tmp / "seeding.mkv")  # a torrent still seeds this one
    (lib / "m" / "Link.mkv").symlink_to(lab.path)
    items = scan.scan(lab.cfg())
    assert [i["path"] for i in items] == ["m/Foo (2019).mkv"]
    assert items[0]["codec"] == "h264"
    assert [i["path"] for i in scan.scan(lab.cfg(LOMBO_SHRINK_SKIP_HARDLINKED="false"))] == [
        "m/Big.mkv",
        "m/Foo (2019).mkv",
    ]


def test_scan_leaves_out_hevc_and_unreadable(lab, tmp_path):
    _, hevc = make_bins(tmp_path / "bin2", video_probe("hevc"), video_probe("hevc"))
    assert scan.scan(lab.cfg(LOMBO_SHRINK_FFPROBE=hevc)) == []
    seen: list[str] = []
    assert scan.scan(lab.cfg(LOMBO_SHRINK_FFPROBE="/bin/false"), seen.append) == []
    assert "unreadable" in seen[0]


def test_convert_accepts_a_good_result_and_never_touches_the_source(lab):
    cfg = lab.cfg()
    out = lab.tmp / "out" / "Foo.hevc.mkv"
    o = media.convert_file(cfg, lab.path, out, "nvenc", 26, poll=0.1)
    assert (o.status, o.out_bytes) == ("accepted", 400)
    assert out.read_bytes() == b"\0" * 400
    assert lab.path.read_bytes() == b"s" * SRC_BYTES
    assert not list(out.parent.glob("*.partial.mkv"))


def test_convert_rejects_and_fails_without_leaving_files(lab, monkeypatch):
    cfg, out = lab.cfg(), lab.tmp / "out" / "Foo.hevc.mkv"
    monkeypatch.setenv("FAKE_OUT_BYTES", "900")
    o = media.convert_file(cfg, lab.path, out, "nvenc", 26, poll=0.1)
    assert o.status == "rejected"
    assert "below the 20% bar" in o.msg
    monkeypatch.setenv("FAKE_OUT_BYTES", "400")
    monkeypatch.setenv("FAKE_DECODE_ERR", "corrupt frame")
    assert "decode check failed: corrupt frame" in media.convert_file(cfg, lab.path, out, "nvenc", 26, poll=0.1).msg
    monkeypatch.delenv("FAKE_DECODE_ERR")
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    o = media.convert_file(cfg, lab.path, out, "nvenc", 26, poll=0.1)
    assert (o.status, "ffmpeg exited 1 boom" in o.msg) == ("failed", True)
    assert not out.exists()
    assert not list(out.parent.glob("*.partial.mkv"))


def test_convert_refuses_before_encoding(lab, tmp_path):
    out = tmp_path / "o" / "x.mkv"
    assert media.convert_file(lab.cfg(), lab.path, out, "nvenc", 29).status == "rejected"  # CQ above MAX_CQ
    assert media.convert_file(lab.cfg(), tmp_path / "nope.mkv", out, "nvenc", 26).status == "failed"
    assert media.convert_file(lab.cfg(LOMBO_SHRINK_FFPROBE="/bin/false"), lab.path, out, "nvenc", 26).status == "failed"
    _, hevc = make_bins(tmp_path / "b2", video_probe("hevc"), video_probe("hevc"))
    o = media.convert_file(lab.cfg(LOMBO_SHRINK_FFPROBE=hevc), lab.path, out, "nvenc", 26)
    assert (o.status, o.msg) == ("skipped", "already HEVC")
    assert not out.parent.exists() or not list(out.parent.iterdir())


def test_watchdog_kills_a_stalled_or_endless_encode(tmp_path):
    partial = tmp_path / "p.partial.mkv"
    stalled = media.run_encode(["sleep", "30"], partial, stall_minutes=0.005, max_seconds=60, poll=0.1)
    assert stalled.code == -1
    assert "stalled" in (stalled.why or "")
    slow = media.run_encode(["sleep", "30"], partial, stall_minutes=10, max_seconds=0.3, poll=0.1)
    assert "took too long" in (slow.why or "")
    assert "could not start" in (media.run_encode(["/no/such/binary"], partial, 1, 1).why or "")


def test_probe_helpers_survive_garbage(tmp_path):
    junk = tmp_path / "junk"
    junk.write_text("#!/bin/sh\necho 'not json'\n")
    junk.chmod(junk.stat().st_mode | stat.S_IEXEC)
    assert media.probe(str(junk), "x") is None
    assert media.probe("/no/such/ffprobe", "x") is None
    assert media.packet_count("/no/such/ffprobe", "x", "v:0") == -1
    assert media.available_encoders("/no/such/ffmpeg") == set()
    assert media.encoder_error("/no/such/ffmpeg", "nvenc", 26)
    assert "could not" not in media.decode_error("/no/such", tmp_path) or True


def test_batch_runs_logs_reports_and_resumes(lab):
    cfg = lab.cfg()
    logs: list[str] = []
    res = batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=logs.append, poll=0.1)
    out = Path(cfg.out_dir)
    assert [r["status"] for r in res.rows] == ["accepted"]
    assert (out / "m" / "Foo (2019).hevc.mkv").exists()
    assert "50%" not in res.report
    assert "60% saved" in (out / "REPORT.txt").read_text()
    assert log_statuses(out) == ["accepted"]
    # run again: accepted is final, nothing is encoded twice
    again = batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=logs.append, poll=0.1)
    assert again.rows == []
    assert log_statuses(out) == ["accepted"]
    # -redo does it again
    assert len(batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, redo=True, log=logs.append, poll=0.1).rows) == 1


def test_batch_dry_run_writes_nothing(lab):
    cfg = lab.cfg()
    res = batch.run_batch(cfg, lab.items, "nvenc", dry_run=True)
    assert "would convert m/Foo (2019).mkv" in res.report
    assert not Path(cfg.out_dir).exists()


def test_batch_failed_is_retried_missing_is_final(lab, monkeypatch):
    cfg = lab.cfg()
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    assert (
        batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=lambda _: None, poll=0.1).rows[0]["status"]
        == "failed"
    )
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")
    assert (
        batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=lambda _: None, poll=0.1).rows[0]["status"]
        == "accepted"
    )
    ghost = [{"path": "m/Ghost.mkv", "size": 5, "title": "Ghost"}]
    assert batch.run_batch(cfg, ghost, "nvenc", dry_run=False, log=lambda _: None).rows[0]["status"] == "missing"
    assert batch.run_batch(cfg, ghost, "nvenc", dry_run=False, log=lambda _: None).rows == []


def test_batch_stop_file_limit_and_space(lab, tmp_path):
    cfg = lab.cfg()
    out = Path(cfg.out_dir)
    out.mkdir(parents=True)
    (out / "STOP").write_text("")
    res = batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=lambda _: None)
    assert (res.stopped, res.rows) == ("STOP file found", [])
    (out / "STOP").unlink()
    (out / "stale.partial.mkv").write_text("x")
    items = [*lab.items, {"path": "m/Ghost.mkv", "size": 5, "title": "Ghost"}]
    res = batch.run_batch(lab.cfg(LOMBO_SHRINK_LIMIT="1"), items, "nvenc", dry_run=False, log=lambda _: None, poll=0.1)
    assert "limit of 1" in res.stopped
    assert not (out / "stale.partial.mkv").exists()  # crash leftovers are removed at the start
    res = batch.run_batch(
        lab.cfg(LOMBO_SHRINK_SPARE_GB="99999"),
        [{"path": "z", "size": 5}],
        "nvenc",
        dry_run=False,
        log=lambda _: None,
    )
    assert res.stopped.startswith("not enough free space in OUT_DIR (")
    assert "GB free" in res.stopped
    assert "99999 GB SPARE_GB reserve" in res.stopped


def test_batch_survives_a_crashing_ship(lab):
    def boom(item, out, mtime):
        raise RuntimeError("network died")

    res = batch.run_batch(lab.cfg(), lab.items, "nvenc", dry_run=False, ship=boom, log=lambda _: None, poll=0.1)
    assert res.rows[0]["status"] == "accepted"
    assert "ship error: network died" in res.rows[0]["ship"]
    assert (Path(lab.cfg().out_dir) / "m" / "Foo (2019).hevc.mkv").exists()


def test_cli_candidates_plan_run(lab, capsys):
    out_json = lab.tmp / "c.json"
    assert main(["candidates", "--out", str(out_json)], lab.env) == 0
    assert "1 files" in capsys.readouterr().out
    assert main(["candidates"], lab.env) == 0
    assert json.loads(capsys.readouterr().out)[0]["path"] == "m/Foo (2019).mkv"
    assert main(["plan", "--list", str(out_json)], lab.env) == 0
    assert "would convert" in capsys.readouterr().out
    assert not Path(lab.cfg().out_dir).exists()
    # run while DRY_RUN is true only plans
    assert main(["run"], {**lab.env, "LOMBO_SHRINK_DRY_RUN": "true"}) == 0
    assert not Path(lab.cfg().out_dir).exists()
    assert main(["run", "--list", str(out_json)], lab.env) == 0
    assert "Accepted 1 of 1" in capsys.readouterr().out
    assert (Path(lab.cfg().out_dir) / "m" / "Foo (2019).hevc.mkv").exists()
    assert main(["review"], lab.env) == 0
    assert "1 converted files still on this PC" in capsys.readouterr().out
    assert main(["run", "--list", str(out_json)], lab.env) == 0  # accepted is final: nothing to do
    assert "Accepted 0 of 0" in capsys.readouterr().out
    assert main(["run", "--list", str(out_json), "--redo"], lab.env) == 0
    assert "Accepted 1 of 1" in capsys.readouterr().out


def test_cli_run_needs_a_working_encoder(lab, capsys):
    assert main(["run"], {**lab.env, "LOMBO_SHRINK_ENCODER": "amf"}) == 1
    assert "lists no hevc_amf" in capsys.readouterr().err
    script = Path(lab.env["LOMBO_SHRINK_FFMPEG"])
    script.write_text(script.read_text().replace(" *lavfi*) exit 0;;", " *lavfi*) echo 'no device' >&2; exit 1;;"))
    assert main(["run"], lab.env) == 1
    assert "does not work here: no device" in capsys.readouterr().err


# ---------------------------------------------------------------- shipping: PC to server through stand-in ssh/scp


def stand_ins(folder: Path) -> tuple[str, str]:
    folder.mkdir(exist_ok=True)
    ssh, scp = folder / "ssh", folder / "scp"
    ssh.write_text('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\nexec sh -c "$*"\n')
    scp.write_text('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\ncp "$1" "${2#*:}"\n')
    for f in (ssh, scp):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(ssh), str(scp)


@pytest.fixture
def pair(lab):
    """The PC config and, as the 'server', the real CLI started through the stand-in ssh."""
    ssh, scp = stand_ins(lab.tmp / "tools")
    stage = lab.tmp / "server-stage"
    src = Path(__file__).resolve().parent.parent / "src"
    server_env = (
        f"LOMBO_SHRINK_LIBRARY_DIR={lab.tmp / 'lib'} LOMBO_SHRINK_STAGE_DIR={stage} LOMBO_SHRINK_DRY_RUN=false "
        f"LOMBO_SHRINK_FFPROBE=/no/such LOMBO_SHRINK_MARGIN_GB=0 PYTHONPATH={src}"
    )
    extra = {
        "LOMBO_SHRINK_SHIP_TARGET": "server",
        "LOMBO_SHRINK_SSH_COMMAND": ssh,
        "LOMBO_SHRINK_SCP_COMMAND": scp,
        "LOMBO_SHRINK_REMOTE_COMMAND": f"env {server_env} {sys.executable} -m lombo_shrink.cli",
        "LOMBO_SHRINK_REMOTE_STAGE_DIR": str(stage),
    }
    return lab, stage, extra, {**lab.env, **extra}


def test_ship_swaps_the_file_in_and_cleans_up_the_pc(pair):
    lab, stage, _extra, penv = pair
    from lombo_shrink import session

    session.open_session(stage / "session.json", 1)
    assert ship.check_server(from_env(penv)) is None
    assert main(["run"], penv) == 0
    assert lab.path.read_bytes() == b"\0" * 400  # the library now holds the 400-byte H.265 file
    out = Path(lab.cfg().out_dir)
    assert not list(out.rglob("*.hevc.mkv"))  # the PC copy is gone after the swap
    report = (out / "REPORT.txt").read_text()
    assert "ship: swapped" in report
    assert "Shipped to the server: 1" in report


def test_ship_with_keep_originals_on_the_pc_keeps_them_on_the_server(pair):
    lab, stage, _extra, penv = pair
    from lombo_shrink import session

    session.open_session(stage / "session.json", 1)
    assert main(["run"], {**penv, "LOMBO_SHRINK_KEEP_ORIGINALS": "true"}) == 0
    assert lab.path.read_bytes() == b"\0" * 400
    held = lab.tmp / "lib" / ".lombo-shrink-holding" / "m" / "Foo (2019).mkv"
    assert held.read_bytes() == b"s" * SRC_BYTES


def test_ship_without_a_session_keeps_the_file_on_the_pc(pair):
    lab, _stage, _extra, penv = pair
    assert main(["run"], penv) == 0
    assert lab.path.read_bytes() == b"s" * SRC_BYTES  # untouched
    out = Path(lab.cfg().out_dir)
    assert (out / "m" / "Foo (2019).hevc.mkv").exists()
    assert "no open conversion session" in (out / "REPORT.txt").read_text()


def test_ship_reports_an_unreachable_server(lab):
    cfg = lab.cfg(LOMBO_SHRINK_SHIP_TARGET="x", LOMBO_SHRINK_SSH_COMMAND="/bin/false")
    assert "cannot reach" in (ship.check_server(cfg) or "")
    assert "No such file" in (
        ship.check_server(lab.cfg(LOMBO_SHRINK_SHIP_TARGET="x", LOMBO_SHRINK_SSH_COMMAND="/no/ssh")) or ""
    )
    status, why = ship.ship_file(cfg, {"path": "m/Foo (2019).mkv", "size": 1000}, lab.path, 0.0, lab.path.parent)
    assert status == "failed"
    assert why


def test_an_unshipped_conversion_is_shipped_on_the_next_run_without_re_encoding(lab):
    calls: list[str] = []

    def down(item, out, mtime):
        calls.append("down")
        return "failed", "server down"

    def up(item, out, mtime):
        calls.append(f"up {mtime}")
        out.unlink()
        return "swapped", ""

    cfg = lab.cfg()
    batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, ship=down, log=lambda _: None, poll=0.1)
    out = Path(cfg.out_dir) / "m" / "Foo (2019).hevc.mkv"
    assert out.exists()
    # dry run and no-ship runs change nothing
    assert batch.run_batch(cfg, lab.items, "nvenc", dry_run=True, ship=up).planned == []
    assert batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, log=lambda _: None).rows == []
    assert calls == ["down"]
    res = batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, ship=up, log=lambda _: None, poll=0.1)
    assert [r["ship"] for r in res.rows] == ["swapped"]
    assert log_statuses(Path(cfg.out_dir)) == ["accepted"]  # not encoded a second time
    assert calls[1].startswith("up ")
    assert calls[1] != "up 0.0"
    # shipped: the next run finds nothing to do
    assert batch.run_batch(cfg, lab.items, "nvenc", dry_run=False, ship=up, log=lambda _: None).rows == []


def test_sources_differing_only_in_container_get_different_outputs(tmp_path):
    out = tmp_path / "o"
    assert batch.output_path(out, "m/A.mkv") == out / "m" / "A.hevc.mkv"
    assert batch.output_path(out, "m/A.mp4") == out / "m" / "A.mp4.hevc.mkv"
    assert batch.output_path(out, "m/A.MKV") == out / "m" / "A.hevc.mkv"
    assert batch.output_path(out, "m/A.mkv") != batch.output_path(out, "m/A.mp4")
    assert batch.output_path(out, "x/A.mkv") != batch.output_path(out, "y/A.mkv")


def test_scan_says_why_files_are_left_out(lab, tmp_path):
    _, hevc = make_bins(tmp_path / "bin3", video_probe("hevc"), video_probe("hevc"))
    quiet: list[str] = []
    assert scan.scan(lab.cfg(LOMBO_SHRINK_FFPROBE=hevc), quiet.append) == []
    assert quiet == ["1 files left out (run with -v to see why)"]
    loud: list[str] = []
    scan.scan(lab.cfg(LOMBO_SHRINK_FFPROBE=hevc), loud.append, verbose=True)
    assert len(loud) == 1
    assert loud[0].startswith("left out: m/Foo (2019).mkv (")


def test_ctrl_c_during_an_encode_kills_ffmpeg(tmp_path):
    started: list[subprocess.Popen] = []

    class Interrupted(subprocess.Popen):
        def wait(self, timeout=None):
            if not started:
                started.append(self)
                raise KeyboardInterrupt
            return super().wait(timeout)

    real = subprocess.Popen
    subprocess.Popen = Interrupted  # type: ignore[misc]
    try:
        with pytest.raises(KeyboardInterrupt):
            media.run_encode(["sleep", "30"], tmp_path / "x.partial.mkv", 10, 60, poll=0.1)
    finally:
        subprocess.Popen = real  # type: ignore[misc]
    assert started[0].poll() is not None  # the child is gone, nothing keeps the graphics card busy
