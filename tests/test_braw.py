"""BRAW ingest, tested with a fake braw2pipe (the real one needs the SDK)."""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

import stylet_core as core

W, H, N = 128, 72, 6

FAKE = f'''#!{sys.executable}
import json, sys
a = sys.argv[1:]
scale = int(a[a.index("--scale") + 1]) if "--scale" in a else 1
clip = a[-1]
if clip.endswith("bad.braw") and "--info" not in a:
    sys.stderr.write("decoder exploded\\n"); sys.exit(5)
w, h = {W} // scale, {H} // scale
if "--info" in a:
    print(json.dumps({{"width": w, "height": h, "frames": {N}, "fps_num": 24000,
                      "fps_den": 1001, "bit_depth": 12, "scale": scale}}))
else:
    out = sys.stdout.buffer
    for i in range({N}):
        out.write(bytes([(i * 40 + x) % 256 for x in range(w * 6)]) * h)
'''


@pytest.fixture
def helper(tmp_path):
    p = tmp_path / "bin" / "braw2pipe"
    p.parent.mkdir()
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture
def clip(tmp_path):
    c = tmp_path / "A006.braw"
    c.write_bytes(b"BRAW")
    return str(c)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")


def test_helper_missing_gives_build_instructions(monkeypatch, tmp_path):
    monkeypatch.delenv("STYLET_BRAW_HELPER", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    if os.path.exists(os.path.join(os.path.dirname(core.__file__), "braw", "braw2pipe")):
        pytest.skip("a real helper is built in this checkout")
    with pytest.raises(RuntimeError, match="build.sh"):
        core.find_braw_helper()


def test_helper_found_by_env_and_by_argument(monkeypatch, helper):
    monkeypatch.setenv("STYLET_BRAW_HELPER", helper)
    assert core.find_braw_helper() == helper
    assert core.find_braw_helper(helper) == helper


def test_probe_dispatches_braw_and_reports_exact_fps(monkeypatch, helper, clip):
    monkeypatch.setenv("STYLET_BRAW_HELPER", helper)
    i = core.probe(clip)
    assert (i["width"], i["height"], i["codec"], i["nb_frames_meta"]) == (W, H, "braw", N)
    assert i["r_frame_rate"] == "24000/1001" and i["bit_depth"] == 12 and i["chroma"] == "rgb"
    assert i["duration"] == pytest.approx(N * 1001 / 24000)
    assert core.probe(clip, braw_helper=helper)["width"] == W
    assert core.braw_info(clip, helper, scale=2)["width"] == W // 2


def test_plan_ingest_is_rgb48_into_prores_444_with_bt709(helper, clip):
    info = core.braw_info(clip, helper)
    pl = core.plan_ingest({"source": clip}, {}, info, helper)
    c = pl["cmd"]
    assert pl["producer"] == [helper, "--scale", "1", clip]
    assert c[c.index("-pix_fmt") + 1] == "rgb48le" and c[c.index("-framerate") + 1] == "24000/1001"
    assert "-profile:v" in c and c[c.index("-profile:v") + 1] == "4"
    assert "format=yuv444p10le" in c[c.index("-vf") + 1] and "out_color_matrix=bt709" in c[c.index("-vf") + 1]
    assert c[-1] == pl["part"] == clip.replace(".braw", ".mov") + ".part" and "-n" in c
    assert core.plan_ingest({"source": clip}, {"profile": 3}, info, helper)["cmd"][
        c.index("-vf") + 1].endswith("format=yuv422p10le")
    with pytest.raises(ValueError):
        core.plan_ingest({"source": clip}, {"scale": 3}, info, helper)


@needs_ffmpeg
def test_ingest_end_to_end_then_reuse(helper, clip, tmp_path):
    lines = []
    out, bad = core.ingest_braw({"source": clip}, {"helper": helper}, lines.append)
    assert bad == [] and out == clip.replace(".braw", ".mov") and os.path.exists(out)
    assert not os.path.exists(out + ".part")
    i = core.probe(out)
    assert (i["codec"], i["width"], i["height"]) == ("prores", W, H)
    assert i["pix_fmt"].startswith("yuv444p") and i["bit_depth"] >= 10      # 4444, not 8-bit
    assert i["nb_frames_meta"] == N and i["color_space"] == "bt709"
    rec = json.load(open(core.ingest_record_path(clip)))
    assert rec["ingest"] == [{"source": clip}] and "braw2pipe" in rec["resolved"]["producer"]

    def boom(*a, **k):
        raise AssertionError("must reuse, not re-decode")
    out2, bad2 = core.ingest_braw({"source": clip}, {"helper": helper}, lines.append, run=boom)
    assert out2 == out and bad2 == [] and any("reusing" in l for l in lines)


@needs_ffmpeg
def test_failed_decoder_leaves_no_intermediate(helper, tmp_path):
    bad = tmp_path / "bad.braw"
    bad.write_bytes(b"x")
    out, failures = core.ingest_braw({"source": str(bad)}, {"helper": helper}, lambda m: None)
    assert failures and "ingest rc=5" in failures[0]
    assert not os.path.exists(out) and not os.path.exists(out + ".part")


def test_encode_of_braw_needs_an_ingest_step(clip):
    job = {"encode": [{"source": clip, "mode": "none"}]}
    with pytest.raises(ValueError, match="ingest"):
        core.run_job(job, log=lambda m: None, dry_run=True)


@needs_ffmpeg
def test_job_chains_ingest_and_layout_encode(helper, clip):
    job = {"ingest": [{"source": clip}], "ingest_settings": {"helper": helper},
           "encode": [{"source": clip, "layout": {"grid": {"rows": 1, "cols": 2}}}],
           "encode_settings": {"encoder": "cpu", "quality": 30, "cpu_preset": "ultrafast"}}
    assert core.run_job(job, log=lambda m: None) == []
    for name in ("r1c1", "r1c2"):
        f = clip.replace(".braw", f"_{name}_cpu.mp4")
        w, h = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                               "-of", "csv=p=0", f], capture_output=True, text=True).stdout.strip().split(",")
        assert (int(w), int(h)) == (W // 2, H)


def test_dry_run_prints_the_ingest_pipeline_and_defers_the_encode(helper, clip):
    lines = []
    job = {"ingest": [{"source": clip}], "ingest_settings": {"helper": helper},
           "encode": [{"source": clip, "mode": "none"}]}
    assert core.run_job(job, log=lines.append, dry_run=True) == []
    assert "braw2pipe" in lines[0] and " | ffmpeg" in lines[0]
    assert any("after ingest" in l for l in lines)
