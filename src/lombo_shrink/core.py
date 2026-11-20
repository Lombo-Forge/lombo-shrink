"""The decisions, all pure: no network, disk, clock or subprocess.

Probe data is the JSON that `ffprobe -show_streams -show_format -show_chapters -of json` prints.
Everything that decides "convert this?", "is the result good?", "how is ffmpeg called?" and "is it
safe to swap?" lives here, so the rules can be tested without a video file or a graphics card.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

Probe = dict[str, Any]

ENCODER_CODECS = {
    "nvenc": "hevc_nvenc",
    "amf": "hevc_amf",
    "qsv": "hevc_qsv",
    "videotoolbox": "hevc_videotoolbox",
    "x265": "libx265",
}
AUTO_ORDER = ("nvenc", "amf", "qsv", "videotoolbox", "x265")
OK_SUBTITLES = frozenset(
    {"subrip", "srt", "ass", "ssa", "webvtt", "text", "hdmv_pgs_subtitle", "dvd_subtitle", "mov_text"}
)
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".wmv")
FINAL_STATUSES = frozenset({"accepted", "rejected", "skipped", "missing"})


# ---------------------------------------------------------------- probe helpers


def num(value: object) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def fps(text: object) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)", str(text))
    if m and num(m.group(2)) > 0:
        return num(m.group(1)) / num(m.group(2))
    return num(text)


def streams(probe: Probe, kind: str) -> list[Probe]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == kind]


def real_video(probe: Probe) -> list[Probe]:
    """Video streams that are not cover art."""
    return [s for s in streams(probe, "video") if (s.get("disposition") or {}).get("attached_pic") != 1]


def _lang(stream: Probe) -> str:
    return str((stream.get("tags") or {}).get("language", "")).lower()


def _flag(stream: Probe, name: str) -> int:
    return int((stream.get("disposition") or {}).get(name, 0) or 0)


# ---------------------------------------------------------------- should we convert?


def bits_per_pixel(probe: Probe, video: Probe, file_size: int) -> float | None:
    """Video bits per pixel per frame, or None when it cannot be worked out."""
    w, h = num(video.get("width")), num(video.get("height"))
    rate = fps(video.get("avg_frame_rate")) or fps(video.get("r_frame_rate"))
    duration = num((probe.get("format") or {}).get("duration"))
    bitrate = num(video.get("bit_rate"))
    if bitrate <= 0:
        total = num((probe.get("format") or {}).get("bit_rate"))
        if total > 0:
            audio = sum(num(a.get("bit_rate")) or 160000 for a in streams(probe, "audio"))
            bitrate = total - audio
        elif duration > 0:
            bitrate = file_size * 8 / duration
    if w > 0 and h > 0 and rate > 0 and bitrate > 0:
        return bitrate / (w * h * rate)
    return None


def skip_reason(probe: Probe, file_size: int, *, allow_hevc: bool = False, min_bpp: float = 0.08) -> str | None:
    """Why this file must not be encoded, or None when it may be. Order matches the proven original."""
    videos = real_video(probe)
    if not videos:
        return "no video stream"
    if len(videos) > 1:
        return f"more than one video stream ({len(videos)})"
    v = videos[0]
    if v.get("codec_name") == "hevc" and not allow_hevc:
        return "already HEVC"
    order = v.get("field_order")
    if order and order not in ("progressive", "unknown"):
        return f"interlaced ({order})"
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        return f"HDR ({v['color_transfer']})"
    side = " ".join(json.dumps(s.get("side_data_list")) for s in probe.get("streams", []) if s.get("side_data_list"))
    if re.search(r"DOVI|Dolby", side):
        return "Dolby Vision"
    if v.get("pix_fmt") not in ("yuv420p", "yuv420p10le", "yuvj420p"):
        return f"pixel format {v.get('pix_fmt')}"
    for s in streams(probe, "subtitle"):
        if s.get("codec_name") not in OK_SUBTITLES:
            return f"unsupported subtitle codec {s.get('codec_name')}"
    if min_bpp > 0:
        bpp = bits_per_pixel(probe, v, file_size)
        if bpp is not None and bpp < min_bpp:
            return f"low bitrate ({bpp:.3f} bits/pixel)"
    return None


# ---------------------------------------------------------------- how to call ffmpeg


def pick_encoder(preference: str, available: set[str]) -> str | None:
    """`available` holds ffmpeg encoder names such as hevc_nvenc. Returns our short name or None."""
    if preference != "auto":
        return preference if ENCODER_CODECS[preference] in available else None
    for name in AUTO_ORDER:
        if ENCODER_CODECS[name] in available:
            return name
    return None


def encoder_args(encoder: str, cq: int) -> list[str]:
    """Video encoder arguments. Only nvenc and x265 were measured; the others are best-effort mappings."""
    if encoder == "nvenc":
        return ["-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-pix_fmt", "p010le"]
    if encoder == "amf":
        qp = str(cq)
        return ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", qp, "-qp_p", qp, "-pix_fmt", "p010le"]
    if encoder == "qsv":
        return ["-c:v", "hevc_qsv", "-preset", "slower", "-global_quality", str(cq), "-pix_fmt", "p010le"]
    if encoder == "videotoolbox":
        quality = max(1, min(100, 100 - 2 * cq))
        return ["-c:v", "hevc_videotoolbox", "-q:v", str(quality), "-tag:v", "hvc1", "-pix_fmt", "p010le"]
    if encoder == "x265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str(cq), "-pix_fmt", "yuv420p10le"]
    raise ValueError(f"unknown encoder {encoder!r}")


def decode_args(encoder: str) -> list[str]:
    """Hardware decoding only where it was measured (nvenc, about 20% faster); the caller retries without it."""
    return {"nvenc": ["-hwaccel", "cuda"], "qsv": ["-hwaccel", "qsv"]}.get(encoder, [])


def stream_args(probe: Probe) -> list[str]:
    """Keep video, audio, subtitles, attachments and chapters; keep the source's default flags."""
    args = ["-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-map_chapters", "0", "-c", "copy"]
    subs = streams(probe, "subtitle")
    if any(s.get("codec_name") == "mov_text" for s in subs):
        args += ["-c:s", "srt"]
    # ffmpeg flags the first audio or subtitle stream "default" when none is; copy "not default" explicitly.
    for letter, kind in (("a", "audio"), ("s", "subtitle")):
        for index, stream in enumerate(streams(probe, kind)):
            if _flag(stream, "default") != 1:
                args += [f"-disposition:{letter}:{index}", "-default"]
    return args


