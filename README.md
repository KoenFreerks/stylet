# Stylet

**Prepare camera recordings for downstream analysis of behavioural experiments.**
Stylet compresses, standardises, crops and splits raw video so that tracking and
scoring pipelines get small, uniform, analysis-ready files instead of hundreds of
gigabytes of camera originals.

It was built for Blackmagic Pocket Cinema Camera 4K recordings (ProRes, BRAW), but
nothing is camera-specific: any resolution, frame rate or codec that FFmpeg can
read works, so people with similar cameras can use it as is.

## What it does

| Stage | What | Typical use |
|---|---|---|
| **Encode / Split** | Crop, scale and re-encode; full length or in chunks | One arena from a wide frame; **a grid of arenas** from one recording |
| **Phase split / Merge** | Merge chunks losslessly; cut **any number of named phases**, keeping every gap between them as a numbered fragment | Baseline / treatment / recovery, repeated cycles, without re-encoding |
| **fps Limiter** | Re-encode at a lower frame rate | A 5 fps copy for tracking |
| **BRAW ingest** | Decode Blackmagic RAW once to a ProRes intermediate | Raw clips (needs a helper, see below) |

### Crops and grids (multi-arena setups)

- **Single crop:** click the centre of the arena on a real frame; the box is clamped
  inside the frame.
- **Arena split:** the classic left/right two-arena layout.
- **Grid:** rows × cols for array setups (for example a 2 × 4 plate of arenas on one
  camera view). Set the cell size, spacing, origin, per-cell nudges and cells to
  skip. Each cell becomes its own video, named `r1c1`, `r1c2`, and so on.
- **Any output size:** native, or a fixed width × height. A preview shows the real
  output of every region before you encode, and you are warned if the output would
  distort the image or upscale it.
- **Scaling keeps fine features:** every resize is Lanczos (a = 3) on the CPU, one
  resample per region, and none at all when the region already has the output size.

### Compression and formats

- Codecs: H.264, H.265 (NVIDIA GPU or CPU), ProRes and lossless FFV1 (CPU).
- 8-bit or 10-bit; 4:2:0, 4:2:2 or 4:4:4; mp4, mov or mkv. Impossible combinations
  are refused with a clear message.
- Loading a video shows what it is before anything runs: resolution, codec, bit depth,
  frame rate (and whether it is variable), duration, size, timecode and warnings.
- The frame-drop audit tells you if the camera or the recording drive lost frames.

## Why standardise before tracking

**Resolution.** Camera originals (4096 × 2160 and up) are far more pixels than pose
estimators (DeepLabCut, SLEAP, ...) or segmentation-based trackers (idtracker.ai,
TRex, ...) need. Per-frame cost grows with pixel count, so training, inference and
tracking get slower and files get bigger for no gain once the animal is already
resolved well enough. Pick the smallest resolution where the animal (and the body
parts you label) still span enough pixels, and every downstream tool works on small,
identical videos. The original recording stays untouched as the archive.

**Phases and individual number.** In many experiments the number of animals in the
arena changes during one recording: you start with one individual, then add a second
or third. Trackers are usually told how many individuals to expect (idtracker.ai and
TRex are configured with the number of animals; multi-animal pose projects declare
their individuals), and they behave best when that number is constant for the whole
video. Cutting the recording at the moments individuals are added gives you one video
per number of animals, each tracked with the right setting. The moment of handling
itself is not discarded: the gap between two phases is always saved as its own
`fragment` clip, because movement there is data too.

## Examples

Each example is the job file you can run with `python stylet.py --headless job.json`
(add `--dry-run` to print the commands first). Everything here can also be done in the
GUI; the crop dialog and the Phase tab write the same settings.

### 1. Downscale 4K to 1080p for pose estimation or tracking

Keep the whole frame, shrink it. `mode: none` keeps the aspect ratio and scales to
`target_h` (Lanczos, so fine features such as legs and antennae survive):

