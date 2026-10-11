"""
The rewrite must build exactly what v1 built. The v1 functions are lifted
straight out of tests/legacy/video_pipeline_v1.py by AST, without running its
module body (which would open a Tk window), and compared with stylet_core over a
grid of inputs: crop filters, ffmpeg commands, output names, phase validation
messages and the folder scan.
"""

import ast
import os
import re
from collections import defaultdict
from itertools import product

import pytest

import stylet_core as core

LEGACY = os.path.join(os.path.dirname(__file__), "legacy", "video_pipeline_v1.py")
WANT = {"even", "parse_hms", "hms", "encode_build_filter_chain", "encode_build_cmd",
        "fps_build_cmd", "fps_build_output_name", "concat_output_name_for_group",
        "concat_phase_output_name", "concat_single_phase_output_name",
        "concat_phases_json_path", "concat_single_phases_json_path",
        "concat_validate_phase_config", "concat_find_groups_and_others",
        "CHUNK_RE", "OUTPUT_RE", "VIDEO_EXTS", "NVDEC_DECODABLE", "DLC_W", "DLC_H"}


@pytest.fixture(scope="module")
def v1():
    tree = ast.parse(open(LEGACY).read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in WANT
                                                  for t in n.targets))]
    ns = {"os": os, "re": re, "defaultdict": defaultdict}
    exec(compile(ast.Module(body=keep, type_ignores=[]), LEGACY, "exec"), ns)
    missing = WANT - set(ns)
    assert not missing, missing
    return ns


WIDTHS = (1024, 1280, 1920, 2048, 3840, 4096, 4104)
HEIGHTS = (720, 1080, 1440, 2160)


