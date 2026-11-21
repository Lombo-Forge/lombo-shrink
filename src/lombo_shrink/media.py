"""ffprobe and ffmpeg calls: probing, one encode with a watchdog, the decode check, one file end to end.

Nothing here ever writes next to the source. The result goes to a `.partial.mkv` name and is renamed to
its final name only after every check passed, so a crash or power loss never leaves a plausible file.
"""

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import core
from .config import Config
from .core import Probe

_PACKET_ARGS = ["-show_entries", "stream=nb_read_packets", "-of", "csv=p=0"]


def probe(ffprobe: str, path: str) -> Probe | None:
    try:
        done = subprocess.run(  # noqa: S603 - argument list, no shell; the binary comes from the settings
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def packet_count(ffprobe: str, path: str, selector: str) -> int:
    try:
        done = subprocess.run(  # noqa: S603
            [ffprobe, "-v", "error", "-count_packets", "-select_streams", selector, *_PACKET_ARGS, path],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    if done.returncode != 0:
        return -1
    return int(core.num(done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""))


def available_encoders(ffmpeg: str) -> set[str]:
    """Names from `ffmpeg -encoders`. Being listed does not prove the hardware exists: see encoder_error."""
    try:
        done = subprocess.run(  # noqa: S603
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def encoder_error(ffmpeg: str, encoder: str, cq: int) -> str | None:
    """Encode one second of generated video. None means the encoder really works on this machine."""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=24:duration=1",
    ]
    cmd += [*core.encoder_args(encoder, cq), "-f", "null", "-"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if done.returncode != 0:
        return (done.stderr.strip().splitlines() or [f"exit code {done.returncode}"])[0]
    return None


@dataclass(frozen=True)
class EncodeResult:
    code: int
    why: str | None  # set when the watchdog killed it
    stderr: str


def run_encode(
    cmd: list[str], partial: Path, stall_minutes: float, max_seconds: float, poll: float = 10
) -> EncodeResult:
    """Run ffmpeg; kill it when the partial file stops growing or it runs far too long."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)  # noqa: S603
        except OSError as e:
            return EncodeResult(-1, f"could not start ffmpeg: {e}", "")
        started = last_grow = time.monotonic()
        last_size = -1
        why: str | None = None
        while True:
            try:
                proc.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                pass
            except BaseException:  # Ctrl+C or a signal: never leave ffmpeg running and holding the graphics card
                proc.kill()
                proc.wait()
                raise
            size = partial.stat().st_size if partial.exists() else 0
            now = time.monotonic()
            if size != last_size:
                last_size, last_grow = size, now
            if now - last_grow >= stall_minutes * 60:
                why = f"encode stalled (no output growth for {stall_minutes:g} min)"
            elif now - started > max_seconds:
                why = f"encode took too long (over {int(max_seconds / 60)} min)"
            if why:
                proc.kill()
                proc.wait()
                break
        err.seek(0)
        return EncodeResult(proc.returncode if why is None else -1, why, err.read().decode("utf-8", "replace"))


def decode_error(ffmpeg: str, path: Path) -> str:
    """Decode the whole output. Any message or a non-zero exit is a problem."""
    try:
        done = subprocess.run(  # noqa: S603
            [ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        return str(e)
    text = " ".join(done.stderr.split())
    if done.returncode != 0 and not text:
        return f"ffmpeg exited {done.returncode}"
    return text


@dataclass(frozen=True)
class Outcome:
    status: str  # accepted, skipped, rejected, failed
    msg: str = ""
    out_bytes: int = 0


def convert_file(cfg: Config, source: Path, out: Path, encoder: str, cq: int, poll: float = 10) -> Outcome:
    """The source is only read. Mirrors the exit codes of the original: skip/accept, reject, fail."""
    if cq > cfg.max_cq:
        return Outcome("rejected", f"CQ above MAX_CQ ({cq} > {cfg.max_cq})")
    if not source.is_file():
        return Outcome("failed", f"source not readable: {source}")
    src_probe = probe(cfg.ffprobe, str(source))
    if src_probe is None:
        return Outcome("failed", "ffprobe could not read the source")
    src_size = source.stat().st_size
    reason = core.skip_reason(src_probe, src_size, allow_hevc=cfg.allow_hevc, min_bpp=cfg.min_bpp)
    if reason:
        return Outcome("skipped", reason)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial.mkv")
    partial.unlink(missing_ok=True)
    try:
        duration = core.num((src_probe.get("format") or {}).get("duration"))
        res = EncodeResult(-1, None, "")
        for hw in (True, False):  # GPU decode first, once more with CPU decode if that fails
            cmd = core.encode_command(
                cfg.ffmpeg, str(source), str(partial), src_probe, encoder, cq, cfg.extra_args, hw_decode=hw
            )
            res = run_encode(cmd, partial, cfg.stall_minutes, core.max_encode_seconds(duration), poll)
            if res.why or res.code == 0 or not core.decode_args(encoder):
                break
        if res.why:
            return Outcome("failed", res.why)
        if res.code != 0:
            first = (res.stderr.strip().splitlines() or [""])[0]
            return Outcome("failed", f"ffmpeg exited {res.code} {first}".strip())
        if not partial.exists():
            return Outcome("failed", "ffmpeg produced no output")

        out_size = partial.stat().st_size
        out_probe = probe(cfg.ffprobe, str(partial))
        problems = core.accept_problems(
            src_probe,
            out_probe,
            src_size=src_size,
            out_size=out_size,
            min_saving_pct=cfg.min_saving_pct,
            src_packets=packet_count(cfg.ffprobe, str(source), str(core.real_video(src_probe)[0].get("index", 0))),
            out_packets=packet_count(cfg.ffprobe, str(partial), "v:0"),
            decode_error=decode_error(cfg.ffmpeg, partial),
        )
        if problems:
            return Outcome("rejected", "; ".join(problems))
        out.unlink(missing_ok=True)
        partial.replace(out)
        return Outcome("accepted", "", out_size)
    finally:
        partial.unlink(missing_ok=True)
