"""The per-file quality probe: the decision, and the ffmpeg round with stand-ins. No real libvmaf is used."""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import unix_only
from lombo_shrink import batch, probe
from lombo_shrink.config import from_env


def curve(table):
    seen = []

    def test(cq):
        seen.append(cq)
        mean, p5, ratio = table[cq]
        return probe.Score(cq, mean, p5, ratio)

    return test, seen


def test_takes_the_highest_level_between_tested_points_that_clears_the_margin():
    test, seen = curve({24: (97.0, 93.0, 0.5), 27: (96.0, 92.0, 0.4), 30: (93.0, 88.0, 0.3)})
    choice = probe.choose(test, max_cq=28, min_saving_pct=20)
    assert (choice.cq, choice.reason) == (27, "")  # 28 would be 95.0, short of 95.3
    assert seen == [24, 27, 30]


def test_never_goes_above_max_cq():
    test, _ = curve({24: (99.0, 97.0, 0.5), 27: (98.0, 96.0, 0.4), 30: (97.0, 95.0, 0.3)})
    assert probe.choose(test, max_cq=28, min_saving_pct=20).cq == 28


def test_tries_22_when_nothing_passes_then_gives_up():
    test, seen = curve({22: (96.0, 91.0, 0.6), 24: (94.0, 89.0, 0.5), 27: (92.0, 87.0, 0.4), 30: (90.0, 85.0, 0.3)})
    assert probe.choose(test, 28, 20).cq == 22
    assert seen == [24, 27, 30, 22]
    test, _ = curve(dict.fromkeys((22, 24, 27, 30), (90.0, 85.0, 0.5)))
    choice = probe.choose(test, 28, 20)
    assert choice.cq is None
    assert "not reachable" in choice.reason


def test_a_small_saving_means_do_not_convert():
    test, _ = curve({24: (97.0, 93.0, 0.95), 27: (96.0, 92.0, 0.9), 30: (93.0, 88.0, 0.85)})
    choice = probe.choose(test, 28, 20)
    assert choice.cq is None
    assert "below 20%" in choice.reason


def test_windows_spread_over_a_film_and_shrink_for_a_short_clip():
    starts, seconds = probe.windows(7200)
    assert (len(starts), seconds, starts[0]) == (6, 20.0, 576.0)
    assert starts[-1] + seconds == pytest.approx(0.92 * 7200)
    starts, seconds = probe.windows(10)
    assert len(starts) == 1
    assert seconds == 8
    assert probe.percentile([1, 2, 3, 4, 5], 0.05) == pytest.approx(1.2)


@unix_only
def test_the_ffmpeg_round_reads_scores_and_failures_raise(tmp_path, monkeypatch):
    commands = []

    def fake(cmd, cwd=None):
        commands.append(cmd)
        out = ""
        if "stream=r_frame_rate:format=duration" in cmd:
            out = json.dumps({"format": {"duration": "10"}, "streams": [{"r_frame_rate": "24/1"}]})
        elif "packet=pts_time,size" in cmd:
            out = "pts_time=1.5|size=1000\npts_time=99|size=5"
        elif "-filter_complex" in cmd:
            name = cmd[cmd.index("-filter_complex") + 1].split("log_path=")[1].split(":")[0]
            Path(cwd, name).write_text(json.dumps({"frames": [{"metrics": {"vmaf": 99}}]}))
        else:
            Path(cmd[-1]).write_bytes(b"x" * 100)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(probe, "_run", fake)
    cfg = from_env({"LOMBO_SHRINK_OUT_DIR": str(tmp_path), "LOMBO_SHRINK_VMAF_FFMPEG": "vmaf-ffmpeg"})
    choice = probe.probe_file(cfg, tmp_path / "a.mkv", "x265")
    assert (choice.cq, choice.mean) == (28, 99.0)
    assert any(c[0] == "vmaf-ffmpeg" for c in commands)
    assert not list(tmp_path.glob("shrink-probe-*"))
    monkeypatch.setattr(probe, "_run", lambda cmd, cwd=None: subprocess.CompletedProcess(cmd, 1, "", "no"))
    with pytest.raises(probe.ProbeError):
        probe.probe_file(cfg, tmp_path / "a.mkv", "x265")


def test_a_failed_probe_uses_the_fixed_cq_and_a_refusal_is_a_skip(tmp_path, monkeypatch):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    cfg = from_env({"LOMBO_SHRINK_QUALITY_PROBE": "true"})
    monkeypatch.setattr(batch.media, "probe", lambda *_: {"streams": []})
    monkeypatch.setattr(batch.core, "skip_reason", lambda *_, **__: None)

    def boom(*_):
        raise probe.ProbeError("broken")

    monkeypatch.setattr(probe, "probe_file", boom)
    assert batch._pick_cq(cfg, src, "x265", lambda _: None) == (cfg.cq, {}, "")
    monkeypatch.setattr(probe, "probe_file", lambda *_: probe.Choice(None, "not reachable", 90, 85))
    assert batch._pick_cq(cfg, src, "x265", lambda _: None)[2] == "not reachable"
    monkeypatch.setattr(probe, "probe_file", lambda *_: probe.Choice(25, "", 96, 91))
    assert batch._pick_cq(cfg, src, "x265", lambda _: None)[0] == 25
