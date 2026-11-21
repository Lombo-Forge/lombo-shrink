import json
import re
from pathlib import Path

import pytest

from conftest import make_bins, unix_only, video_probe
from lombo_shrink import notify, session
from lombo_shrink.cli import main
from lombo_shrink.config import ConfigError, from_env
from lombo_shrink.jellyfin import Jellyfin, JellyfinError

ROOT = Path(__file__).resolve().parent.parent


def test_config_defaults_are_safe(env):
    cfg = from_env(env)
    assert cfg.dry_run is True
    assert cfg.cq == 26
    assert cfg.encoder == "auto"
    assert cfg.require_session is True
    assert cfg.keep_originals is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ENCODER", "turbo"),
        ("CQ", "soon"),
        ("CQ", "99"),
        ("CQ", "30"),  # above the default MAX_CQ of 28
        ("MIN_SAVING_PCT", "0"),
        ("WEBHOOK_URL", "ftp://x"),
        ("JELLYFIN_URL", "jellyfin:8096"),
        ("SSH_COMMAND", "ssh 'unclosed"),
        ("SCP_COMMAND", " "),
    ],
)
def test_config_rejects_bad_values_and_names_the_setting(env, name, value):
    with pytest.raises(ConfigError, match=f"LOMBO_SHRINK_{name}|LOMBO_SHRINK_CQ"):
        from_env({**env, f"LOMBO_SHRINK_{name}": value})


def test_config_jellyfin_needs_a_real_key(env):
    with pytest.raises(ConfigError, match="API_KEY"):
        from_env(
            {**env, "LOMBO_SHRINK_JELLYFIN_URL": "http://jellyfin:8096", "LOMBO_SHRINK_JELLYFIN_API_KEY": "changeme"}
        )


