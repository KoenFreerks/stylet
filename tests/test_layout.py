"""
Layouts (single crop, arena split, grid) and their ffmpeg filters. The legacy
modes must be expressible as layouts and give the same filters as v1's modes
[DD V.1]; Lanczos a=3 must be in every scaled chain [DD V.2].
"""

import json
import os
from itertools import product

import pytest

import stylet_core as core

SOURCES = ((4096, 2160), (3840, 2160), (2868, 1512), (1920, 1080))


@pytest.mark.parametrize("w,h", SOURCES)
def test_two_arena_is_a_layout(w, h):
    half = core.even(w // 2)
    layout = {"regions": [{"name": "left_crop", "x": 0, "y": 0, "w": half, "h": h},
                          {"name": "right_crop", "x": half, "y": 0, "w": half, "h": h}],
              "output": {"w": core.DLC_W, "h": core.DLC_H}}
    got = core.layout_filter_chain(layout, w, h)
    want = core.build_filter_chain(w, h, "two_arena", w // 2, 1080)
    if (half, h) == (core.DLC_W, core.DLC_H):          # v1 adds a no-op scale there
        return
    assert got == want


@pytest.mark.parametrize("w,h", SOURCES)
@pytest.mark.parametrize("click", (0, 700, 2048, 4000, 9999))
def test_single_is_a_layout(w, h, click):
    x, cw, ch = core.crop_box(w, h, "single", click)
    reg = core.centered_region(w, h, click, h // 2, cw, h, "crop")
    assert (reg["x"], reg["w"], reg["h"]) == (x, cw, ch)
    got = core.layout_filter_chain({"regions": [reg],
                                    "output": {"w": core.DLC_W, "h": core.DLC_H}}, w, h)
    assert got == core.build_filter_chain(w, h, "single", click, 1080)


def test_grid_tiles_the_frame_without_resampling():
    layout = {"grid": {"rows": 2, "cols": 3}}
    chains = core.layout_filter_chain(layout, 3840, 2160)
    assert [n for n, _ in chains] == ["r1c1", "r1c2", "r1c3", "r2c1", "r2c2", "r2c3"]
    assert chains[0][1] == "crop=1280:1080:0:0,format=yuv420p"        # crop only
    assert chains[5][1] == "crop=1280:1080:2560:1080,format=yuv420p"


def test_grid_cells_scaled_with_lanczos_a3():
    layout = {"grid": {"rows": 2, "cols": 2}, "output": {"w": 1024, "h": 1080}}
    for _, vf in core.layout_filter_chain(layout, 4096, 2160):
        assert "scale=1024:1080:flags=lanczos:param0=3" in vf
        assert vf.index("crop=") < vf.index("scale=") < vf.index("format=")


def test_scaler_is_overridable_and_recorded_default_is_lanczos():
    layout = {"regions": [{"name": "a", "x": 0, "y": 0, "w": 2000, "h": 1000}],
              "output": {"w": 1000, "h": 500}, "scaler": {"flags": "bicubic", "param0": 0}}
    assert "flags=bicubic:param0=0" in core.layout_filter_chain(layout, 4096, 2160)[0][1]
    assert core.SCALER_DEFAULT == {"flags": "lanczos", "param0": 3}


def test_grid_pitch_gap_offsets_skip():
    cells = core.grid_regions(4096, 2160, 2, 2, cell_w=1000, cell_h=1000, x0=100, y0=50,
                              pitch_x=1200, pitch_y=1100, offsets={"r2c2": [10, -4]},
                              skip=("r1c2",))
    assert [c["name"] for c in cells] == ["r1c1", "r2c1", "r2c2"]
    assert cells[2] == {"name": "r2c2", "x": 1310, "y": 1146, "w": 1000, "h": 1000}


def test_centered_region_clamps_inside_frame():
    assert core.centered_region(4096, 2160, -50, 5000, 1000, 800) == \
        {"name": "crop", "x": 0, "y": 1360, "w": 1000, "h": 800}
    r = core.centered_region(4096, 2160, 4090, 10, 1000, 800)
    assert r["x"] + r["w"] == 4096 and r["y"] == 0


@pytest.mark.parametrize("layout,msg", [
    ({}, "exactly one"),
    ({"regions": [{"name": "a", "x": 0, "y": 0, "w": 5000, "h": 100}]}, "outside"),
    ({"regions": [{"name": "a", "x": 1, "y": 0, "w": 100, "h": 100}]}, "even"),
    ({"regions": [{"name": "a.b", "x": 0, "y": 0, "w": 100, "h": 100}]}, "letters"),
    ({"regions": [{"name": "a", "x": 0, "y": 0, "w": 100, "h": 100},
                  {"name": "a", "x": 0, "y": 0, "w": 100, "h": 100}]}, "unique"),
    ({"regions": [{"name": "a", "x": 0, "y": 0, "w": 100, "h": 100}],
      "output": {"w": 100}}, "both"),
])
def test_bad_layouts_are_refused(layout, msg):
    with pytest.raises(ValueError, match=msg):
        core.layout_filter_chain(layout, 4096, 2160)


def test_plan_encode_names_and_commands_for_a_layout():
    entry = {"source": "/d/A.mov", "layout": {"grid": {"rows": 1, "cols": 2},
                                              "output": {"w": 1024, "h": 1080}}}
    info = {"width": 4096, "height": 2160, "codec": "prores"}
    jobs, log_path = core.plan_encode(entry, {"encoder": "cpu"}, info)
    assert [j["output"] for j in jobs] == ["/d/A_r1c1_cpu.mp4", "/d/A_r1c2_cpu.mp4"]
    assert log_path == "/d/A_cpu_encode.log"
    assert "-hwaccel" not in jobs[0]["cmd"]
    assert jobs[1]["cmd"][jobs[1]["cmd"].index("-vf") + 1] == jobs[1]["vf"]


def test_headless_dry_run_accepts_a_layout_entry(monkeypatch):
    monkeypatch.setattr(core, "probe", lambda p: {"width": 4096, "height": 2160,
                                                  "codec": "prores", "duration": 10.0})
    lines = []
    job = {"encode": [{"source": "A.mov", "layout": {"grid": {"rows": 1, "cols": 2}}}]}
    assert core.run_job(job, log=lines.append, dry_run=True) == []
    assert len(lines) == 2 and all(l.startswith("DRY RUN: ") for l in lines)


def test_layout_from_params_box_and_grid():
    box = core.layout_from_params({"kind": "box", "cx": "2048", "cy": 1080, "w": 1024,
                                   "h": "1080", "out_w": "1024", "out_h": "1080"})
    assert box == {"regions": [{"name": "crop", "cx": 2048, "cy": 1080, "w": 1024, "h": 1080}],
                   "output": {"w": 1024, "h": 1080}}
    assert core.layout_filter_chain(box, 4096, 2160)[0][1] == \
        "crop=1024:1080:1536:540,format=yuv420p"
    grid = core.layout_from_params({"kind": "grid", "rows": "2", "cols": 3, "cell_w": "",
                                    "x0": "", "y0": "10", "out_w": "", "out_h": ""})
    assert grid == {"grid": {"rows": 2, "cols": 3, "x0": 0, "y0": 10}}
    with pytest.raises(ValueError):
        core.layout_from_params({"kind": "ring"})


def test_preview_command_uses_the_real_filter():
    cmd = core.preview_cmd("A.mov", "crop=100:100:0:0,format=yuv420p", 5, "o.png", 320)
    assert cmd[cmd.index("-vf") + 1] == \
        "crop=100:100:0:0,format=yuv420p,scale=320:-2:flags=lanczos"
    assert cmd[cmd.index("-ss") + 1] == "5" and cmd[-1] == "o.png"
