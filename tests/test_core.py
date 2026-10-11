"""
Behaviour of stylet_core that v1 had no separate function for: the forward
keyframe snap, the always-kept fragment, what a phase split writes and in
which order, the encode record, and headless job files. No ffmpeg is run:
commands go to a fake runner and probing is monkeypatched.
"""

import ast
import json
import os

import pytest

import stylet_core as core


# ---------------------------------------------------------------------------
# Phase trims [DD V.3, V.4]
# ---------------------------------------------------------------------------
def test_trim_start_snaps_forward_never_backward():
    seek, dur = core.trim_plan(kf=1807.9, start_sec=1800, end_sec=3600)
    assert seek > 1800 and seek == pytest.approx(1807.95)     # starts late, never early
    assert dur == pytest.approx(3600 - 1807.9)                # end held at the request


def test_trim_without_keyframe_falls_back_to_requested_start():
    assert core.trim_plan(None, 10, 25) == (10, 15)


def test_trim_refuses_when_snapped_start_passes_end():
    assert core.trim_plan(kf=40.0, start_sec=30, end_sec=35) is None


def test_trim_command_is_a_lossless_stream_copy():
    cmd = core.trim_cmd("in.mp4", "out.mp4", 12.3456789, 60)
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-ss") + 1] == "12.345679" and cmd.index("-ss") < cmd.index("-i")


def test_fragment_is_always_cut_when_there_is_a_gap():
    secs = {"p1": (0, 1800), "p2": (2400, 5400)}
    assert core.phase_segments("Phase 1 + Phase 2", secs) == [
        ("phase1", 0, 1800), ("fragment", 1800, 2400), ("phase2", 2400, 5400)]
    assert core.phase_segments("Phase 1 + Phase 2", {"p1": (0, 1800), "p2": (1800, 5400)}) == [
        ("phase1", 0, 1800), ("phase2", 1800, 5400)]
    # even a one-second handling gap is kept: v1 cut a fragment whenever p2s > p1e
    assert core.phase_segments("Phase 1 + Phase 2", {"p1": (0, 1800), "p2": (1801, 5400)})[1] == \
        ("fragment", 1800, 1801)
    assert core.phase_segments("Phase 1 only", {"p1": (5, 9), "p2": None}) == [("phase1", 5, 9)]
    assert core.phase_segments("No split", None) == []


def test_split_writes_phase1_fragment_phase2_in_order_from_the_snapped_keyframes():
    ran = []
    cfg = {"mode": "Phase 1 + Phase 2"}
    secs = {"p1": (0, 100), "p2": (160, 400)}
    out = core.split_phases("/v/rec.mp4", cfg, secs, lambda l: core.single_phase_path("/v/rec.mp4", l),
                            log=lambda m: None, run=lambda c, lg: ran.append(c) or True,
                            kf_fn=lambda p, s: s + 3.0)            # keyframe 3 s after each start
    assert [os.path.basename(p) for p in out] == ["rec_phase1.mp4", "rec_fragment.mp4",
                                                  "rec_phase2.mp4"]
    seeks = [float(c[c.index("-ss") + 1]) for c in ran]
    assert seeks == pytest.approx([3.05, 103.05, 163.05])
    assert all(c[c.index("-i") + 1] == "/v/rec.mp4" for c in ran)  # source only read


def test_a_failed_trim_stops_the_split():
    out = core.split_phases("/v/r.mp4", {"mode": "Phase 1 + Phase 2"},
                            {"p1": (0, 100), "p2": (160, 400)},
                            lambda l: f"/v/r_{l}.mp4", log=lambda m: None,
                            run=lambda c, lg: False, kf_fn=lambda p, s: s)
    assert out is None


# ---------------------------------------------------------------------------
# Encode record and headless jobs
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_video(tmp_path, monkeypatch):
    src = tmp_path / "A006.mov"
    src.write_bytes(b"\0" * 10)
    info = {"duration": 3600.0, "width": 4096, "height": 2160, "codec": "prores", "fps": 30.0,
            "r_frame_rate": "30/1", "pix_fmt": "yuv422p10le", "nb_frames_meta": 108000,
            "size_bytes": 10}
    monkeypatch.setattr(core, "probe", lambda p: dict(info))
    return str(src)


