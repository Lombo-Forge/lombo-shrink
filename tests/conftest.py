"""Shared helpers: a fake Jellyfin/webhook on localhost, fake ffmpeg/ffprobe scripts, probe builders.

No test needs a real video, a graphics card or a real server. Tests that move files use tmp_path only.
"""

import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses shell-script stand-ins for ffmpeg and ssh")


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.key = "good-key"
        self.items: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, payload=None):
                data = json.dumps(payload if payload is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                if self.path == "/System/Info/Public":
                    return self._reply(200, {"ServerName": "Fake", "Version": "0.0.0"})
                if self.headers.get("Authorization") != f'MediaBrowser Token="{outer.key}"':
                    return self._reply(401)
                if self.path == "/System/Info":
                    return self._reply(200, {"ServerName": "Fake"})
                if self.path.startswith("/Items?"):
                    return self._reply(200, {"Items": outer.items})
                return self._reply(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"method": "POST", "path": self.path, "body": json.loads(body or b"{}")})
                self._reply(204)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()


def video_probe(codec: str = "h264", bit_rate: int = 8_000_000, duration: float = 100, **video: Any) -> dict[str, Any]:
    v = {
        "index": 0,
        "codec_type": "video",
        "codec_name": codec,
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "avg_frame_rate": "24/1",
        "r_frame_rate": "24/1",
        "bit_rate": str(bit_rate),
        "field_order": "progressive",
        "display_aspect_ratio": "16:9",
        "disposition": {"default": 1},
        **video,
    }
    audio = {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "eac3",
        "tags": {"language": "eng"},
        "disposition": {"default": 1},
    }
    sub = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "subrip",
        "tags": {"language": "eng"},
        "disposition": {"default": 0},
    }
    return {
        "streams": [v, audio, sub],
        "format": {"duration": str(duration), "bit_rate": str(bit_rate + 640000)},
        "chapters": [{}, {}],
    }


def make_bins(folder: Path, src: dict[str, Any], out: dict[str, Any]) -> tuple[str, str]:
    """Write fake ffmpeg and ffprobe scripts. ffprobe answers `out` for *.partial.mkv and `src` otherwise.
    Behaviour switches come from environment variables (FAKE_OUT_BYTES, FAKE_FFMPEG_FAIL, FAKE_DECODE_ERR)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "src.json").write_text(json.dumps(src))
    (folder / "out.json").write_text(json.dumps(out))
    ffmpeg = folder / "ffmpeg"
    ffmpeg.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        ' *-encoders*) echo " V....D hevc_nvenc  NVIDIA NVENC hevc encoder"; exit 0;;\n'
        ' *-version*) echo "ffmpeg version fake"; exit 0;;\n'
        ' *-xerror*) if [ -n "$FAKE_DECODE_ERR" ]; then echo "$FAKE_DECODE_ERR" >&2; exit 1; fi; exit 0;;\n'
        " *lavfi*) exit 0;;\n"
        "esac\n"
        'if [ -n "$FAKE_FFMPEG_FAIL" ]; then echo boom >&2; exit 1; fi\n'
        "for a; do last=$a; done\n"
        'head -c "${FAKE_OUT_BYTES:-500}" /dev/zero > "$last"\n'
    )
    ffprobe = folder / "ffprobe"
    ffprobe.write_text(
        "#!/bin/sh\n"
        'D=$(dirname "$0")\n'
        'case "$*" in *-version*) echo "ffprobe version fake"; exit 0;; *-count_packets*) echo 100; exit 0;; esac\n'
        "for a; do last=$a; done\n"
        'case "$last" in *.partial.mkv) cat "$D/out.json";; *) cat "$D/src.json";; esac\n'
    )
    for f in (ffmpeg, ffprobe):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(ffmpeg), str(ffprobe)


@pytest.fixture
def env(fake, tmp_path):
    return {
        "LOMBO_SHRINK_LIBRARY_DIR": str(tmp_path / "lib"),
        "LOMBO_SHRINK_OUT_DIR": str(tmp_path / "out"),
        "LOMBO_SHRINK_STAGE_DIR": str(tmp_path / "stage"),
        "LOMBO_SHRINK_WEBHOOK_URL": fake.url + "/hook",
    }
