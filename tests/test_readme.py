"""Every job file shown in the README must be valid and plan the file names the
README says it writes, so the examples cannot rot."""

import json
import os
import re

import pytest

import stylet_core as core

README = os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md")


def blocks():
    text = open(README).read()
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]


@pytest.fixture
def fake_probe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    info = {"width": 4096, "height": 2160, "codec": "prores", "duration": 7200.0, "fps": 24.0,
            "r_frame_rate": "24/1", "pix_fmt": "yuv422p10le", "nb_frames_meta": None,
            "size_bytes": 1}
    monkeypatch.setattr(core, "probe", lambda p, helper=None: info)
    monkeypatch.setattr(core, "first_keyframe_at_or_after", lambda p, s: s)


def test_every_json_example_is_a_valid_job(fake_probe, tmp_path):
    jobs = [b for b in blocks() if {"encode", "phases", "fps", "ingest"} & set(b)]
    assert len(jobs) >= 10
    for job in jobs:
        if "ingest" in job:                      # needs the BRAW helper; checked in test_braw
            continue
        lines = []
        assert core.run_job(job, log=lines.append, dry_run=True) == [], job
        assert lines, job
    assert os.listdir(tmp_path) == []            # a dry run writes nothing


def written(job):
    out = []
    for e in job.get("encode", []):
        jobs, _ = core.plan_encode(e, job.get("encode_settings"),
                                   {"width": 4096, "height": 2160, "codec": "prores"})
        out += [os.path.basename(j["output"]) for j in jobs]
    return out


def find(key):
    return [b for b in blocks() if key(b)]


def test_the_names_the_readme_promises():
    one = find(lambda b: b.get("encode", [{}])[0].get("mode") == "none")[0]
    assert written(one) == ["A006_full_nvenc.mp4"]
    grid = find(lambda b: "grid" in json.dumps(b) and "512" in json.dumps(b))[0]
    names = written(grid)
    assert names[0] == "plate01_r1c1_nvenc.mp4" and names[-1] == "plate01_r2c4_nvenc.mp4"
    assert len(names) == 8
    two = find(lambda b: b.get("encode", [{}])[0].get("mode") == "two_arena"
               and "time_mode" not in b["encode"][0])[0]
    assert written(two) == ["A006_left_crop_nvenc.mp4", "A006_right_crop_nvenc.mp4"]
    p = find(lambda b: b.get("phases", [{}])[0].get("mode") == "Phases")[0]["phases"][0]
    ok, secs = core.validate_phase_config(p, 10800.0)
    assert ok and [l for l, *_ in core.phase_segments("Phases", secs)] == [
        "phase1-n1", "fragment1", "phase2-n2", "fragment2", "phase3-n3"]
    assert core.fps_output_name("A006_crop_nvenc_phase2.mp4", 5) == "A006_crop_nvenc_phase2_5fps.mp4"
