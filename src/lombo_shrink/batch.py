"""The batch on the main PC: read the candidate list, convert each file into a mirror folder, log, report.

It never writes to the library. Results go to OUT_DIR as `<name>.hevc.mkv`; the log is `convert-log.jsonl`.
Resume: accepted, rejected, skipped and missing are final; failed is retried. Create a file named STOP in
OUT_DIR to stop before the next file.
"""

import json
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import core, media, probe
from .config import Config

Ship = Callable[
    [dict[str, Any], Path, float], tuple[str, str]
]  # (item, converted file, source mtime) -> (status, reason)


@dataclass
class BatchResult:
    stopped: str = "finished the list"
    rows: list[dict[str, Any]] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    report: str = ""


@contextmanager
def keep_awake() -> Iterator[None]:
    """Stop Windows sleeping while the batch runs. Does nothing elsewhere."""
    windll = getattr(__import__("ctypes"), "windll", None) if sys.platform == "win32" else None
    if windll is None:
        yield
        return
    windll.kernel32.SetThreadExecutionState(0x80000001)  # pragma: no cover
    try:  # pragma: no cover
        yield
    finally:  # pragma: no cover
        windll.kernel32.SetThreadExecutionState(0x80000000)


def output_path(out_dir: Path, rel: str) -> Path:
    """`a.mkv` -> `a.hevc.mkv`; any other container keeps its extension (`a.mp4` -> `a.mp4.hevc.mkv`), so
    two sources that differ only in container never share one output."""
    p = Path(rel)
    stem = str(p.with_suffix("")) if p.suffix.lower() == ".mkv" else rel
    return out_dir / (stem + ".hevc.mkv")


def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # a half-written last line after a crash
    return records


def _pick_cq(cfg: Config, src: Path, encoder: str, log: Callable[[str], None]) -> tuple[int, dict[str, float], str]:
    """(CQ to use, scores to record, why not to convert). A file the encode would skip anyway is not probed."""
    info = media.probe(cfg.ffprobe, str(src))
    if info is None or core.skip_reason(info, src.stat().st_size, allow_hevc=cfg.allow_hevc, min_bpp=cfg.min_bpp):
        return cfg.cq, {}, ""  # convert_file reports it
    try:
        choice = probe.probe_file(cfg, src, encoder, log)
    except probe.ProbeError as e:
        log(f"  quality probe failed ({e}); using CQ {cfg.cq}")
        return cfg.cq, {}, ""
    scores = {"vmaf_mean": round(choice.mean, 2), "vmaf_p5": round(choice.p5, 2)}
    if choice.cq is None:
        return cfg.cq, scores, choice.reason
    log(f"  quality probe picked CQ {choice.cq}")
    return choice.cq, scores, ""