def test_encode_writes_a_replayable_record_before_running_ffmpeg(fake_video, tmp_path):
    seen = []

    def run(cmd, log_path):
        seen.append(os.path.exists(core.encode_record_path(entry)))  # record already there?
        return 0

    entry = {"source": fake_video, "mode": "two_arena", "arenas": ["left"]}
    failed = core.encode_file(entry, {"encoder": "cpu"}, log=lambda m: None, run=run)
    assert failed == [] and seen == [True]
    rec = json.load(open(tmp_path / "A006.encode.json"))
    assert rec["encode"] == [entry] and rec["encode_settings"]["encoder"] == "cpu"
    assert [o["label"] for o in rec["resolved"]["outputs"]] == ["left_crop"]
    assert rec["resolved"]["audit"]["drop_count"] == 0
    # the record is itself a job file: a dry run rebuilds the same command
    printed = []
    assert core.run_job(rec, log=printed.append, dry_run=True) == []
    assert printed == ["DRY RUN: " + rec["resolved"]["commands"][0]]


def test_headless_redo_with_a_different_centre(fake_video):
    printed = []
    job = {"encode": [{"source": fake_video, "mode": "single", "click_x": 3000}],
           "encode_settings": {"encoder": "cpu"}}
    core.run_job(job, log=printed.append, dry_run=True)
    assert len(printed) == 1 and "crop=2048:2160:1976:0" in printed[0]


def test_headless_encode_needs_an_explicit_mode(fake_video):
    with pytest.raises(ValueError, match="explicit 'mode'"):
        core.run_job({"encode": [{"source": fake_video}]}, dry_run=True)


def test_headless_phase_split_uses_the_saved_sidecar(fake_video, tmp_path, monkeypatch):
    core.save_json(core.single_phases_json(fake_video),
                   {"mode": "Phase 1 + Phase 2", "phase1_start": "00:00:00",
                    "phase1_end": "00:30:00", "phase2_start": "00:35:00",
                    "phase2_end": "00:59:00"})
    monkeypatch.setattr(core, "first_keyframe_at_or_after", lambda p, s: s)
    printed = []
    assert core.run_job({"phases": [{"source": fake_video}]}, log=printed.append,
                        dry_run=True) == []
    cmds = [p for p in printed if p.startswith("DRY RUN")]
    assert [c.split()[-1].rsplit("_", 1)[-1] for c in cmds] == \
        ["phase1.mov", "fragment.mov", "phase2.mov"]


def test_single_file_split_never_deletes_the_source(fake_video, monkeypatch):
    monkeypatch.setattr(core, "run_quiet", lambda cmd, log=print: True)
    monkeypatch.setattr(core, "first_keyframe_at_or_after", lambda p, s: s)
    cfg = {"mode": "Phase 1 only", "phase1_start": "00:00:00", "phase1_end": "00:10:00"}
    assert core.process_single(fake_video, cfg, log=lambda m: None) == []
    assert os.path.exists(fake_video)


def test_fps_skips_when_already_at_or_below_target(fake_video):
    status, _ = core.reduce_fps(fake_video, 30, {}, log=lambda m: None, run=lambda *a: 0)
    assert status == "skipped"
    status, out = core.reduce_fps(fake_video, 5, {}, log=lambda m: None, run=lambda *a: 0)
    assert status == "ok" and out.endswith("A006_5fps.mp4")


# ---------------------------------------------------------------------------
# Headless means no GUI toolkit at import
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fname", ["stylet_core.py", "stylet.py"])
def test_no_module_level_gui_import(fname):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), fname)
    for node in ast.parse(open(path).read()).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(n.startswith(("tkinter", "PySide6", "PyQt")) for n in names), fname
