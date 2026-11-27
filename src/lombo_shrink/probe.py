"""Pick a quality level (CQ) for one source: encode short samples at several levels, score each against the
source with VMAF, and take the highest level that still looks like the source. Off unless QUALITY_PROBE=true.

Needs an ffmpeg with the libvmaf filter. The source is only read. Any failure raises ProbeError and the caller
uses the fixed CQ instead.
"""

import json
import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import core
from .config import Config

SAMPLES, SAMPLE_SECONDS = 6, 20.0
LEVELS = (24, 27, 30)
MIN_CQ = 22
MEAN_TARGET, P5_TARGET = 95.0, 90.0  # mean VMAF, and the 5th percentile (the worst frames)
MARGIN = 0.3  # a level that was not tested must clear the targets by this much
SUBSAMPLE = 5
STEP_TIMEOUT = 3600


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Score:
    cq: int
    mean: float
    p5: float
    ratio: float  # sample size / source video size


@dataclass(frozen=True)
class Choice:
    cq: int | None  # None: do not convert, see reason
    reason: str
    mean: float
    p5: float


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    pos = p * (len(ordered) - 1)
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def interpolate(xs: list[int], ys: list[float], x: float) -> float:
    """y at x from the two tested points around it (the nearest two when x is outside them)."""
    i = 0
    while i < len(xs) - 2 and x > xs[i + 1]:
        i += 1
    return ys[i] + (ys[i + 1] - ys[i]) * ((x - xs[i]) / (xs[i + 1] - xs[i]))


def windows(duration: float) -> tuple[list[float], float]:
    """(start times, seconds each): spread from 8% to 92% of the film; fewer and shorter for a short one."""
    seconds = SAMPLE_SECONDS
    if duration < seconds * 1.25:
        seconds = max(1.0, math.floor(duration * 0.8))
    count = SAMPLES
    if duration < count * seconds * 1.5:
        count = max(1, math.floor(duration / (seconds * 1.5)))
    low = 0.08 * duration
    high = max(low, 0.92 * duration - seconds)
    if count == 1:
        return [(low + high) / 2], seconds
    return [low + i * (high - low) / (count - 1) for i in range(count)], seconds


def choose(test: Callable[[int], Score], max_cq: int, min_saving_pct: float) -> Choice:
    """The decision, separate from ffmpeg: `test(cq)` measures one level."""
    scores = {cq: test(cq) for cq in LEVELS}

    def passing() -> list[int]:
        return sorted(
            c for c, s in scores.items() if MIN_CQ <= c <= max_cq and s.mean >= MEAN_TARGET and s.p5 >= P5_TARGET
        )

    good = passing()
    if not good:
        scores[MIN_CQ] = test(MIN_CQ)
        good = passing()
    xs = sorted(scores)
    means, p5s = [scores[c].mean for c in xs], [scores[c].p5 for c in xs]
    logs = [math.log(max(scores[c].ratio, 1e-6)) for c in xs]
    if not good:
        best = max(scores.values(), key=lambda s: s.mean)
        return Choice(
            None, f"quality target not reachable (best vmaf {best.mean:.1f} at CQ {best.cq})", best.mean, best.p5
        )
    cq = good[-1]
    mean, p5 = scores[cq].mean, scores[cq].p5
    higher = [x for x in xs if x > cq]
    if higher:
        for k in range(min(higher[0] - 1, max_cq), cq, -1):
            im, ip = interpolate(xs, means, k), interpolate(xs, p5s, k)
            if im >= MEAN_TARGET + MARGIN and ip >= P5_TARGET + MARGIN:
                cq, mean, p5 = k, im, ip
                break
    saving = 100 * (1 - math.exp(interpolate(xs, logs, cq)))
    if saving < min_saving_pct:
        return Choice(None, f"predicted saving {saving:.1f}% below {min_saving_pct:g}%", mean, p5)
    return Choice(cq, "", mean, p5)


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argument list, no shell; the binaries come from the settings
            cmd, capture_output=True, text=True, errors="replace", timeout=STEP_TIMEOUT, check=False, cwd=cwd
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(f"{type(e).__name__}: {e}") from e