```json
{"encode": [{"source": "A006.mov", "mode": "none"}],
 "encode_settings": {"encoder": "nvenc", "codec": "h264", "quality": 20, "target_h": 1080}}
```

Writes `A006_full_nvenc.mp4`. For any other size, use a layout with an explicit output
(here the full 4096 × 2160 frame to 1920 × 1012, the same aspect ratio):

```json
{"encode": [{"source": "A006.mov",
             "layout": {"regions": [{"name": "full", "x": 0, "y": 0, "w": 4096, "h": 2160}],
                        "output": {"w": 1920, "h": 1012}}}]}
```

### 2. Crop one arena

Click the arena centre on a real frame (GUI), or give it in the job. The box is the
standard 1024 × 1080 arena shape, clamped inside the frame:

```json
{"encode": [{"source": "A006.mov", "mode": "single", "click_x": 2048}]}
```

Writes `A006_crop_nvenc.mp4`. For an arbitrary box size and position use a layout with
a centre: `{"regions": [{"name": "arena", "cx": 2048, "cy": 1080, "w": 1600, "h": 1200}]}`.

### 3. Two arenas side by side

Splits the frame into a left and a right arena, one video each (use `"arenas": ["left"]`
to keep only one side):

```json
{"encode": [{"source": "A006.mov", "mode": "two_arena"}]}
```

Writes `A006_left_crop_nvenc.mp4` and `A006_right_crop_nvenc.mp4`.

### 4. A grid of arenas, downscaled

A 2 × 4 array of arenas in one view, each cell cropped and then halved to 512 × 540 for
the tracker. Cells of 1024 × 1080 halve exactly, so the aspect ratio is unchanged and
no warning appears. Use `cell_w`, `cell_h`, `x0`, `y0`, `pitch_x`, `pitch_y`, `offsets`
and `skip` when the arenas are not an even tiling of the frame:

```json
{"encode": [{"source": "plate01.mov",
             "layout": {"grid": {"rows": 2, "cols": 4},
                        "output": {"w": 512, "h": 540}}}]}
```

Writes `plate01_r1c1_nvenc.mp4` ... `plate01_r2c4_nvenc.mp4`: eight videos, one per
arena. If the output size would stretch the image, Stylet warns you before encoding.

### 5. Long recordings in chunks

Encode in 30 minute pieces (safer for multi-hour recordings), then merge them
losslessly and cut phases from the merged video:

```json
{"encode": [{"source": "A006.mov", "mode": "two_arena", "arenas": ["left"],
             "time_mode": "chunked", "chunk_minutes": 30}]}
```

Writes `A006_left_crop_chunk000_nvenc.mp4`, `..._chunk001_...` and so on. The Phase tab
finds the chunk group and merges it before splitting (next examples); with no split it
just writes `A006_left_crop_merged_nvenc.mp4`.

### 6. One individual, then more: cut by number of animals

Phase 1 has one individual, someone adds a second at about 20 minutes, and from 23
minutes the arena holds two. The handling period (20:00 to 23:00) is kept as its own
fragment:

```json
{"phases": [{"source": "A006_crop_nvenc.mp4", "mode": "Phase 1 + Phase 2",
             "phase1_start": "00:00:00", "phase1_end": "00:20:00",
             "phase2_start": "00:23:00", "phase2_end": "01:00:00"}]}
```

Writes `A006_crop_nvenc_phase1.mp4` (track with n = 1), `A006_crop_nvenc_fragment.mp4`
(the handling) and `A006_crop_nvenc_phase2.mp4` (track with n = 2). A phase start
snaps forward to the next keyframe, so a clip may start a few seconds late but never
early: no handling frames leak into the tracked footage.

### 7. Any number of phases, with names

Individuals added one after another, or treatments repeated: list as many phases as you
need. Names are optional and become part of the file name. Leave the last `end` blank for
"until the end of the video":

