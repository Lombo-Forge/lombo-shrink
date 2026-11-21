"""A conversion session is a lock file with an expiry. While it is open, `swap --apply` is allowed to replace
files, and other tools (a nightly mover, for example) can check the same file and stand down.

The file is JSON: {"version": 1, "owner": "lombo-shrink", "opened": <epoch seconds>, "expires": <epoch seconds>}. It
expires by itself. Other tools read "version", "owner" and "expires" (the contract in the hub repository).
"""

import json
import time
from pathlib import Path


def open_session(path: Path, hours: int, now: float | None = None) -> float:
    if not 1 <= hours <= 72:
        raise ValueError("HOURS must be 1 to 72")
    opened = time.time() if now is None else now
    expires = opened + hours * 3600
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = {"version": 1, "owner": "lombo-shrink", "opened": int(opened), "expires": int(expires)}
    tmp.write_text(json.dumps(body) + "\n")
    tmp.replace(path)
    return expires


def close_session(path: Path) -> None:
    path.unlink(missing_ok=True)


def expiry(path: Path, now: float | None = None) -> float | None:
    """When the open session ends, or None when there is no open session."""
    try:
        expires = float(json.loads(path.read_text())["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return expires if expires > (time.time() if now is None else now) else None
