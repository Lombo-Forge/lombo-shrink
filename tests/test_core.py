import copy

import pytest

from conftest import video_probe
from lombo_shrink import core


def test_num_and_fps_are_forgiving():
    assert core.num("12.5") == 12.5
    assert core.num(None) == 0.0
    assert core.num("nan") == 0.0
    assert core.fps("24000/1001") == pytest.approx(23.976, abs=0.001)
    assert core.fps("0/0") == 0.0
    assert core.fps("25") == 25.0


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, None),
        ({"codec": "hevc"}, "already HEVC"),
        ({"field_order": "tt"}, "interlaced (tt)"),
        ({"color_transfer": "smpte2084"}, "HDR (smpte2084)"),
        ({"pix_fmt": "yuv444p"}, "pixel format yuv444p"),
        ({"bit_rate": 200_000}, "low bitrate"),
    ],
)
def test_skip_reason(change, expected):
    p = video_probe(**change)
    got = core.skip_reason(p, 10_000_000)
    assert (got is None) if expected is None else (got or "").startswith(expected)


def test_skip_reason_other_cases():
    p = video_probe()
    assert core.skip_reason(copy.deepcopy(p), 1, allow_hevc=True, min_bpp=0) is None
    assert core.skip_reason({"streams": []}, 1) == "no video stream"
    two = copy.deepcopy(p)
    two["streams"].append(copy.deepcopy(p["streams"][0]))
    assert "more than one" in (core.skip_reason(two, 1) or "")
    cover = copy.deepcopy(p)
    cover["streams"].append({**p["streams"][0], "disposition": {"attached_pic": 1}})
    assert core.skip_reason(cover, 10_000_000) is None  # cover art is not a second video
    dv = copy.deepcopy(p)
    dv["streams"][0]["side_data_list"] = [{"side_data_type": "DOVI configuration record"}]
    assert core.skip_reason(dv, 1) == "Dolby Vision"
    sub = copy.deepcopy(p)
    sub["streams"][2]["codec_name"] = "eia_608"
    assert "unsupported subtitle" in (core.skip_reason(sub, 1) or "")


def test_bits_per_pixel_fallbacks():
    p = video_probe()
    del p["streams"][0]["bit_rate"]
    assert core.bits_per_pixel(p, p["streams"][0], 1) == pytest.approx(0.17, abs=0.01)  # from format minus audio guess
    del p["format"]["bit_rate"]
    assert core.bits_per_pixel(p, p["streams"][0], 100_000_000) == pytest.approx(0.16, abs=0.01)  # from size / duration
    assert core.bits_per_pixel({"format": {}}, {}, 1) is None


def test_pick_encoder():
    have = {"hevc_amf", "libx265"}
    assert core.pick_encoder("auto", have) == "amf"
    assert core.pick_encoder("nvenc", have) is None
    assert core.pick_encoder("x265", have) == "x265"
    assert core.pick_encoder("auto", set()) is None


@pytest.mark.parametrize("name", list(core.ENCODER_CODECS))
def test_encoder_args_name_the_codec_and_cq(name):
    args = core.encoder_args(name, 26)
    assert core.ENCODER_CODECS[name] in args
    assert "10" in " ".join(args) or "p010le" in args or "yuv420p10le" in args  # always 10-bit output
    with pytest.raises(ValueError, match="unknown"):
        core.encoder_args("nope", 26)


def test_encode_command_keeps_streams_and_flags():
    p = video_probe(color_transfer="bt709", color_primaries="unknown")
    p["streams"][2]["codec_name"] = "mov_text"
    cmd = core.encode_command("ffmpeg", "in.mkv", "out.partial.mkv", p, "nvenc", 26, ("-x", "1"))
    text = " ".join(cmd)
    assert cmd[-1] == "out.partial.mkv"
    assert "-hwaccel cuda -i in.mkv" in text
    assert "-map 0:V -map 0:a? -map 0:s? -map 0:t? -map_chapters 0 -c copy" in text
    assert "-c:s srt" in text
    assert "-disposition:s:0 -default" in text  # the source subtitle is not default: say so
    assert "-disposition:a:0" not in text  # audio is default in the source
    assert "-color_trc bt709" in text
    assert "color_primaries" not in text  # unknown is not passed on
    assert "-x 1" in text
    assert "-hwaccel" not in core.encode_command("ffmpeg", "i", "o", p, "nvenc", 26, hw_decode=False)


def good_pair():
    src = video_probe()
    out = video_probe("hevc", 3_000_000)
    return src, out


def accept(src, out, **kw):
    args = {
        "src_size": 1000,
        "out_size": 500,
        "min_saving_pct": 20,
        "src_packets": 100,
        "out_packets": 100,
        "decode_error": "",
    }
    return core.accept_problems(src, out, **{**args, **kw})


def test_accept_good_output():
    assert accept(*good_pair()) == []


def test_accept_reports_every_broken_rule():
    src, out = good_pair()
    assert "saving 10.0% is below the 20% bar" in accept(src, out, out_size=900)
    assert "output is unreadable by ffprobe" in accept(src, None)
    assert any("packet count" in p for p in accept(src, out, out_packets=99))
    assert any("could not count" in p for p in accept(src, out, src_packets=-1))
    assert any("decode check failed" in p for p in accept(src, out, decode_error="bad frame"))
    broken = copy.deepcopy(out)
    broken["streams"][0].update(width=1280, height=720, display_aspect_ratio="4:3", r_frame_rate="30/1")
    broken["streams"][1]["tags"]["language"] = "fra"
    broken["streams"][1]["disposition"] = {"default": 0}
    broken["streams"][2]["codec_name"] = "ass"
    broken["format"]["duration"] = "110"
    broken["chapters"] = []
    text = "; ".join(accept(src, broken))
    for piece in (
        "size differs",
        "aspect ratio differs",
        "frame rate differs",
        "audio 0 language differs",
        "audio 0 default/forced differs",
        "subtitle 0 codec differs",
        "duration differs",
        "chapter count differs",
    ):
        assert piece in text