```json
{"phases": [{"source": "A006_crop_nvenc.mp4", "mode": "Phases", "phases": [
              {"name": "n1", "start": "00:00:00", "end": "00:20:00"},
              {"name": "n2", "start": "00:23:00", "end": "01:00:00"},
              {"name": "n3", "start": "01:04:00", "end": ""}]}]}
```

Writes `A006_crop_nvenc_phase1-n1.mp4`, `..._fragment1.mp4` (20:00 to 23:00),
`..._phase2-n2.mp4`, `..._fragment2.mp4` (01:00 to 01:04) and `..._phase3-n3.mp4`. Each
`phase` video goes to the tracker with its own number of individuals. In the GUI, the
Phase tab's "Phases" mode has one row per phase and a "Split evenly..." button for N
equal pieces. For chunked recordings, point the entry at the chunk group instead:
`{"folder": "...", "group": "A006_left_crop_nvenc.mp4", "mode": "Phases", "phases": [...]}`.

### 8. A low frame rate copy for tracking

When the behaviour is slow, a 5 fps copy is much cheaper to track:

```json
{"fps": [{"source": "A006_crop_nvenc_phase2.mp4", "target_fps": 5}]}
```

Writes `A006_crop_nvenc_phase2_5fps.mp4`. It keeps the source's bit depth and chroma
when the encoder can write them, and writes `A006_crop_nvenc_phase2_5fps.fps.json` so the
step can be replayed.

### 9. The whole pipeline in one job

```json
{"encode": [{"source": "A006.mov", "mode": "single", "click_x": 2048}],
 "encode_settings": {"encoder": "nvenc", "quality": 20},
 "phases": [{"source": "A006_crop_nvenc.mp4", "mode": "Phase 1 + Phase 2",
             "phase1_start": "00:00:00", "phase1_end": "00:20:00",
             "phase2_start": "00:23:00", "phase2_end": "01:00:00"}],
 "fps": [{"source": "A006_crop_nvenc_phase1.mp4", "target_fps": 5},
         {"source": "A006_crop_nvenc_phase2.mp4", "target_fps": 5}]}
```

### 10. Archive quality, other codecs and BRAW

Keep more than 8-bit 4:2:0, for example a 10-bit ProRes archive of a crop, or a lossless
FFV1 copy:

```json
{"encode": [{"source": "A006.mov", "mode": "single", "click_x": 2048}],
 "encode_settings": {"encoder": "cpu", "codec": "prores", "pix_fmt": "yuv422p10le"}}
```

(`"codec": "ffv1"` writes lossless `.mkv`; `"codec": "h265", "pix_fmt": "yuv420p10le"`
writes 10-bit H.265.) For Blackmagic RAW, decode once first (needs the experimental
helper, see Status), then crop the intermediate as above:

```json
{"ingest": [{"source": "A006.braw"}],
 "encode": [{"source": "A006.braw", "mode": "single", "click_x": 2048}]}
```

## Install and run

Stylet needs three things: **Python 3.9+**, **FFmpeg** (which includes ffprobe) and, for the
GUI, **PySide6**. FFmpeg is a separate program that Stylet calls; it is not bundled.

**Easiest, any platform (conda):**

```bash
conda env create -f environment.yml     # Python, ffmpeg, PySide6
conda activate stylet
python stylet.py --check                # confirm everything is found
python stylet.py
```

**Or install the pieces yourself:**