def test_env_example_stays_in_sync_with_config():
    used = set(
        re.findall(
            r'(?:get|number|url_or_none|command)\("([A-Z_]+)"', (ROOT / "src/lombo_shrink/config.py").read_text()
        )
    )
    documented = set(re.findall(r"^#?\s*LOMBO_SHRINK_([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert used == documented, f"only in config: {used - documented}; only in .env.example: {documented - used}"


def test_client_sends_the_key_and_reports_failures(fake):
    jf = Jellyfin(fake.url, "good-key")
    assert jf.info()["ServerName"] == "Fake"
    jf.refresh_library()
    assert fake.posts()[0]["path"] == "/Library/Refresh"
    with pytest.raises(JellyfinError, match="HTTP 401"):
        Jellyfin(fake.url, "wrong").info()
    with pytest.raises(JellyfinError, match="Refresh"):
        Jellyfin("http://127.0.0.1:1", "k", timeout=1).refresh_library()
    with pytest.raises(JellyfinError, match=r"."):
        Jellyfin("http://127.0.0.1:1", "k", timeout=1).public_info()


def test_notify_posts_json(fake):
    notify.send(fake.url + "/hook", "hello")
    assert fake.posts()[0]["body"] == {"content": "hello", "text": "hello"}
    with pytest.raises(notify.NotifyError):
        notify.send("http://127.0.0.1:1/hook", "x", timeout=1)


def test_session_lock(tmp_path):
    lock = tmp_path / "s" / "session.json"
    assert session.expiry(lock) is None
    until = session.open_session(lock, 2, now=1000)
    assert until == 1000 + 7200
    assert session.expiry(lock, now=2000) == until
    assert session.expiry(lock, now=until + 1) is None  # expires by itself
    body = json.loads(lock.read_text())
    assert (body["version"], body["owner"], body["expires"]) == (1, "lombo-shrink", until)
    session.close_session(lock)
    assert session.expiry(lock) is None
    with pytest.raises(ValueError, match="1 to 72"):
        session.open_session(lock, 0)
    lock.write_text("not json")
    assert session.expiry(lock) is None


def test_verbose_flag_works_before_and_after_the_subcommand():
    bad = {"LOMBO_SHRINK_CQ": "x"}  # a config error (exit 2) proves the arguments themselves parsed
    for argv in (["-v", "plan"], ["plan", "-v"], ["candidates", "-v"], ["run", "-v"], ["--verbose", "plan"]):
        assert main(argv, bad) == 2, argv


def test_session_command(env, capsys):
    assert main(["session", "status"], env) == 0
    assert capsys.readouterr().out.strip() == "Closed."
    assert main(["session", "open", "1"], env) == 0
    assert "open until" in capsys.readouterr().out
    assert main(["session", "status"], env) == 0
    assert capsys.readouterr().out.startswith("Open until")
    assert main(["session", "open", "99"], env) == 1
    assert main(["session", "close"], env) == 0


def test_bad_settings_exit_2(capsys):
    assert main(["plan"], {"LOMBO_SHRINK_CQ": "x"}) == 2
    assert "LOMBO_SHRINK_CQ" in capsys.readouterr().err


def test_commands_need_a_library(env, capsys):
    env = {**env, "LOMBO_SHRINK_LIBRARY_DIR": "/does/not/exist"}
    assert main(["plan"], env) == 1
    assert main(["candidates"], env) == 1
    assert "LIBRARY_DIR" in capsys.readouterr().err


@unix_only
def test_doctor_passes_and_fails(env, tmp_path, fake, capsys):
    (tmp_path / "lib").mkdir()
    ffmpeg, ffprobe = make_bins(tmp_path / "bin", video_probe(), video_probe("hevc"))
    good = {
        **env,
        "LOMBO_SHRINK_FFMPEG": ffmpeg,
        "LOMBO_SHRINK_FFPROBE": ffprobe,
        "LOMBO_SHRINK_JELLYFIN_URL": fake.url,
        "LOMBO_SHRINK_JELLYFIN_API_KEY": "good-key",
    }
    assert main(["doctor"], good) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "hevc_nvenc encodes a test clip" in out
    bad = {
        **good,
        "LOMBO_SHRINK_JELLYFIN_API_KEY": "wrong",
        "LOMBO_SHRINK_FFMPEG": "/no/ffmpeg",
        "LOMBO_SHRINK_OUT_DIR": str(tmp_path / "lib" / "out"),
    }
    assert main(["doctor"], bad) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Jellyfin: /System/Info: HTTP 401" in out
    assert "[FAIL] output folder: OUT_DIR is inside the library" in out
    assert "[FAIL] ffmpeg" in out


@unix_only
def test_doctor_reports_an_encoder_that_cannot_encode(env, tmp_path, capsys):
    (tmp_path / "lib").mkdir()
    ffmpeg, ffprobe = make_bins(tmp_path / "bin", video_probe(), video_probe("hevc"))
    script = Path(ffmpeg)
    script.write_text(
        script.read_text().replace(
            " *lavfi*) exit 0;;", " *lavfi*) echo 'No NVENC capable devices found' >&2; exit 1;;"
        )
    )
    assert main(["doctor"], {**env, "LOMBO_SHRINK_FFMPEG": ffmpeg, "LOMBO_SHRINK_FFPROBE": ffprobe}) == 1
    assert "hevc_nvenc failed: No NVENC capable devices found" in capsys.readouterr().out


def test_review_lists_unshipped_files(env, tmp_path, capsys):
    assert main(["review"], env) == 0
    assert "No log yet" in capsys.readouterr().out
    out = tmp_path / "out"
    (out / "m").mkdir(parents=True)
    (out / "m" / "A.hevc.mkv").write_bytes(b"x" * 10)
    (out / "convert-log.jsonl").write_text(
        json.dumps({"path": "m/A.mkv", "status": "accepted", "out_bytes": 10})
        + "\n"
        + "{half a line\n"
        + json.dumps({"path": "m/B.mkv", "status": "accepted", "out_bytes": 10})
        + "\n"  # already shipped: file gone
    )
    assert main(["review"], env) == 0
    text = capsys.readouterr().out
    assert "m/A.mkv" in text
    assert "m/B.mkv" not in text
    assert "1 converted files still on this PC" in text


def test_ctrl_c_exits_cleanly_with_code_130(env, capsys, monkeypatch):
    from lombo_shrink import cli

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_candidates", boom)
    assert cli.main(["candidates"], env) == 130
    assert "Interrupted" in capsys.readouterr().err