def colour_args(video: Probe) -> list[str]:
    args: list[str] = []
    for key, flag in (
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
    ):
        value = str(video.get(key) or "")
        if value and value not in ("unknown", "unspecified"):
            args += [flag, value]
    return args


def encode_command(
    ffmpeg: str,
    source: str,
    partial: str,
    probe: Probe,
    encoder: str,
    cq: int,
    extra: tuple[str, ...] = (),
    *,
    hw_decode: bool = True,
) -> list[str]:
    video = real_video(probe)[0]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    if hw_decode:
        cmd += decode_args(encoder)
    cmd += ["-i", source, *stream_args(probe), *encoder_args(encoder, cq), *colour_args(video), *extra, partial]
    return cmd


def max_encode_seconds(duration: float) -> float:
    return 2 * duration + 600


# ---------------------------------------------------------------- is the result good?


def accept_problems(
    src: Probe,
    out: Probe | None,
    *,
    src_size: int,
    out_size: int,
    min_saving_pct: float,
    src_packets: int,
    out_packets: int,
    decode_error: str,
) -> list[str]:
    """Every rule the output broke. An empty list means accept."""
    problems: list[str] = []
    saving = 100 * (1 - out_size / src_size) if src_size > 0 else 0.0
    if saving < min_saving_pct:
        problems.append(f"saving {saving:.1f}% is below the {min_saving_pct:.0f}% bar")
    if out is None:
        problems.append("output is unreadable by ffprobe")
    else:
        problems += _compare(src, out, src_packets, out_packets)
    if decode_error:
        problems.append(f"decode check failed: {decode_error}")
    return problems