def test_crop_filters_identical(v1):
    n = 0
    for w, h in product(WIDTHS, HEIGHTS):
        for mode in ("two_arena", "single", "none"):
            for click in (0, 100, w // 2, w - 10, w + 500):
                for th in (720, 1080):
                    for al, ar in product((True, False), repeat=2):
                        assert core.build_filter_chain(w, h, mode, click, th, al, ar) == \
                            v1["encode_build_filter_chain"](w, h, mode, click, th, al, ar)
                        n += 1
    assert n > 1000


def test_encode_commands_identical(v1):
    for nv, codec, src, ow, seg, q in product((True, False), ("h264_nvenc", "libx265"),
                                              ("prores", "h264"), (True, False),
                                              (True, False), (18, 20, 28)):
        kw = dict(input_file="/in/A006.mov", output_path="/out/A006_left_crop_nvenc.mp4",
                  vf_filter="crop=1:2:3:0", video_codec=codec, use_nvenc=nv, src_codec=src,
                  quality_value=q, nvenc_preset="p5", cpu_preset="medium", overwrite=ow,
                  use_segmenter=seg, segment_time=1800,
                  segment_pattern="/out/A006_left_crop_chunk%03d_nvenc.mp4")
        assert core.build_encode_cmd(**kw) == v1["encode_build_cmd"](**kw)


def test_prores_is_never_cuda_decoded():
    cmd = core.build_encode_cmd(input_file="a.mov", output_path="b.mp4", vf_filter="x",
                                video_codec="h264_nvenc", use_nvenc=True, src_codec="prores",
                                quality_value=20, nvenc_preset="p5", cpu_preset="medium",
                                overwrite=True)
    assert "-hwaccel" not in cmd


def test_plan_encode_matches_v1_names_and_commands(v1):
    """What v1's encode button built, reconstructed from its own pieces."""
    info = {"width": 4096, "height": 2160, "codec": "prores"}
    for nv, chunked in product((True, False), (False, True)):
        s = {"encoder": "nvenc" if nv else "cpu", "codec": "h264", "quality": 20,
             "nvenc_preset": "p5", "cpu_preset": "medium", "target_h": 1080, "overwrite": True}
        entry = {"source": "/d/A006.mov", "mode": "two_arena",
                 "time_mode": "chunked" if chunked else "full", "chunk_minutes": 30}
        jobs, log_path = core.plan_encode(entry, s, info)
        suffix = "nvenc" if nv else "cpu"
        assert log_path == f"/d/A006_{suffix}_encode.log"
        for job, (label, vf) in zip(jobs, v1["encode_build_filter_chain"](4096, 2160, "two_arena",
                                                                          2048, 1080)):
            out = f"/d/A006_{label}_{suffix}.mp4"
            pat = f"/d/A006_{label}_chunk%03d_{suffix}.mp4"
            assert job["cmd"] == v1["encode_build_cmd"](
                input_file="/d/A006.mov", output_path=out, vf_filter=vf,
                video_codec="h264_nvenc" if nv else "libx264", use_nvenc=nv,
                src_codec="prores", quality_value=20, nvenc_preset="p5", cpu_preset="medium",
                overwrite=True, start_sec=None, duration=None, use_segmenter=chunked,
                segment_time=1800 if chunked else None, segment_pattern=pat if chunked else None)
        assert [j["label"] for j in jobs] == ["left_crop", "right_crop"]


def test_fps_commands_and_names_identical(v1):
    for nv, src, q, ow, t in product((True, False), ("h264", "prores"), (18, 23),
                                     (True, False), (5, 2.5, 10)):
        args = ("/d/x_phase2.mp4", "/d/x_phase2_5fps.mp4", t, src, nv, q, "p5", "slow", ow)
        assert core.build_fps_cmd(*args) == v1["fps_build_cmd"](*args)
    for p in ("/d/a.mp4", "/d/a_30fps.mp4", "/d/a_29.97fps.mov", "/d/a_left_crop_nvenc_phase2.mp4"):
        for t in (5, 2.5, 10.0, 12.5):
            assert core.fps_output_name(p, t) == v1["fps_build_output_name"](p, t)


@pytest.mark.parametrize("key", ["A006_left_crop_nvenc.mp4", "rec_full_cpu.mov",
                                 "odd.name.mp4", "noext"])
def test_output_names_identical(v1, key):
    assert core.merged_name(key) == v1["concat_output_name_for_group"](key)
    for ph in ("phase1", "phase2", "fragment"):
        assert core.group_phase_name(key, ph) == v1["concat_phase_output_name"](key, ph)
        assert core.single_phase_path("/x/" + key, ph) == \
            v1["concat_single_phase_output_name"]("/x/" + key, ph)
    assert core.group_phases_json("/f", key) == v1["concat_phases_json_path"]("/f", key)
    assert core.single_phases_json("/x/" + key) == v1["concat_single_phases_json_path"]("/x/" + key)


def test_hms_parsing_identical(v1):
    for s in ("00:00:00", "1:02:03", "01;02;03", "01.02.03", "1,2,3", "12:59:59"):
        assert core.parse_hms(s) == v1["parse_hms"](s)
    for bad in ("", "1:2", "01:60:00", "a:b:c", "1:2:3:4"):
        with pytest.raises(ValueError) as e_new:
            core.parse_hms(bad)
        with pytest.raises(ValueError) as e_old:
            v1["parse_hms"](bad)
        assert str(e_new.value) == str(e_old.value)
    for n in (0, 59, 3600, 7322, 86399):
        assert core.hms(n) == v1["hms"](n)


def test_phase_validation_identical(v1):
    cases = []
    for mode in ("No split", "Phase 1 only", "Phase 1 + Phase 2"):
        for p1, p2 in product([("00:00:00", "00:30:00"), ("00:30:00", "00:10:00"),
                               ("", "00:10:00"), ("00:00:00", "03:00:00")],
                              [("00:35:00", "01:30:00"), ("00:20:00", "01:00:00"),
                               ("00:30:00", "00:30:00"), ("00:40:00", "05:00:00"),
                               ("x", "01:00:00")]):
            cases.append({"mode": mode, "phase1_start": p1[0], "phase1_end": p1[1],
                          "phase2_start": p2[0], "phase2_end": p2[1]})
    for cfg in cases:
        for dur in (None, 0, 5400.0):
            assert core.validate_phase_config(cfg, dur) == \
                v1["concat_validate_phase_config"](cfg, dur)


def test_folder_scan_identical(v1, tmp_path):
    names = ["A_left_crop_chunk000_nvenc.mp4", "A_left_crop_chunk001_nvenc.mp4",
             "A_left_crop_chunk002_nvenc.mp4", "B_full_chunk001_cpu.mp4",
             "C_left_crop_nvenc.mp4", "C_left_crop_nvenc_phase1.mp4",
             "C_left_crop_nvenc_fragment.mp4", "A_left_crop_merged_nvenc.mp4",
             "C_left_crop_nvenc.phases.json", "notes.txt", "D.MOV"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    new = core.find_groups_and_others(str(tmp_path))
    old = v1["concat_find_groups_and_others"](str(tmp_path))
    assert new == old
