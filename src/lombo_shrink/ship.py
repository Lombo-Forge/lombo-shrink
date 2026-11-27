"""PC to server: copy an accepted file to the server's staging folder, run `lombo-shrink swap --apply` there
over ssh, and delete the PC copy only when the server says it was swapped.

Only ssh and scp are used (key login, no prompts). Every remote argument is quoted. A failure affects only
that file: it stays on this PC and the reason is returned.
"""

import hashlib
import json
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .config import Config

OPTS = ["-o", "BatchMode=yes"]
SWAP_TIMEOUT = 6 * 3600  # the server hashes and copies the whole file: a big film on a slow disk takes long


class ShipError(Exception):
    pass


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShipError(f"{cmd[0]}: {e}") from e
    return done


def _ssh(cfg: Config, remote: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return _run([*cfg.ssh_command, *OPTS, cfg.ship_target, remote], timeout)


def _scp(cfg: Config, local: Path, remote: str) -> None:
    done = _run([*cfg.scp_command, *OPTS, str(local), f"{cfg.ship_target}:{remote}"], 24 * 3600)
    if done.returncode != 0:
        raise ShipError(f"copy to the server failed: {done.stderr.strip() or done.returncode}")


def check_server(cfg: Config) -> str | None:
    """None when the server answers and has lombo-shrink; otherwise the problem."""
    try:
        done = _ssh(cfg, f"command -v {shlex.quote(cfg.remote_command.split()[0])}", 30)
    except ShipError as e:
        return str(e)
    if done.returncode != 0:
        return f"cannot reach {cfg.ship_target} over ssh, or `{cfg.remote_command}` is not installed there"
    return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def ship_file(cfg: Config, item: dict[str, Any], out: Path, src_mtime: float, out_dir: Path) -> tuple[str, str]:
    """Returns (status, reason): swapped, skipped or failed. On `swapped` the PC copy is deleted."""
    try:
        staged = out.relative_to(out_dir).as_posix()
        sid = uuid.uuid4().hex[:12]
        stage = cfg.remote_stage_dir.rstrip("/")
        manifest = [
            {
                "path": item["path"],
                "staged": staged,
                "sha256": sha256_of(out),
                "out_bytes": out.stat().st_size,
                "src_bytes": int(item["size"]),
                "src_mtime": src_mtime,
            }
        ]
        q = shlex.quote
        prep = _ssh(cfg, f"mkdir -p {q(stage + '/uploads')} {q(stage + '/manifests')} {q(stage + '/results')}", 60)
        if prep.returncode != 0:
            raise ShipError(f"could not prepare the staging folder: {prep.stderr.strip()}")
        # Upload under a plain name first, then move: odd characters in titles never reach scp.
        _scp(cfg, out, f"{stage}/uploads/{sid}.part")
        target = f"{stage}/{staged}"
        moved = _ssh(
            cfg,
            f"mkdir -p {q(target.rsplit('/', 1)[0])} && mv -f {q(stage + '/uploads/' + sid + '.part')} {q(target)}",
            600,
        )
        if moved.returncode != 0:
            raise ShipError(f"could not place the upload: {moved.stderr.strip()}")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / f"{sid}.json"
            local.write_text(json.dumps(manifest), encoding="utf-8")
            _scp(cfg, local, f"{stage}/manifests/{sid}.json")
        result_path = f"{stage}/results/{sid}.json"
        keep = " --keep-originals" if cfg.keep_originals else ""
        swap = _ssh(
            cfg,
            f"{cfg.remote_command} swap --apply --manifest {q(f'{stage}/manifests/{sid}.json')}"
            f" --result {q(result_path)}{keep}",
            SWAP_TIMEOUT,
        )
        read = _ssh(cfg, f"cat {q(result_path)}", 60)
        try:
            entry = json.loads(read.stdout)["files"][0]
            status, reason = str(entry["status"]), str(entry.get("reason", ""))
        except (ValueError, KeyError, IndexError, TypeError):
            raise ShipError(
                f"the server gave no readable result (exit {swap.returncode}):"
                f" {swap.stderr.strip() or swap.stdout.strip()}"
            ) from None
    except (ShipError, OSError) as e:
        return "failed", str(e)
    if status == "swapped" or (status == "skipped" and reason == "already swapped"):
        out.unlink(missing_ok=True)
        parent = out.parent
        while parent != out_dir and out_dir in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return "swapped", ""
    return status, reason
