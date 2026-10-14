# CLAUDE.md — Stylet

Operating rules for Claude Code in this repo. The reasoning behind each rule
is in `design_decisions.md`, cited as *[DD x]* with the same section letters as
the SpiderMitePipeline original.

## 0. What this is

Stylet turns raw camera recordings (ProRes) into the
analysis-ready videos the tracking pipelines consume: crop each arena to the
tracking geometry, split into phases (phase1 + fragment + phase2), limit the
frame rate. It is both the development and the production tool.

- `stylet_core.py` — all logic: probing, frame audit, crop filters, ffmpeg
  commands, merge/phase split, fps, headless jobs. No GUI import.
- `stylet_gui.py` — the Qt (PySide6) GUI: three tabs (**Encode / Split**,
  **Phase split / Merge**, **fps Limiter**), a crop/layout dialog with output preview.
- `stylet.py` — the entry point: `--headless` jobs, or launches the GUI.
- `braw/` — `braw2pipe.cpp` + `build.sh`: a helper built against the free Blackmagic
  RAW SDK that decodes `.braw` to raw frames *[DD V.10]*. **Not yet compiled or run
  against real BRAW** (the SDK has not been installed yet); the Python side is tested with a fake.
- Layouts (`stylet_core.py`, "Layouts" section): an encode entry may carry
  `"layout"` instead of `"mode"` — `regions` (x/y/w/h or centre cx/cy/w/h) or a
  `grid` (rows, cols, cell size, pitch, offsets, skip), with optional `output`
  size. Per region: crop → scale (Lanczos a=3, CPU only; skipped when the region
  already has the output size) → `format=yuv420p`. Outputs are named
  `<base>_<region>_<enc>.mp4`. The old modes stay and are pinned to v1;
  `tests/test_layout.py` shows they are expressible as layouts.
- `tests/` — `test_equivalence.py` compares every command/name/validation
  builder against the v1 functions lifted from `tests/legacy/video_pipeline_v1.py`
  (the original 3040-line GUI, kept only for that); `test_core.py` covers the
  snap, the fragment, the job record and headless runs.

Moved out of `SpiderMitePipeline` on 2026-09-23. Downstream, the production
pipeline starts from the 5 fps video this tool writes and identifies it by
SHA-256; nothing here imports from, or is imported by, either repo.

**The author is the domain expert.** Cut-point rules below change what data
exists downstream. If a change seems to require breaking one, stop and ask.

## 1. Running

Needs Python with PySide6 (GUI only; headless is standard library) and
`ffmpeg`/`ffprobe` on the PATH. Use any Python environment that has PySide6
(`pip install PySide6`) for the GUI and the tests:

```bash
python stylet.py                    # GUI
python stylet.py --headless job.json [--dry-run]
python -m pytest tests/ -q          # 124 tests
```

`python stylet.py --check` reports ffmpeg/ffprobe, encoders, NVENC (a real one-frame
encode), PySide6 and the BRAW helper; headless and the GUI fail early with the install
command when ffmpeg is missing, and the GUI selects the CPU encoder when NVENC is not
usable. ffmpeg is never bundled. MIT licence (`LICENSE`); `pip install .` works.

Source recordings are **read-only inputs**. Single-file sources are never
deleted (only copies); chunk intermediates may be *[DD V.9]*.

## 2. GUI for choosing, headless for replaying (since 2026-09-24)

Choosing a crop centre or a phase boundary still needs a human watching real
frames — that is what the GUI is for. **Replaying** a choice does not: every
encode writes `<name>.encode.json` before ffmpeg starts, and phase bounds live
in `<name>.phases.json`, so a crop or split can be redone headless by editing
the record and running `--headless`. The author reversed the old "GUI-only by
design" decision for this reason *[DD G.1 excerpt, amended]*. Keep all logic
in `stylet_core.py`; the GUI only collects choices.

A job file (all sections optional, run in stage order; `ingest` turns `.braw` into
a ProRes intermediate that later `encode` entries naming the `.braw` then use):