def test_accept_allows_mov_text_to_subrip_and_compares_sample_aspect():
    src, out = good_pair()
    src["streams"][2]["codec_name"] = "mov_text"
    assert accept(src, out) == []
    for p in (src, out):
        del p["streams"][0]["display_aspect_ratio"]
    out["streams"][0]["sample_aspect_ratio"] = "4:3"
    assert any("sample aspect ratio" in p for p in accept(src, out))
    out["streams"].pop()  # a missing subtitle track
    assert any("subtitle count differs" in p for p in accept(src, out))


def test_resume_rules():
    records = [
        {"path": "a", "status": "accepted", "out_bytes": 5},
        {"path": "b", "status": "failed"},
        {"path": "c", "status": "rejected"},
        {"path": "a", "status": "failed"},  # a later failure makes it retryable
        {"path": "d", "status": "accepted", "out_bytes": 7},
        {"path": "d", "status": "skipped"},
    ]
    done, accepted = core.resume_state(records)
    assert done == {"c": "rejected", "d": "skipped"}
    assert accepted == {}
    assert core.should_process("new", done, redo=False)
    assert not core.should_process("c", done, redo=False)
    assert core.should_process("c", done, redo=True)
    assert not core.should_process("m", {"m": "missing"}, redo=True)


def test_stop_reason_and_room():
    assert core.stop_reason(tried=0, limit=0, produced_bytes=0, max_gb=0, stop_file=False) is None
    assert "limit of 2" in (core.stop_reason(tried=2, limit=2, produced_bytes=0, max_gb=0, stop_file=False) or "")
    assert core.stop_reason(tried=0, limit=0, produced_bytes=0, max_gb=0, stop_file=True) == "STOP file found"
    assert "GB" in (core.stop_reason(tried=0, limit=0, produced_bytes=3 * 1024**3, max_gb=2, stop_file=False) or "")
    assert core.has_room(30 * 1024**3, 1024**3, 20)
    assert not core.has_room(20 * 1024**3, 1024**3, 20)
    assert [i["path"] for i in core.sort_candidates([{"path": "s", "size": 1}, {"path": "b", "size": 9}])] == ["b", "s"]


def test_format_report():
    rows = [
        {"title": "A", "status": "accepted", "src": 2 * 1024**3, "out": 1024**3, "cq": 26, "ship": "swapped"},
        {"title": "B", "status": "accepted", "src": 1024**3, "out": 512 * 1024**2, "cq": 26, "ship": ""},
        {"title": "C", "status": "skipped", "msg": "already HEVC"},
        {"title": "D", "status": "skipped", "msg": "already HEVC"},
    ]
    text = core.format_report(rows, "Report")
    assert "50% saved" in text
    assert "GB saved: 1.5" in text
    assert "Shipped to the server: 1" in text
    assert "Kept on this PC: 1" in text
    assert "2 x already HEVC" in text


@pytest.mark.parametrize("bad", ["", "/abs", "a/../b", "dir/", "a\0b", "..\\x"])
def test_safe_relative_rejects(bad):
    assert not core.safe_relative(bad)
    assert core.safe_relative("movies/Foo (2019)/Foo.mkv")


def test_parse_manifest():
    ok = {
        "path": "m/Foo.mp4",
        "staged": "m/Foo.hevc.mkv",
        "sha256": "A" * 64,
        "out_bytes": 5,
        "src_bytes": 9,
        "src_mtime": 1700000000,
    }
    (entry,) = core.parse_manifest([ok])
    assert entry.sha256 == "a" * 64
    assert entry.src_mtime == 1700000000.0
    assert core.parse_manifest([{**ok, "src_mtime": "2026-11-13T22:26:59"}])[0].src_mtime is not None
    assert core.parse_manifest([{**ok, "src_mtime": "garbage"}])[0].src_mtime is None
    assert core.parse_manifest([{**ok, "src_mtime": ""}])[0].src_mtime is None
    for bad in (
        {**ok, "path": "../x"},
        {**ok, "staged": "/x"},
        {**ok, "sha256": "zz"},
        {**ok, "out_bytes": "x"},
        {**ok, "src_bytes": 0},
    ):
        with pytest.raises(ValueError, match=r"."):
            core.parse_manifest([bad])
    with pytest.raises(ValueError, match="list"):
        core.parse_manifest({})
    with pytest.raises(ValueError, match="object"):
        core.parse_manifest([1])


def test_swap_helpers():
    assert core.final_relative("a/b.mkv") == "a/b.mkv"
    assert core.final_relative("a/b.MP4") == "a/b.mkv"
    assert core.final_relative("noext") == "noext.mkv"
    assert core.mtime_matches(100.0, 101.5)
    assert not core.mtime_matches(100.0, 110.0)
    assert core.mtime_matches(100.0, None)
    assert core.room_ok(10, 5, 5)
    assert not core.room_ok(10, 6, 5)
