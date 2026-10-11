"""Output options (codec, pixel format, container) and probe parsing."""

import json
import os

import pytest

import stylet_core as core

INFO = {"width": 4096, "height": 2160, "codec": "prores"}


def plan(settings, entry=None):
    entry = entry or {"source": "/d/A.mov", "mode": "two_arena"}
    return core.plan_encode(entry, settings, INFO)[0]


def arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_defaults_are_8bit_420_mp4_faststart():
    cmd = plan({"encoder": "cpu"})[0]["cmd"]
    assert arg(cmd, "-pix_fmt") == "yuv420p" and cmd[-1].endswith(".mp4")
    assert "+faststart" in cmd and cmd[cmd.index("-vf") + 1].endswith("format=yuv420p")


def test_10bit_h265_changes_filter_and_encoder_args_and_names():
    jobs = plan({"encoder": "cpu", "codec": "h265", "pix_fmt": "yuv420p10le", "container": "mkv"})
    cmd = jobs[0]["cmd"]
    assert arg(cmd, "-pix_fmt") == "yuv420p10le" and arg(cmd, "-c:v") == "libx265"
    assert arg(cmd, "-vf").endswith("format=yuv420p10le")
    assert cmd[-1].endswith("A_left_crop_cpu.mkv") and "-movflags" not in cmd


def test_layout_gets_the_pixel_format_too():
    e = {"source": "/d/A.mov", "layout": {"grid": {"rows": 1, "cols": 2}}}
    cmd = plan({"encoder": "cpu", "pix_fmt": "yuv444p"}, e)[0]["cmd"]
    assert arg(cmd, "-vf").endswith("format=yuv444p")


def test_prores_output_is_cpu_only_mov_422_10bit():
    jobs = plan({"encoder": "nvenc", "codec": "prores", "prores_profile": 4})
    cmd = jobs[0]["cmd"]
    assert arg(cmd, "-c:v") == "prores_ks" and arg(cmd, "-profile:v") == "4"
    assert arg(cmd, "-pix_fmt") == "yuv422p10le" and jobs[0]["output"].endswith("_cpu.mov")
    assert "-hwaccel" not in cmd and "-cq" not in cmd and "-crf" not in cmd


def test_ffv1_is_lossless_mkv():
    cmd = plan({"encoder": "cpu", "codec": "ffv1"})[0]["cmd"]
    assert arg(cmd, "-c:v") == "ffv1" and cmd[-1].endswith(".mkv") and "-crf" not in cmd


def test_chunked_mkv_segments_without_faststart():
    cmd = plan({"encoder": "cpu", "container": "mkv"},
               {"source": "/d/A.mov", "mode": "single", "time_mode": "chunked"})[0]["cmd"]
    assert "-segment_format_options" not in cmd and cmd[-1].endswith("_chunk%03d_cpu.mkv")


@pytest.mark.parametrize("s,msg", [
    ({"encoder": "nvenc", "pix_fmt": "yuv422p10le"}, "cannot write"),
    ({"encoder": "cpu", "codec": "prores", "pix_fmt": "yuv420p"}, "cannot write"),
    ({"encoder": "cpu", "container": "avi"}, "cannot go"),
    ({"encoder": "cpu", "codec": "prores", "container": "mp4"}, "cannot go"),
    ({"encoder": "cpu", "codec": "vp9"}, "unknown codec"),
])
def test_impossible_combinations_are_refused(s, msg):
    with pytest.raises(ValueError, match=msg):
        plan(s)


# ---- probe -----------------------------------------------------------------
def stream(**kw):
    return {"codec_type": "video", "codec_name": "prores", "width": 4096, "height": 2160,
            "pix_fmt": "yuv422p10le", "r_frame_rate": "24000/1001",
            "avg_frame_rate": "24000/1001", **kw}


def parse(streams, **fmt):
    return core.parse_probe({"streams": streams, "format": {"duration": "12.5", **fmt}}, 1)


def test_probe_pocket_prores_facts():
    i = parse([stream(tags={"timecode": "01:02:03:04"}),
               {"codec_type": "audio"}, {"codec_type": "data", "tags": {"timecode": "x"}}])
    assert (i["width"], i["height"], i["codec"]) == (4096, 2160, "prores")
    assert i["fps"] == pytest.approx(23.976, abs=1e-3) and not i["vfr"]
    assert (i["bit_depth"], i["chroma"], i["timecode"], i["audio_streams"]) == \
        (10, "422", "01:02:03:04", 1)
    assert core.source_warnings(i, "yuv422p10le") == []


def test_probe_skips_cover_art_and_refuses_no_video():
    art = stream(width=100, height=100, disposition={"attached_pic": 1})
    assert parse([art, stream()])["width"] == 4096
    with pytest.raises(ValueError, match="no video stream"):
        parse([art, {"codec_type": "audio"}])