def run_batch(
    cfg: Config,
    items: list[dict[str, Any]],
    encoder: str,
    *,
    dry_run: bool,
    redo: bool = False,
    ship: Ship | None = None,
    log: Callable[[str], None] = print,
    poll: float = 10,
) -> BatchResult:
    out_dir = Path(cfg.out_dir)
    library = Path(cfg.library_dir)
    result = BatchResult()
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.rglob("*.partial.mkv"):
            stale.unlink(missing_ok=True)
    log_path = out_dir / "convert-log.jsonl"
    records = _read_log(log_path)
    done, accepted = core.resume_state(records)
    converted_mtime = {str(r["path"]): float(r.get("src_mtime") or 0) for r in records if r.get("status") == "accepted"}
    tried = 0

    def record(it: dict[str, Any], status: str, msg: str, out: Path | None, extra: dict[str, Any]) -> int:
        size = out.stat().st_size if out and out.exists() else 0
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "path": it["path"],
            "status": status,
            "src_bytes": it.get("size", 0),
            "out_bytes": size,
            "msg": msg,
            "title": it.get("title", ""),
            **extra,
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return size

    def row(it: dict[str, Any], status: str, msg: str = "", **more: Any) -> None:
        result.rows.append(
            {
                "title": it.get("title", it["path"]),
                "status": status,
                "msg": msg,
                "src": 0,
                "out": 0,
                "cq": cfg.cq,
                **more,
            }
        )

    def try_ship(it: dict[str, Any], out: Path, mtime: float) -> tuple[str, str]:
        if not ship:
            return "", ""
        log("  shipping to the server...")
        try:
            status, msg = ship(it, out, mtime)
        except Exception as err:  # noqa: BLE001 - one bad ship never stops the run; the file stays on this PC
            status, msg = "failed", f"ship error: {err}"
        log("  swapped into the library" if status == "swapped" else f"  not shipped ({status}), kept here: {msg}")
        return status, msg

    with keep_awake():
        for it in items:
            why = core.stop_reason(
                tried=tried,
                limit=cfg.limit,
                produced_bytes=sum(accepted.values()),
                max_gb=cfg.max_gb,
                stop_file=(out_dir / "STOP").exists(),
            )
            if why:
                result.stopped = why
                break
            src = library / it["path"]
            out = output_path(out_dir, it["path"])
            if not core.should_process(it["path"], done, redo=redo):
                # Converted earlier but never shipped (server was down): ship it now, do not re-encode.
                if ship and not dry_run and done.get(it["path"]) == "accepted" and out.is_file():
                    size = out.stat().st_size
                    log(f"[retry] {it.get('title', it['path'])}")
                    st, msg = try_ship(it, out, converted_mtime.get(it["path"], 0.0))
                    row(
                        it,
                        "accepted",
                        src=it.get("size", 0),
                        out=size,
                        ship=st if st == "swapped" else f"{st} {msg}".strip(),
                    )
                continue
            if not dry_run:
                free = shutil.disk_usage(out_dir).free
                if not core.has_room(free, int(it.get("size", 0)), cfg.spare_gb):
                    need = (int(it.get("size", 0)) * 1.1 + cfg.spare_gb * 1024**3) / 1024**3
                    result.stopped = (
                        f"not enough free space in OUT_DIR ({free / 1024**3:.1f} GB free, "
                        f"{need:.1f} GB needed including the {cfg.spare_gb:g} GB SPARE_GB reserve)"
                    )
                    break
            if dry_run:
                result.planned.append(
                    f"would convert {it['path']} ({int(it.get('size', 0)) // 1024**2} MB) with {encoder} "
                    + ("CQ picked per file" if cfg.quality_probe else f"CQ {cfg.cq}")
                )
                tried += 1
                continue
            if not src.is_file():
                record(it, "missing", "source not found", None, {})
                row(it, "missing", "source not found")
                log(f"MISSING {src}")
                continue
            log(f"[{tried + 1}] {it.get('title', it['path'])}  ({int(it.get('size', 0)) // 1024**2} MB)")
            mtime = src.stat().st_mtime
            started = time.monotonic()
            cq, scores, refused = _pick_cq(cfg, src, encoder, log) if cfg.quality_probe else (cfg.cq, {}, "")
            tried += 1
            if refused:
                record(it, "skipped", refused, None, {"src_mtime": mtime, **scores})
                row(it, "skipped", refused)
                log(f"  skipped: {refused}")
                continue
            outcome = media.convert_file(cfg, src, out, encoder, cq, poll)
            extra = {
                **scores,
                "cq": cq,
                "encoder": encoder,
                "encode_seconds": round(time.monotonic() - started, 1),
                "src_mtime": mtime,
            }
            if outcome.status == "accepted":
                size = record(it, "accepted", "", out, extra)
                accepted[it["path"]] = float(size)
                ship_status, ship_msg = try_ship(it, out, mtime)
                row(
                    it,
                    "accepted",
                    src=it.get("size", 0),
                    out=size,
                    ship=ship_status if ship_status == "swapped" else f"{ship_status} {ship_msg}".strip(),
                    cq=cq,
                )
            else:
                if outcome.status == "rejected":
                    out.unlink(missing_ok=True)
                    accepted.pop(it["path"], None)
                record(it, outcome.status, outcome.msg, None, extra)
                row(it, outcome.status, outcome.msg)
                log(f"  {outcome.status}: {outcome.msg}")

    if dry_run:
        result.report = "\n".join(result.planned) or "Nothing to do."
    else:
        result.report = core.format_report(
            result.rows, f"Conversion report {datetime.now().isoformat(timespec='seconds')}  (OUT_DIR {out_dir})"
        )
        (out_dir / "REPORT.txt").write_text(result.report + "\n", encoding="utf-8")
    return result