```json
{"ingest": [{"source": "A006.braw", "out_dir": "..."}],
 "ingest_settings": {"helper": null, "scale": 1, "profile": 4, "overwrite": false},
 "encode": [{"source": "A006.mov", "mode": "two_arena|single|none",
             "arenas": ["left", "right"], "click_x": 2048,
             "time_mode": "full|chunked", "chunk_minutes": 30, "out_dir": "..."}],
 "encode_settings": {"encoder": "nvenc|cpu", "codec": "h264|h265", "quality": 20,
                     "nvenc_preset": "p5", "cpu_preset": "medium",
                     "target_h": 1080, "overwrite": true},
 "phases": [{"source": "x_left_crop_nvenc.mp4"},
            {"source": "y.mp4", "mode": "Phases", "phases": [
               {"name": "baseline", "start": "00:00:00", "end": "00:10:00"},
               {"start": "00:12:00", "end": "00:30:00"}, {"start": "00:30:00", "end": ""}]},
            {"folder": "...", "group": "x_left_crop_nvenc.mp4", "delete_chunks": false}],
 "fps": [{"source": "x_left_crop_nvenc_phase2.mp4", "target_fps": 5}],
 "fps_settings": {"encoder": "nvenc", "quality": 20}}
```

Output options (encode and fps): `codec` h264|h265|prores|ffv1, `pix_fmt`
(default: 8-bit 4:2:0 for encode; fps **keeps the source's** bit depth/chroma when
the encoder can write it), `container` mp4|mov|mkv, `prores_profile`. Impossible
combinations are refused. Scaling is always CPU Lanczos a=3 *[DD V.2]*.

A `phases` entry without `mode`/timestamps uses the saved `.phases.json`.
Headless encodes need an explicit `mode` — nothing is guessed.

## 3. Behaviour to preserve

Deliberate; keep through any future refactor. *[DD V.*]*
- **Three stages stay separate by concern** (Encode/Split → Phase split/Merge →
  fps Limiter). The 1024×1080 archive is canonical and immutable; derivatives
  are regeneratable. *[DD V.1]*
- **`-hwaccel cuda` only for NVDEC-able source codecs** — ProRes decodes on CPU
  (CUDA-decoding ProRes fails hard). *[DD V.2]*
- **Phase splitting is lossless stream-copy; the start snaps FORWARD** to the
  first keyframe at or after the requested time, never backward. Backward snap
  pulled handling-period frames into phase 2 — a data-integrity bug, fixed. A
  clip may start up to one GOP late; it must never start early. **Do not
  restore the backward snap.** Frame-exact cuts with `-c copy` were rejected
  (broken-reference GOP). *[DD V.3]*
- **The between-phases fragment is ALWAYS kept**, never optional — handling-
  period movement is data. *[DD V.4]*
- **Any number of phases (`"mode": "Phases"`)** follows the same rules as the two-phase
  modes. Phases run in time order and may not overlap; each is cut with the
  forward-snapping stream copy; **every gap between consecutive phases is kept as
  `fragment<N>`** (N = the phase before it; touching phases have none). Output labels:
  `phase<N>` or `phase<N>-<name>` (name = letters, digits, `-`). The last end may be
  blank = end of video. The old three modes and their names (`phase1`, `fragment`,
  `phase2`) are unchanged and pinned to v1. *[DD V.13]*
- **Phase timestamps are saved to `.phases.json` before any ffmpeg runs**
  (crash safety); `parse_hms` is typo-tolerant. *[DD V.5]*
- **Wholesale NVENC/CUDA init failures = NVIDIA driver version mismatch**, not
  a code fault → reboot / realign driver; the CPU encoder is the keep-moving
  workaround. *[DD V.6]*
- **Qt widgets are touched only from the main thread** (workers emit `Bus.call`);
  the old Tk worker-thread access segfaulted. ffmpeg pipes are read as binary with `errors="replace"`.
  *[DD V.7]*
- **Single-file sources are never deleted** (only copies); chunk intermediates
  may be. All selection checkboxes default unchecked. *[DD V.9]*
- **Output names and sidecar formats are fixed**: downstream tools parse the
  names (`_left_crop_nvenc_phase2_5fps`). `test_equivalence.py` pins them to v1.


## 4. Provenance — partly closed

Four sidecars now exist (`<name>.ingest.json` for BRAW, `<out>.fps.json` for fps, since 2026-10): `<path>.frame_audit.json` (frame-drop audit),
`<name>.phases.json` (phase bounds) and, since 2026-09-24,
`<name>.encode.json` (source, crop mode, arenas, crop centre, encoder
settings, source probe, audit, the resolved filters and the exact ffmpeg
commands). Arena side and crop geometry are therefore no longer recoverable
only from the filename. Still open *[DD H, J.12]*: the sidecars are separate
files, not one record. That is the natural
thing for the downstream pipeline to attach as declared origin later.

## 5. How to work

Small, reviewable changes; show the diff; commit only when asked. Fix only
what was asked and report anything else. If a change makes a statement in this
file or in `design_decisions.md` false, updating the doc is part of the change.