def test_probe_fps_fallbacks_and_vfr():
    assert parse([stream(r_frame_rate="0/0", avg_frame_rate="50/1")])["fps"] == 50
    i = parse([stream(r_frame_rate="0/0", avg_frame_rate="0/0")])
    assert i["fps"] == 30.0 and i["fps_assumed"]
    v = parse([stream(r_frame_rate="60/1", avg_frame_rate="47/1")])
    assert v["vfr"] and "variable frame rate" in core.source_warnings(v)[0]


def test_probe_rotation_swaps_to_displayed_pixels():
    i = parse([stream(width=2160, height=3840, side_data_list=[{"rotation": -90}])])
    assert (i["width"], i["height"], i["rotation"]) == (3840, 2160, 270)
    assert any("rotated" in w for w in core.source_warnings(i))


def test_source_warnings_8bit_target_sar_interlace():
    i = parse([stream(sample_aspect_ratio="2:1", field_order="tt")])
    w = " ".join(core.source_warnings(i, "yuv420p"))
    assert "10-bit source -> 8-bit" in w and "non-square" in w and "interlaced" in w


def test_layout_warns_about_distortion_and_upscale():
    lay = {"grid": {"rows": 2, "cols": 2}, "output": {"w": 1024, "h": 1080}}
    w = core.layout_warnings(lay, 4096, 2160)
    assert len(w) == 4 and "distorted by 100%" in w[0]       # 2048x1080 squeezed to half width
    ok = {"grid": {"rows": 1, "cols": 1}, "output": {"w": 2048, "h": 1080}}
    assert core.layout_warnings(ok, 4096, 2160) == []
    up = {"regions": [{"name": "a", "x": 0, "y": 0, "w": 512, "h": 540}],
          "output": {"w": 1024, "h": 1080}}
    assert "upscaled" in core.layout_warnings(up, 4096, 2160)[0]


# ---- fps stage --------------------------------------------------------------
def fps_info(pix="yuv420p", fps=30.0):
    return {"codec": "h264", "fps": fps, "pix_fmt": pix, "width": 1024, "height": 1080,
            "duration": 60.0}


def test_fps_8bit_source_gives_the_v1_command_and_name():
    pl = core.plan_fps("/d/x_phase2.mp4", 5.0, {"encoder": "cpu", "quality": 20}, fps_info())
    assert pl["output"] == "/d/x_phase2_5fps.mp4"
    assert pl["cmd"] == core.build_fps_cmd("/d/x_phase2.mp4", "/d/x_phase2_5fps.mp4", 5.0,
                                           "h264", False, 20, "p5", "medium", True)


def test_fps_keeps_10bit_when_the_encoder_can():
    pl = core.plan_fps("/d/x.mp4", 5.0, {"encoder": "cpu"}, fps_info("yuv420p10le"))
    assert arg(pl["cmd"], "-pix_fmt") == "yuv420p10le"
    assert arg(pl["cmd"], "-vf").endswith("format=yuv420p10le")


def test_fps_falls_back_to_8bit_with_a_note_on_h264_nvenc():
    pl = core.plan_fps("/d/x.mp4", 5.0, {"encoder": "nvenc"}, fps_info("yuv420p10le"))
    assert arg(pl["cmd"], "-pix_fmt") == "yuv420p"
    assert "cannot be written" in pl["options"]["notes"][0]


def test_fps_explicit_pix_fmt_and_container_win():
    pl = core.plan_fps("/d/x.mp4", 5, {"encoder": "cpu", "codec": "h265", "pix_fmt": "yuv420p",
                                       "container": "mkv"}, fps_info("yuv420p10le"))
    assert pl["output"] == "/d/x_5fps.mkv" and arg(pl["cmd"], "-c:v") == "libx265"
    assert "-movflags" not in pl["cmd"]


def test_fps_writes_a_replayable_record_before_ffmpeg(tmp_path):
    src = str(tmp_path / "x_phase2.mp4")
    seen = []
    rc = lambda cmd, log: (seen.append(os.path.exists(str(tmp_path / "x_phase2_5fps.fps.json"))), 0)[1]
    status, out = core.reduce_fps(src, 5, {"encoder": "cpu"}, log=lambda m: None, run=rc,
                                  info={**fps_info(), "r_frame_rate": "30/1"})
    assert status == "ok" and seen == [True]            # record existed when ffmpeg ran
    rec = json.load(open(core.fps_record_path(out)))
    assert rec["fps"] == [{"source": src, "target_fps": 5}]
    assert rec["fps_settings"]["encoder"] == "cpu" and "ffmpeg" in rec["resolved"]["command"]