| Platform | FFmpeg |
|---|---|
| Linux | `sudo apt install ffmpeg` (Fedora `dnf`, Arch `pacman`) |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`, or a build from ffmpeg.org with its `bin` folder on the PATH) |

then `pip install .` (or `pip install PySide6` and run from the folder); this gives a
`stylet` command. Headless runs need nothing beyond the Python standard library and ffmpeg.

```bash
stylet --check                         # what is installed, what works, what to fix
stylet                                 # the GUI
stylet --headless job.json             # replay a job file, no GUI
stylet --headless job.json --dry-run   # print the ffmpeg commands only
```

`--check` reports ffmpeg/ffprobe, which encoders exist (H.264, H.265, ProRes, FFV1), whether
the NVIDIA GPU encoder actually works, whether PySide6 and the BRAW helper are present, and
says how to fix anything missing. No NVIDIA GPU is needed: if the GPU encoder is not
usable, the GUI starts with the CPU encoder selected. If ffmpeg is missing, Stylet says so
at start-up with the install command for your system.

## Reproducible: every step leaves a job file

Choosing a crop centre or a phase boundary needs a person watching real frames, so
that part is a GUI. Replaying it does not. Every encode writes `<name>.encode.json`
before ffmpeg starts, and every fps step writes `<out>.fps.json`. Each is a valid
job file that records the source, the crop or grid, the settings, the source probe and
the exact ffmpeg commands. Edit one and run it with `--headless` to redo a crop, or
use it as the methods record for a dataset. A grid job looks like this:

```json
{"encode": [{"source": "plate01.mov",
             "layout": {"grid": {"rows": 2, "cols": 4},
                        "output": {"w": 1024, "h": 1080}}}],
 "encode_settings": {"encoder": "cpu", "codec": "h265", "quality": 22},
 "fps": [{"source": "plate01_r1c1_cpu.mp4", "target_fps": 5}]}
```

`CLAUDE.md` has the full job-file format.

## Data-integrity rules

These are deliberate and tested:

- Source recordings are read-only inputs and are never deleted.
- Phase splitting is a lossless stream copy, and a phase start snaps **forward** to
  the next keyframe, never backward, so frames from a neighbouring phase never leak in.
- Every fragment between phases is always kept (`fragment1`, `fragment2`, ...), since
  movement there is data.
- Phase times are saved before any ffmpeg runs, so a crash loses nothing.

## Status

- Tested: the crop, grid, codec, container and fps logic (124 automated tests, plus
  real ffmpeg encodes of synthetic ProRes clips) and the GUI under an offscreen display.
- **BRAW is experimental.** FFmpeg cannot decode Blackmagic RAW, so Stylet uses a small
  helper, `braw/braw2pipe`, that you build against the free Blackmagic RAW SDK with
  `braw/build.sh`. The helper has not yet been compiled against the SDK; the Python
  side is tested with a stand-in. Until you have built it, convert BRAW to ProRes
  yourself (for example in DaVinci Resolve) and load that.

## License

MIT, see `LICENSE`. Stylet does not include or redistribute FFmpeg, PySide6 or the Blackmagic
RAW SDK: each is installed separately and keeps its own licence.

- **FFmpeg** is LGPL 2.1 or later, but a build that enables GPL components (notably libx264,
  `--enable-gpl`) is GPL as a whole. Many distribution builds are
  GPL builds; `ffmpeg -version` shows `--enable-gpl` in its configuration. If you redistribute an FFmpeg build together with Stylet, you take on FFmpeg's
  obligations; see <https://www.ffmpeg.org/legal.html>. That page also notes that H.264 and
  other codecs may involve patents, and it is not legal advice.
- **PySide6** (Qt for Python) is LGPL.
- The **Blackmagic RAW SDK** is free to download under Blackmagic's own licence and is only
  needed to build the optional BRAW helper.

"FFmpeg" is a trademark of Fabrice Bellard, originator of the FFmpeg project. Stylet is not
affiliated with or endorsed by the FFmpeg project.

## Project files

- `stylet.py` is the entry point, `stylet_core.py` holds all the logic (no GUI imports),
  and `stylet_gui.py` is the Qt interface.
- `LICENSE` (MIT), `pyproject.toml` (`pip install .`), `environment.yml` (conda env).
- `CLAUDE.md` holds the rules that keep the cuts correct.
- `design_decisions.md` holds the reasoning behind them.

Downstream, the analysis pipeline starts from the 5 fps videos Stylet writes.