def _compare(src: Probe, out: Probe, src_packets: int, out_packets: int) -> list[str]:
    problems: list[str] = []
    sv, ov = real_video(src), real_video(out)
    counts = (
        ("video", len(sv), len(ov)),
        ("audio", len(streams(src, "audio")), len(streams(out, "audio"))),
        ("subtitle", len(streams(src, "subtitle")), len(streams(out, "subtitle"))),
        ("attachment", len(streams(src, "attachment")), len(streams(out, "attachment"))),
        ("chapter", len(src.get("chapters") or []), len(out.get("chapters") or [])),
    )
    problems += [f"{k} count differs ({a} vs {b})" for k, a, b in counts if a != b]
    if len(ov) == 1 and len(sv) == 1:
        v, o = sv[0], ov[0]
        if (v.get("width"), v.get("height")) != (o.get("width"), o.get("height")):
            problems.append(f"size differs ({v.get('width')}x{v.get('height')} vs {o.get('width')}x{o.get('height')})")
        if v.get("display_aspect_ratio") and o.get("display_aspect_ratio"):
            if v["display_aspect_ratio"] != o["display_aspect_ratio"]:
                problems.append(f"aspect ratio differs ({v['display_aspect_ratio']} vs {o['display_aspect_ratio']})")
        elif v.get("sample_aspect_ratio") != o.get("sample_aspect_ratio"):
            problems.append(
                f"sample aspect ratio differs ({v.get('sample_aspect_ratio')} vs {o.get('sample_aspect_ratio')})"
            )
        if v.get("r_frame_rate") != o.get("r_frame_rate"):
            problems.append(f"frame rate differs ({v.get('r_frame_rate')} vs {o.get('r_frame_rate')})")
        if src_packets < 0 or out_packets < 0:
            problems.append("could not count video packets")
        elif src_packets != out_packets:
            problems.append(f"video packet count differs ({src_packets} vs {out_packets})")
    for kind in ("audio", "subtitle"):
        a, b = streams(src, kind), streams(out, kind)
        if len(a) != len(b):
            continue
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            c1, c2 = x.get("codec_name"), y.get("codec_name")
            if c1 != c2 and not (kind == "subtitle" and c1 == "mov_text" and c2 == "subrip"):
                problems.append(f"{kind} {i} codec differs ({c1} vs {c2})")
            if _lang(x) != _lang(y):
                problems.append(f"{kind} {i} language differs")
            if _flag(x, "default") != _flag(y, "default") or _flag(x, "forced") != _flag(y, "forced"):
                problems.append(f"{kind} {i} default/forced differs")
    d1 = num((src.get("format") or {}).get("duration"))
    d2 = num((out.get("format") or {}).get("duration"))
    if abs(d1 - d2) > 1.5:
        problems.append(f"duration differs ({d1:g} vs {d2:g})")
    return problems


# ---------------------------------------------------------------- batch bookkeeping


def resume_state(records: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, float]]:
    """From the log: path -> last final status, and path -> accepted output bytes. `failed` is retried."""
    done: dict[str, str] = {}
    accepted: dict[str, float] = {}
    for r in records:
        path = str(r.get("path", ""))
        status = r.get("status")
        if status == "failed":
            done.pop(path, None)
            accepted.pop(path, None)
            continue
        done[path] = str(status)
        if status == "accepted":
            accepted[path] = num(r.get("out_bytes"))
        else:
            accepted.pop(path, None)
    return done, accepted


def should_process(path: str, done: dict[str, str], *, redo: bool) -> bool:
    if path not in done:
        return True
    return redo and done[path] in ("accepted", "rejected", "skipped")


def stop_reason(*, tried: int, limit: int, produced_bytes: float, max_gb: float, stop_file: bool) -> str | None:
    if limit > 0 and tried >= limit:
        return f"reached the limit of {limit}"
    if stop_file:
        return "STOP file found"
    if max_gb > 0 and produced_bytes / 1024**3 >= max_gb:
        return f"reached {max_gb:g} GB of output"
    return None


