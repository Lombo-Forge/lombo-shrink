"""Command line. Talks to the outside world; core.py decides.

lombo-shrink doctor                 check settings, ffmpeg, the encoder, the library and (if set) the server
lombo-shrink candidates [--out F]   list the files worth converting, biggest first (JSON); only reads
lombo-shrink plan [--list F]        show what `run` would convert; changes nothing, ever
lombo-shrink run [--list F]         convert (needs DRY_RUN=false), optionally shipping each file to the server
lombo-shrink review                 list converted files still waiting on this PC; only reads
lombo-shrink swap --manifest M --result R [--apply]   server side: swap converted files in (dry run without --apply)
lombo-shrink session open|close|status [HOURS]        server side: the lock that allows `swap --apply`
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, batch, core, media, notify, scan, session, ship, swap
from .config import Config, ConfigError, from_env
from .jellyfin import Jellyfin, JellyfinError


def _need_library(cfg: Config) -> str | None:
    if not cfg.library_dir or not Path(cfg.library_dir).is_dir():
        return "LOMBO_SHRINK_LIBRARY_DIR must be an existing folder (the library as this machine sees it)"
    return None


def _resolve_encoder(cfg: Config) -> tuple[str | None, str]:
    available = media.available_encoders(cfg.ffmpeg)
    name = core.pick_encoder(cfg.encoder, available)
    if name is None:
        wanted = "any H.265 encoder" if cfg.encoder == "auto" else core.ENCODER_CODECS[cfg.encoder]
        return None, f"ffmpeg ({cfg.ffmpeg}) lists no {wanted}"
    return name, ""


def doctor(cfg: Config) -> int:
    ok = True

    def line(good: bool, label: str, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{' ok ' if good else 'FAIL'}] {label}: {detail}")

    line(True, "mode", "dry run (nothing is changed)" if cfg.dry_run else "LIVE")
    problem = _need_library(cfg)
    line(problem is None, "library", cfg.library_dir if problem is None else str(problem))
    out = Path(cfg.out_dir).resolve()
    inside = bool(cfg.library_dir) and Path(cfg.library_dir).resolve() in (out, *out.parents)
    line(not inside, "output folder", "OUT_DIR is inside the library: set it elsewhere" if inside else str(out))
    version = media.available_encoders(cfg.ffmpeg)
    line(bool(version), "ffmpeg", cfg.ffmpeg if version else f"cannot run {cfg.ffmpeg} (set LOMBO_SHRINK_FFMPEG)")
    line(_runs(cfg.ffprobe), "ffprobe", cfg.ffprobe)
    if version:
        name, why = _resolve_encoder(cfg)
        if name is None:
            line(False, "encoder", why)
        else:
            err = media.encoder_error(cfg.ffmpeg, name, cfg.cq)
            line(
                err is None,
                "encoder",
                f"{core.ENCODER_CODECS[name]} encodes a test clip"
                if err is None
                else f"{core.ENCODER_CODECS[name]} failed: {err}",
            )
    if cfg.ship_target:
        err2 = ship.check_server(cfg)
        line(
            err2 is None,
            "server",
            f"{cfg.ship_target} answers and has {cfg.remote_command}" if err2 is None else str(err2),
        )
    else:
        line(True, "server", "SHIP_TARGET not set: converted files stay on this PC until shipped")
    if cfg.jellyfin_url and cfg.jellyfin_api_key:
        try:
            info = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key).info()
            line(True, "Jellyfin", f"{info.get('ServerName')} (key accepted)")
        except JellyfinError as e:
            line(False, "Jellyfin", str(e))
    line(True, "webhook", "set" if cfg.webhook_url else "not set (alerts are skipped)")
    return 0 if ok else 1


def _runs(binary: str) -> bool:
    try:
        return subprocess.run([binary, "-version"], capture_output=True, timeout=30, check=False).returncode == 0  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return False


def _load_items(cfg: Config, list_file: str | None, verbose: bool = False) -> list[dict[str, Any]]:
    if list_file:
        data = json.loads(Path(list_file).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError(f"{list_file} must hold a JSON list")
        return core.sort_candidates(data)
    return scan.scan(cfg, lambda m: print(m, file=sys.stderr), verbose=verbose)


def _candidates(cfg: Config, out: str | None, verbose: bool = False) -> int:
    if (problem := _need_library(cfg)) is not None:
        print(f"[FAIL] {problem}", file=sys.stderr)
        return 1
    items = scan.scan(cfg, lambda m: print(m, file=sys.stderr), verbose=verbose)
    text = json.dumps(items, indent=1)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        total = sum(int(i["size"]) for i in items)
        print(f"{len(items)} files, {total / 1024**3:.1f} GB, written to {out}")
    else:
        print(text)
    return 0


def _batch(cfg: Config, list_file: str | None, *, dry: bool, redo: bool = False, verbose: bool = False) -> int:
    if (problem := _need_library(cfg)) is not None:
        print(f"[FAIL] {problem}", file=sys.stderr)
        return 1
    name, why = _resolve_encoder(cfg)
    if name is None:
        print(f"[FAIL] {why}", file=sys.stderr)
        return 1
    items = _load_items(cfg, list_file, verbose)
    if not dry and (err := media.encoder_error(cfg.ffmpeg, name, cfg.cq)):
        print(f"[FAIL] {core.ENCODER_CODECS[name]} does not work here: {err}", file=sys.stderr)
        return 1
    out_dir = Path(cfg.out_dir)

    def ship_one(item: dict[str, Any], out: Path, mtime: float) -> tuple[str, str]:
        return ship.ship_file(cfg, item, out, mtime, out_dir)

    shipper: batch.Ship | None = None
    if cfg.ship_target and not dry:
        if (err := ship.check_server(cfg)) is not None:
            print(f"warning: {err}; nothing will be shipped, files stay in {cfg.out_dir}", file=sys.stderr)
        else:
            shipper = ship_one

    result = batch.run_batch(cfg, items, name, dry_run=dry, redo=redo, ship=shipper)
    print(result.report)
    if dry:
        print(
            f"\nDry run: {len(result.planned)} files with {name}. Nothing was encoded."
            " Set LOMBO_SHRINK_DRY_RUN=false to convert."
        )
    else:
        done = sum(1 for r in result.rows if r["status"] == "accepted")
        print(
            f"Stopped: {result.stopped}. Accepted {done} of {len(result.rows)} tried."
            f" Log: {Path(cfg.out_dir) / 'convert-log.jsonl'}"
        )
        if cfg.webhook_url:
            try:
                notify.send(cfg.webhook_url, f"LomboShrink: {done} files converted ({result.stopped}).")
            except notify.NotifyError as e:
                print(f"warning: {e}", file=sys.stderr)
    return 0


def _review(cfg: Config) -> int:
    log = Path(cfg.out_dir) / "convert-log.jsonl"
    if not log.exists():
        print("No log yet.")
        return 0
    records = []
    for raw in log.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(raw))
        except ValueError:
            continue
    _, accepted = core.resume_state(records)
    waiting = [(p, b) for p, b in accepted.items() if batch.output_path(Path(cfg.out_dir), p).exists()]
    for path, size in waiting:
        print(f"{path}  {size / 1024**2:,.0f} MB")
    print(f"{len(waiting)} converted files still on this PC, {sum(b for _, b in waiting) / 1024**3:.1f} GB")
    return 0


def _swap(cfg: Config, manifest: str, result: str, *, apply: bool) -> int:
    try:
        entries = core.parse_manifest(json.loads(Path(manifest).read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as e:
        print(f"[FAIL] manifest: {e}", file=sys.stderr)
        return 2
    if not cfg.library_dir and not cfg.branches:
        print("[FAIL] set LOMBO_SHRINK_LIBRARY_DIR (or BRANCHES) on the server", file=sys.stderr)
        return 2
    if apply and cfg.dry_run:
        print(
            "Refusing to apply: LOMBO_SHRINK_DRY_RUN is true on this machine."
            " Set it to false to allow real swaps. Nothing changed.",
            file=sys.stderr,
        )
        return 1
    is_open = session.expiry(Path(cfg.session_file)) is not None
    if apply and cfg.require_session and not is_open:
        print("No conversion session is open: run `lombo-shrink session open` first. Nothing changed.", file=sys.stderr)
    elif not apply and cfg.require_session and not is_open:
        print("warning: no conversion session is open (--apply would refuse)", file=sys.stderr)
    refresh = None
    if cfg.jellyfin_url and cfg.jellyfin_api_key:
        refresh = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key).refresh_library
    results = swap.run_swap(cfg, entries, apply=apply, session_open=is_open, refresh=refresh)
    for r in results:
        print(f"{r.status:<8} {r.path}{'  -- ' + r.reason if r.reason else ''}{'  (' + r.note + ')' if r.note else ''}")
    payload: dict[str, Any] = {
        "files": [{"path": r.path, "status": r.status, "reason": r.reason, "note": r.note} for r in results]
    }
    if not apply:
        payload["dry_run"] = True
    tmp = Path(result + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(result)
    return 0 if all(r.ok for r in results) else 1


def _session(cfg: Config, action: str, hours: int) -> int:
    path = Path(cfg.session_file)
    if action == "open":
        try:
            until = session.open_session(path, hours)
        except ValueError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            return 1
        print(f"Conversion session open until {datetime.fromtimestamp(until):%a %H:%M}.")
    elif action == "close":
        session.close_session(path)
        print("Conversion session closed.")
    else:
        until_ts = session.expiry(path)
        print(f"Open until {datetime.fromtimestamp(until_ts):%a %H:%M}." if until_ts else "Closed.")
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    # Progress lines must appear as they happen, also when output is piped or captured (Python buffers it otherwise).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(line_buffering=True)
    p = argparse.ArgumentParser(prog="lombo-shrink", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="say why files are left out of the list")
    # The same flag after the subcommand too (`candidates -v`); SUPPRESS keeps a `-v` given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    cand = sub.add_parser("candidates", parents=[common])
    cand.add_argument("--out")
    sub.add_parser("plan", parents=[common]).add_argument("--list")
    run = sub.add_parser("run", parents=[common])
    run.add_argument("--list")
    run.add_argument("--redo", action="store_true", help="also redo files already accepted, rejected or skipped")
    sub.add_parser("review")
    sw = sub.add_parser("swap")
    sw.add_argument("--manifest", required=True)
    sw.add_argument("--result", required=True)
    sw.add_argument("--apply", action="store_true")
    ses = sub.add_parser("session")
    ses.add_argument("action", choices=["open", "close", "status"])
    ses.add_argument("hours", nargs="?", type=int, default=8)
    args = p.parse_args(argv)

    try:
        cfg = from_env(dict(os.environ) if env is None else env)
    except ConfigError as e:
        print(f"[FAIL] settings: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "doctor":
            return doctor(cfg)
        if args.cmd == "candidates":
            return _candidates(cfg, args.out, args.verbose)
        if args.cmd == "plan":
            return _batch(cfg, args.list, dry=True, verbose=args.verbose)
        if args.cmd == "run":
            return _batch(cfg, args.list, dry=cfg.dry_run, redo=args.redo, verbose=args.verbose)
        if args.cmd == "review":
            return _review(cfg)
        if args.cmd == "swap":
            return _swap(cfg, args.manifest, args.result, apply=args.apply)
        return _session(cfg, args.action, args.hours)
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. The unfinished file was removed; run again to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