def has_vmaf(ffmpeg: str) -> bool:
    try:
        return "libvmaf" in _run([ffmpeg, "-hide_banner", "-filters"]).stdout.split()
    except ProbeError:
        return False


def _source_bytes(ffprobe: str, source: str, start: float, seconds: float) -> float:
    done = _run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{start:.3f}%{start + seconds + 1:.3f}",
         "-show_entries", "packet=pts_time,size", "-of", "compact=p=0", source]
    )  # fmt: skip
    total = 0.0
    for line in done.stdout.splitlines():
        fields = dict(part.partition("=")[::2] for part in line.split("|"))
        if "size" in fields and start <= core.num(fields.get("pts_time")) < start + seconds:
            total += core.num(fields["size"])
    if done.returncode != 0 or total <= 0:
        raise ProbeError("could not measure the source in a sample window")
    return total


def probe_file(cfg: Config, source: Path, encoder: str, log: Callable[[str], None] = lambda _: None) -> Choice:
    vmaf = cfg.vmaf_ffmpeg or cfg.ffmpeg
    src = str(source.resolve())  # scoring runs in a work folder, so a relative path would not be found
    done = _run(
        [cfg.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate:format=duration", "-of", "json", src]
    )  # fmt: skip
    try:
        data = json.loads(done.stdout)
        duration, rate = core.num(data["format"]["duration"]), str(data["streams"][0]["r_frame_rate"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ProbeError("could not read the duration and frame rate") from e
    if duration <= 0 or rate in ("", "0/0"):
        raise ProbeError("could not read the duration and frame rate")
    starts, seconds = windows(duration)
    source_total = sum(_source_bytes(cfg.ffprobe, src, s, seconds) for s in starts)
    quiet = ["-hide_banner", "-loglevel", "error", "-nostats", "-y"]

    with tempfile.TemporaryDirectory(prefix="shrink-probe-", dir=cfg.out_dir or None) as work:

        def test(cq: int) -> Score:
            frames: list[float] = []
            encoded = 0
            for i, start in enumerate(starts):
                sample = Path(work) / f"s{i}_{cq}.mkv"
                window = ["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", src]
                video = ["-map", "0:v:0", "-an", "-sn", *core.encoder_args(encoder, cq), *cfg.extra_args, str(sample)]
                result = None
                for hw in (core.decode_args(encoder), []):  # GPU decode first, then CPU decode
                    result = _run([cfg.ffmpeg, *quiet, *hw, *window, *video])
                    if result.returncode == 0 or not hw:
                        break
                if result is None or result.returncode != 0 or not sample.exists():
                    raise ProbeError(f"sample encode failed at CQ {cq}")
                encoded += sample.stat().st_size
                name = f"v_{i}_{cq}.json"  # a bare name, with the work folder as cwd: no drive-colon escaping
                chain = (
                    f"trim=start=0:duration={seconds:.3f},setpts=PTS-STARTPTS,fps={rate},settb=1/24000,"
                    "format=yuv420p10le"
                )
                graph = (
                    f"[0:v]{chain}[d];[1:v]{chain}[r];[d][r]libvmaf=log_fmt=json:log_path={name}:"
                    f"n_subsample={SUBSAMPLE}:n_threads=8"
                )
                scored = _run(
                    [vmaf, *quiet, "-i", sample.name, *window, "-filter_complex", graph, "-f", "null", "-"], cwd=work
                )
                report = Path(work) / name
                try:
                    frames += [float(f["metrics"]["vmaf"]) for f in json.loads(report.read_text())["frames"]]
                except (OSError, ValueError, KeyError, TypeError) as e:
                    raise ProbeError(f"VMAF scoring failed at CQ {cq} (exit {scored.returncode})") from e
                sample.unlink(missing_ok=True)
                report.unlink(missing_ok=True)
            if not frames:
                raise ProbeError(f"no frames were scored at CQ {cq}")
            score = Score(cq, sum(frames) / len(frames), percentile(frames, 0.05), encoded / source_total)
            log(f"  probe CQ {cq}: vmaf mean {score.mean:.1f}, p5 {score.p5:.1f}, size {score.ratio:.0%} of source")
            return score

        return choose(test, cfg.max_cq, cfg.min_saving_pct)