def has_room(free_bytes: int, file_size: int, spare_gb: float) -> bool:
    return free_bytes >= file_size * 1.1 + spare_gb * 1024**3


def sort_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Biggest first, so each conversion saves the most space."""
    return sorted(items, key=lambda i: (-int(i.get("size", 0)), str(i.get("path", ""))))


def format_report(rows: list[dict[str, Any]], title: str) -> str:
    lines = [title]
    saved = 0
    accepted = [r for r in rows if r["status"] == "accepted"]
    for r in rows:
        if r["status"] == "accepted":
            pct = round(100 * (r["src"] - r["out"]) / r["src"]) if r["src"] > 0 else 0
            lines.append(
                f"{r['title']}  {r['src'] / 1024**2:,.0f} MB -> {r['out'] / 1024**2:,.0f} MB  {pct}% saved"
                f"  CQ {r['cq']}  ship: {r.get('ship') or 'not shipped (kept on this PC)'}"
            )
            saved += r["src"] - r["out"]
        else:
            lines.append(f"{r['title']}  {r['status']}: {r.get('msg', '')}")
    lines += ["", f"Files converted: {len(accepted)}", f"GB saved: {saved / 1024**3:.1f}"]
    shipped = sum(1 for r in accepted if r.get("ship") == "swapped")
    lines += [f"Shipped to the server: {shipped}", f"Kept on this PC: {len(accepted) - shipped}"]
    for status in ("skipped", "rejected", "failed", "missing"):
        group = [r for r in rows if r["status"] == status]
        if group:
            lines.append(f"{status}: {len(group)}")
            reasons: dict[str, int] = {}
            for r in group:
                reasons[str(r.get("msg", ""))] = reasons.get(str(r.get("msg", "")), 0) + 1
            lines += [f"  {n} x {why}" for why, n in reasons.items()]
    return "\n".join(lines)


# ---------------------------------------------------------------- the swap (server side)


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the library
    staged: str  # relative to the staging folder
    sha256: str
    out_bytes: int
    src_bytes: int
    src_mtime: float | None  # seconds since the epoch; None skips the modified-time check


def safe_relative(path: str) -> bool:
    """A relative path with no `..`, no leading slash, no NUL and no trailing slash."""
    if not path or path.startswith(("/", "\\")) or path.endswith("/") or "\0" in path or "\n" in path:
        return False
    return ".." not in path.replace("\\", "/").split("/")


def parse_manifest(data: object) -> list[Entry]:
    """Validate the manifest the PC uploads. Raises ValueError with a short reason."""
    if not isinstance(data, list):
        raise ValueError("the manifest must be a JSON list")
    entries: list[Entry] = []
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("every manifest entry must be an object")
        path, staged = str(raw.get("path", "")), str(raw.get("staged", ""))
        sha = str(raw.get("sha256", "")).lower()
        if not safe_relative(path):
            raise ValueError(f"unsafe or empty path {path!r}: it must be relative with no ..")
        if not safe_relative(staged):
            raise ValueError(f"unsafe or empty staged path {staged!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"bad sha256 for {path!r}")
        try:
            out_b, src_b = int(raw["out_bytes"]), int(raw["src_bytes"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"out_bytes and src_bytes must be whole numbers ({path!r})") from None
        if out_b <= 0 or src_b <= 0:
            raise ValueError(f"sizes must be positive ({path!r})")
        mtime = raw.get("src_mtime")
        entries.append(Entry(path, staged, sha, out_b, src_b, None if mtime in (None, "") else _epoch(mtime)))
    return entries


def _epoch(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def final_relative(rel: str) -> str:
    """The new file keeps the name; any container other than .mkv becomes .mkv."""
    if rel.lower().endswith(".mkv"):
        return rel
    stem, dot, _ = rel.rpartition(".")
    return (stem if dot else rel) + ".mkv"


def mtime_matches(have: float, want: float | None, tolerance: float = 2.0) -> bool:
    return want is None or abs(have - want) <= tolerance


def room_ok(free_bytes: int, new_size: int, margin_bytes: int) -> bool:
    return free_bytes - new_size >= margin_bytes
