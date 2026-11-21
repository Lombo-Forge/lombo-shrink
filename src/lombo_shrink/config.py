"""Settings, read once from environment variables (see .env.example).

Every setting has a LOMBO_SHRINK_ prefix so it never collides with another program's variables.
A bad value stops the app at start-up with a message that names the variable.
"""

import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PREFIX = "LOMBO_SHRINK_"
ENCODER_CHOICES = ("auto", "nvenc", "amf", "qsv", "videotoolbox", "x265")


class ConfigError(Exception):
    """A setting is missing or invalid. The message says which one and what to do."""


@dataclass(frozen=True)
class Config:
    dry_run: bool
    library_dir: str  # the library as this machine sees it (a mount on the PC, the real folder on the server)
    out_dir: str  # where converted files are written on the PC; never inside the library
    ffmpeg: str
    ffprobe: str
    encoder: str  # auto, nvenc, amf, qsv, videotoolbox or x265
    cq: int  # constant quality; higher is smaller and harsher
    max_cq: int
    min_saving_pct: float
    min_bpp: float
    spare_gb: float
    max_gb: float
    limit: int
    stall_minutes: int
    allow_hevc: bool
    extra_args: tuple[str, ...]
    skip_hardlinked: bool
    # ---- server side (swap, session)
    stage_dir: str
    branches: tuple[str, ...]  # library folders on each disk; empty means library_dir itself
    keep_originals: bool
    margin_gb: float
    require_session: bool
    session_file: str
    # ---- PC to server (ship)
    ship_target: str  # user@host; empty means do not ship
    ssh_command: tuple[str, ...]
    scp_command: tuple[str, ...]
    remote_command: str  # how to start lombo-shrink on the server
    remote_stage_dir: str
    # ---- optional
    jellyfin_url: str | None
    jellyfin_api_key: str | None
    webhook_url: str | None


def _truthy(value: str, name: str) -> bool:
    """Strict on/off: a typo such as DRY_RUN=treu is an error, never a silent 'off'."""
    word = value.strip().lower()
    if word in {"1", "true", "yes", "on"}:
        return True
    if word in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{PREFIX}{name} must be true or false (got {value!r})")


def from_env(env: dict[str, str]) -> Config:
    def get(name: str, default: str = "") -> str:
        return (env.get(PREFIX + name) or default).strip()

    def number(name: str, default: str, *, kind: type[int] | type[float], minimum: float, maximum: float) -> float:
        raw = get(name, default)
        try:
            value = kind(raw)
        except ValueError:
            raise ConfigError(f"{PREFIX}{name} must be a number (got {raw!r})") from None
        if not minimum <= value <= maximum:
            raise ConfigError(f"{PREFIX}{name} must be between {minimum:g} and {maximum:g} (got {raw})")
        return value

    def url_or_none(name: str) -> str | None:
        value = get(name).rstrip("/")
        if value and (urlparse(value).scheme not in {"http", "https"} or not urlparse(value).netloc):
            raise ConfigError(f"{PREFIX}{name} must be a full http:// or https:// URL (or empty), got {value!r}")
        return value or None

    def command(name: str, default: str) -> tuple[str, ...]:
        try:
            parts = tuple(shlex.split(get(name, default)))
        except ValueError as e:
            raise ConfigError(f"{PREFIX}{name} is not a valid command: {e}") from None
        if not parts:
            raise ConfigError(f"{PREFIX}{name} must not be empty")
        return parts

    encoder = get("ENCODER", "auto").lower()
    if encoder not in ENCODER_CHOICES:
        raise ConfigError(f"{PREFIX}ENCODER must be one of {', '.join(ENCODER_CHOICES)} (got {encoder!r})")

    cq = int(number("CQ", "26", kind=int, minimum=10, maximum=40))
    max_cq = int(number("MAX_CQ", "28", kind=int, minimum=10, maximum=40))
    if cq > max_cq:
        raise ConfigError(f"{PREFIX}CQ ({cq}) is above {PREFIX}MAX_CQ ({max_cq}); raise MAX_CQ or lower CQ")

    jf_url = url_or_none("JELLYFIN_URL")
    jf_key = get("JELLYFIN_API_KEY") or None
    if jf_url and (not jf_key or jf_key == "changeme"):
        raise ConfigError(f"{PREFIX}JELLYFIN_API_KEY is not set. Create one in Jellyfin: Dashboard > API Keys.")

    stage = get("STAGE_DIR", str(Path.home() / "lombo-shrink-staging"))
    return Config(
        dry_run=_truthy(get("DRY_RUN", "true") or "true", "DRY_RUN"),
        library_dir=get("LIBRARY_DIR"),
        out_dir=get("OUT_DIR", "converted"),
        ffmpeg=get("FFMPEG", "ffmpeg"),
        ffprobe=get("FFPROBE", "ffprobe"),
        encoder=encoder,
        cq=cq,
        max_cq=max_cq,
        min_saving_pct=number("MIN_SAVING_PCT", "20", kind=float, minimum=1, maximum=90),
        min_bpp=number("MIN_BPP", "0.08", kind=float, minimum=0, maximum=2),
        spare_gb=number("SPARE_GB", "20", kind=float, minimum=0, maximum=100000),
        max_gb=number("MAX_GB", "0", kind=float, minimum=0, maximum=1000000),
        limit=int(number("LIMIT", "0", kind=int, minimum=0, maximum=1000000)),
        stall_minutes=int(number("STALL_MINUTES", "5", kind=int, minimum=1, maximum=240)),
        allow_hevc=_truthy(get("ALLOW_HEVC", "false"), "ALLOW_HEVC"),
        extra_args=tuple(shlex.split(get("EXTRA_ENCODER_ARGS"))),
        skip_hardlinked=_truthy(get("SKIP_HARDLINKED", "true"), "SKIP_HARDLINKED"),
        stage_dir=stage,
        branches=tuple(p for p in get("BRANCHES").split(":") if p),
        keep_originals=_truthy(get("KEEP_ORIGINALS", "false"), "KEEP_ORIGINALS"),
        margin_gb=number("MARGIN_GB", "20", kind=float, minimum=0, maximum=100000),
        require_session=_truthy(get("REQUIRE_SESSION", "true") or "true", "REQUIRE_SESSION"),
        session_file=get("SESSION_FILE", str(Path(stage) / "session.json")),
        ship_target=get("SHIP_TARGET"),
        ssh_command=command("SSH_COMMAND", "ssh"),
        scp_command=command("SCP_COMMAND", "scp"),
        remote_command=get("REMOTE_COMMAND", "lombo-shrink"),
        remote_stage_dir=get("REMOTE_STAGE_DIR", "lombo-shrink-staging"),
        jellyfin_url=jf_url,
        jellyfin_api_key=jf_key,
        webhook_url=url_or_none("WEBHOOK_URL"),
    )
