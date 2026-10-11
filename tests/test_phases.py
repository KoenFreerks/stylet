"""The general "Phases" mode: any number of phases, named, with the gaps between
them always kept as numbered fragments. The cut rules are the same as for the
two-phase modes [DD V.3, V.4, V.5]."""

import os

import pytest

import stylet_core as core


def cfg(*phases, mode="Phases"):
    return {"mode": mode, "phases": [dict(name=n, start=a, end=b) for n, a, b in phases]}


FOUR = cfg(("", "00:00:00", "00:10:00"), ("stim", "00:12:00", "00:20:00"),
           ("", "00:20:00", "00:30:00"), ("recovery", "00:31:00", "01:00:00"))


def test_any_number_of_phases_with_numbered_fragments_for_every_gap():
    ok, secs = core.validate_phase_config(FOUR, 3600.0)
    assert ok
    assert core.phase_segments("Phases", secs) == [
        ("phase1", 0, 600), ("fragment1", 600, 720), ("phase2-stim", 720, 1200),
        ("phase3", 1200, 1800), ("fragment3", 1800, 1860), ("phase4-recovery", 1860, 3600)]
    # phases 2 and 3 touch (no gap), so there is no fragment2


def test_a_single_phase_and_a_hundred_phases():
    ok, secs = core.validate_phase_config(cfg(("", "00:05:00", "00:06:00")), 600)
    assert ok and core.phase_segments("Phases", secs) == [("phase1", 300, 360)]
    many = {"mode": "Phases", "phases": core.even_phases(0, 100 * 60, 100)}
    ok, secs = core.validate_phase_config(many, 6000)
    assert ok and len(core.phase_segments("Phases", secs)) == 100


def test_last_end_may_be_blank_meaning_end_of_video():
    c = cfg(("", "00:00:00", "00:10:00"), ("", "00:20:00", ""))
    ok, secs = core.validate_phase_config(c, 1500.0)
    assert ok and secs["phases"][-1]["end"] == 1500.0
    assert not core.validate_phase_config(c, None)[0]
    bad = cfg(("", "00:00:00", ""), ("", "00:20:00", "00:30:00"))
    assert "empty time" in core.validate_phase_config(bad, 1500.0)[1]


@pytest.mark.parametrize("c,msg", [
    (cfg(), "at least one"),
    (cfg(("", "00:10:00", "00:05:00")), "end must be after start"),
    (cfg(("", "00:00:00", "00:10:00"), ("", "00:09:00", "00:20:00")), "may not overlap"),
    (cfg(("", "00:00:00", "00:10:00"), ("", "00:11:00", "05:00:00")), "exceeds"),
    (cfg(("a_b", "00:00:00", "00:10:00")), "letters, digits"),
    (cfg(("x", "0:0:x", "00:10:00")), "Phase 1"),
])
def test_bad_phase_configs_are_refused_with_a_clear_message(c, msg):
    ok, m = core.validate_phase_config(c, 3600.0)
    assert not ok and msg in m


def test_typo_tolerant_times_still_work():
    ok, secs = core.validate_phase_config(cfg(("", "00;00;00", "00.10.00")), 3600)
    assert ok and secs["phases"][0]["end"] == 600


def test_split_writes_every_clip_in_order_and_snaps_each_start_forward():
    ran = []
    ok, secs = core.validate_phase_config(FOUR, 3600.0)
    out = core.split_phases("/v/rec.mp4", FOUR, secs,
                            lambda l: core.single_phase_path("/v/rec.mp4", l), log=lambda m: None,
                            run=lambda c, lg: ran.append(c) or True, kf_fn=lambda p, s: s + 3.0)
    assert [os.path.basename(p) for p in out] == [
        "rec_phase1.mp4", "rec_fragment1.mp4", "rec_phase2-stim.mp4", "rec_phase3.mp4",
        "rec_fragment3.mp4", "rec_phase4-recovery.mp4"]
    seeks = [float(c[c.index("-ss") + 1]) for c in ran]
    assert seeks == pytest.approx([3.05, 603.05, 723.05, 1203.05, 1803.05, 1863.05])
    assert all(s > b for s, b in zip(seeks, [0, 600, 720, 1200, 1800, 1860]))   # never early
    assert all(c[c.index("-c") + 1] == "copy" for c in ran)


def test_output_names_for_chunk_groups_and_the_folder_scan(tmp_path):
    key = "A006_left_crop_nvenc.mp4"
    assert core.group_phase_name(key, "phase2-stim") == "A006_left_crop_merged_phase2-stim_nvenc.mp4"
    assert core.group_phase_name(key, "fragment3") == "A006_left_crop_merged_fragment3_nvenc.mp4"
    for n in ("C_nvenc.mp4", "C_nvenc_phase1.mp4", "C_nvenc_phase12-late.mp4",
              "C_nvenc_fragment2.mp4", "C_nvenc_fragment.mp4",
              "A_left_crop_merged_phase3_nvenc.mp4"):
        (tmp_path / n).write_bytes(b"x")
    groups, singles, others = core.find_groups_and_others(str(tmp_path))
    assert [os.path.basename(s) for s in singles] == ["C_nvenc.mp4"]     # outputs are skipped


def test_headless_phases_job(tmp_path, monkeypatch):
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(core, "probe", lambda p, helper=None: {"duration": 3600.0})
    monkeypatch.setattr(core, "first_keyframe_at_or_after", lambda p, s: s)
    printed = []
    job = {"phases": [dict(FOUR, source=str(src))]}
    assert core.run_job(job, log=printed.append, dry_run=True) == []
    names = [c.split()[-1].rsplit("_", 1)[-1] for c in printed if c.startswith("DRY RUN")]
    assert names == ["phase1.mp4", "fragment1.mp4", "phase2-stim.mp4", "phase3.mp4",
                     "fragment3.mp4", "phase4-recovery.mp4"]


def test_even_phases_tile_the_range_without_gaps():
    ph = core.even_phases(60, 3660, 4, names=["a", "", "c"])
    assert [p["name"] for p in ph] == ["a", "", "c", ""]
    assert ph[0]["start"] == "00:01:00" and ph[-1]["end"] == "01:01:00"
    assert all(ph[i]["end"] == ph[i + 1]["start"] for i in range(3))
    with pytest.raises(ValueError):
        core.even_phases(10, 10, 3)
