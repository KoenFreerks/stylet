# Design decisions — Stylet (formerly "video standardizer")

Copied **verbatim** from `SpiderMitePipeline/design_decisions.md` at commit
`c03e9c4` (2026-09-23), when `video_pipeline.py` moved to this repo. Section
letters are kept, so a citation like *[DD V.3]* means the same thing in both
repos. Cross-references to sections that stayed behind (A–G, I, J) point to
the SpiderMitePipeline copy. From here on, **this file is the live record for
the video tool**; the SpiderMitePipeline copy is history.

---

## V. Video preprocessing pipeline (upstream of DLC)

Decisions for the `video_pipeline.py` tkinter tool that turns raw camera
recordings into the DLC-geometry, analysis-ready clips section I consumes. One
GUI, three tabs: **Encode / Split**, **Phase split / Merge**, **fps Limiter**.
All claims verified against `video_pipeline.py`. It is widget-coupled
throughout and, by the G.1 Exception, **stays a GUI** (no headless-core split);
the decisions below are about *behaviour*, which is deliberate and worth
preserving as-is.

### V.1 Three pipeline stages kept separate by concern

**Context.** Raw recordings are 4K ProRes (4096×2160 two-arena, or 1920×1080
single-arena) at 30 fps on Blackmagic cameras; DLC needs exactly 1024×1080
clips; analysis sometimes needs reduced fps; long recordings sometimes arrive
as multiple camera chunks.

**Decision.** Three stages, each rerunnable and each owning one concern:
1. **Encode / Split** — ProRes → 1024×1080 DLC-geometry archive (full fps, full
   duration). Two-arena 4K is cropped into two 1024×1080 halves with Lanczos
   downscale; single-arena 1920×1080 is cropped to 1024×1080 with **no** scale
   (no resample). This archive is canonical and immutable.
2. **Phase split / Merge** — losslessly concatenate camera chunks and/or split a
   recording into analytical phases (V.3, V.4). Derivatives, regeneratable.
3. **fps Limiter** — reduce fps for faster DLC analysis. Analysis-specific,
   regeneratable, rerunnable.

**Why.** Archives are expensive to produce (CPU ProRes decode) and must not be
re-derived casually; phase clips and fps-reduced clips are cheap and
experiment-specific, so they are downstream of the archive and disposable.
Separating concerns means a change to the analysis fps never forces a re-encode.

**Risks / open.** The stage boundaries live only in the GUI's tab structure and
the filename suffixes (`_crop`, `_merged`, `_phaseN`, `_fragment`, `_Nfps`);
there is no manifest tying a derivative back to its archive except the filename,
which H says not to rely on. See H's provenance gap.

### V.2 GPU encode but CPU decode for ProRes; hwaccel only for NVDEC-able codecs

**Context.** NVENC is available and fast for H.264/H.265 *encode*; ProRes is
**not** NVDEC-decodable.

**Decision.** `-hwaccel cuda` is added **only** when the source codec is in
`NVDEC_DECODABLE` (`{h264, hevc, vp9, av1, mpeg2video, mpeg4, vc1}`). For ProRes
the decode runs on CPU and only the encode runs on NVENC. Scaling (Lanczos) is a
CPU filter regardless. NVENC quality settings are fixed: `-preset p5 -tune hq
-rc vbr -cq 20 -b:v 0 -rc-lookahead 32 -spatial-aq 1 -temporal-aq 1
-aq-strength 8 -bf 3 -b_ref_mode middle -pix_fmt yuv420p -movflags +faststart`.

**Why.** Requesting CUDA decode on a ProRes input fails hard with
`device type cuda needed for codec prores`. Gating hwaccel on the source codec
keeps ProRes on the working CPU-decode path while still using the GPU where it
actually helps.

**Risks / open.** When the encoder dropdown is set to CPU, hwaccel is skipped
entirely (clean CPU-only path), which is also the workaround when the NVIDIA
driver is in a mismatched state (V.6).

### V.3 Phase splitting is lossless stream-copy; start snaps FORWARD (never backward)

**Context.** Experiments have a phase 1 (single male) and, after the handler
adds intruders, a phase 2 (multi-male, often hours). The male-adding period must
be excluded from both analytical phases. Phase boundaries are read by eye from
the original 4K video, so they are approximate by nature.

**Decision.** Phase trims stay **lossless stream-copy** (`-c copy`, no re-encode;
`-avoid_negative_ts make_zero` rezeroes PTS so DLC sees a normal video), but the
start now snaps **forward to the first keyframe at or after the requested time,
never backward**. `concat_trim_phase` probes the archive's keyframes (ffprobe,
windowed with `-read_intervals` so it stays fast on multi-hour files), takes the
first keyframe `>=` the requested start, and input-seeks there (`-ss` before
`-i`, plus a small `+0.05 s` so the seek lands on *that* keyframe, not the
previous one). The end is held at `end_sec`. If no keyframe is found at/after the
start within the search window (e.g. a handling fragment shorter than one GOP),
it falls back to the old backward-snap so the clip is still produced.

**Why.** A stream copy can only begin on a keyframe, and these archives set no
explicit `-g`, so the encoder's default GOP governs the snap (measured: **250
frames ≈ 8.33 s** at 30 fps). The **previous behaviour snapped *backward*** to
the preceding keyframe, so a phase-2 clip could begin up to one GOP *before* the
typed start — pulling handling-period frames (the brush in view) into the
analysed footage. That is a **data-integrity** failure: contaminating frames
enter phase 2 silently, and the only guard was a human start-pad. Forward-snap
guarantees the clip **never begins before the requested time** — starting a few
seconds late is harmless (phase 2 is hours long and the boundary is eyeballed),
starting early is not. Stream-copy is retained because it is instant and
bit-exact; re-encoding for a frame-exact cut loses a generation and is slow, and
`-ss` *after* `-i` with `-c copy` was rejected earlier for producing a
broken-reference GOP (smear until the next keyframe).

**Trade-off.** The start is now up to one GOP **late, by a variable amount**
(the gap to the next keyframe) — where it used to be up to one GOP *early*. This
buys the never-before-requested guarantee at the cost of a few dropped lead-in
seconds. The ~10 s start-pad the user used to type to cover the old backward
snap is no longer needed for contamination safety (it is now merely harmless).

**Alternatives rejected.** Re-encoding for frame-exact cuts (loses a generation,
slow). Backward-snap plus human start-padding (the prior decision) — replaced,
because the pad is a discipline not enforced in code and one missed pad
contaminates phase 2. Tightening the encode GOP to shrink the snap window is
still deferred (enlarges archives, helps only future encodes) and is orthogonal
to the snap *direction*.

**Verified.** On a real archive (`..._crop_nvenc.mp4`, GOP 250 / 8.33 s), a
requested `phase2_start` of 1830.0 s produced source-frame **1825.0 s** under the
old backward snap (inside the [1810, 1830] handling window) and **1833.33 s**
under the new forward snap (after the boundary).

**Risks / open.** (1) Forward-snap drops up to one GOP at the *start* of every
clip, the handling fragment included; a fragment shorter than one GOP has no
in-window keyframe and falls back to backward-snap, so a very short fragment can
still straddle the boundary — acceptable, since the fragment is handling data by
definition (V.4). (2) Each cut now runs a windowed ffprobe keyframe scan (cheap).
(3) `end_sec` is held exactly, so if the GOP ever grows the only cost is a larger,
variable late-start — never early contamination.

### V.4 The between-phases fragment is always kept, never optional

**Context.** Between phase 1 and phase 2 there is a handling gap (intruders being
introduced). Mites move during this gap.

**Decision.** Whenever `Phase 1 + Phase 2` is selected and `phase2_start >
phase1_end`, the tool **always** emits a `_fragment_` clip covering the gap. It
is not behind a checkbox, and it is never deleted by the "delete chunks after
merge" option. A zero-length gap (`phase2_start == phase1_end`) is silently
skipped.

**Why.** Movement during handling is real behavioural data; discarding it would
throw away potentially vital information. Making it mandatory removes the chance
of a user (e.g. a master student) forgetting to keep it.

**Risks / open.** None significant; worst case is some unused fragment clips on
disk.

### V.5 Crash-safety: phase sidecars saved before any ffmpeg, and on focus-out

**Context.** A merge/phase batch can run for a long time on hours-long videos and
can die mid-run (out-of-disk, USB hiccup, driver crash). Re-typing all phase
timestamps after a crash is painful and error-prone.

**Decision.** Phase timestamps persist to `.phases.json` at three points:
(1) **on field focus-out / mode change** (auto-save while typing),
(2) **upfront, immediately after the confirm dialog is accepted and before any
ffmpeg runs**, and (3) after each successful operation (now redundant with the
upfront save). On the next launch / re-scan the GUI reloads the sidecar and
pre-fills the fields, so a crash costs only a re-confirm, never re-typing.

**Why.** The expensive, irrecoverable thing is the human's typed judgement about
where the phases are; the ffmpeg work is reproducible. Persisting that judgement
before risking it is cheap insurance. Time parsing is also typo-tolerant
(`parse_hms` accepts `;`, `.`, `,` as separators for `:`).

**Risks / open.** Sidecars accumulate next to videos; they are intentionally
listed under "Untouched" in the plan, not cleaned up.

### V.6 NVENC/NVDEC failures are almost always a driver-version mismatch

**Context.** GPU operations have twice failed wholesale with
`CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE` / `forward compatibility was
attempted on non supported HW`, every file failing identically before a frame is
processed.

**Decision.** Treat this signature as a **userspace/kernel NVIDIA driver version
mismatch** (e.g. client 535.309.01 vs kernel module 535.288.01 after an
unattended driver upgrade without reboot), **not** a code or hardware fault.
First remedy is a reboot; if that fails, reinstall/realign the driver
(`apt install --reinstall nvidia-driver-535`) or rebuild the DKMS module. The
immediate workaround that keeps work moving is switching the encoder dropdown to
**CPU**, which skips `-hwaccel cuda` entirely.

**Why.** CUDA refuses to initialise across mismatched driver versions; ffmpeg
surfaces that as a device-creation failure that looks like a hardware-support
error but isn't. The mismatch arises when a driver package updates but the
loaded kernel module is stale until reboot. Confirmed against `dmesg`
(`NVRM: API mismatch`).

**Risks / open.** Pin the NVIDIA driver or blacklist it from unattended-upgrades
so the mismatch state doesn't recur silently between reboots.

### V.7 Thread-safety: GUI widgets touched only from the main thread

**Context.** A long merge crashed with `Bus error (core dumped)`; `dmesg` showed
the fault inside `libtk8.6.so` from a worker thread — not in ffmpeg.

**Decision.** All Tk access from background `task()` threads is marshalled to the
main thread via `root.after(0, …)`. `log()` checks the current thread and
defers; `safe_messagebox()` wraps all in-thread dialog calls; `BatchProgress`
updates the same way. ffmpeg stderr/stdout pipes are read as **binary** with
`errors="replace"` (Blackmagic metadata is not always valid UTF-8).

**Why.** Tkinter is not thread-safe; touching widgets from a worker thread
corrupts Tk's internal state and segfaults under load. Marshalling every UI
update onto the main thread is the standard, required idiom.

**Risks / open.** Any newly-added UI update from inside a worker must follow the
same discipline. Because this tool stays GUI-only (the G.1 split that would
have made the invariant *structural* is off the table for it), the invariant
remains **convention-enforced** — it has to be held by review, not by
architecture. See J.13.

### V.8 Batch progress is per-file, no ETA

**Context.** Batches of long videos run unattended; the user wants to know how
far along a batch is, but ETAs depend on the machine and were explicitly not
wanted.

**Decision.** Each tab shows a determinate `BatchProgress` bar that advances
**per input file**, labelled "N of M · filename", green on clean completion /
amber with a failed count otherwise. No ETA, no per-file sub-progress.

**Why.** "12 of 50 done" is the decision-relevant number (wait, leave, or come
back) and is machine-independent; a per-output or time-based bar adds noise and
an unwanted, unreliable ETA. Per-file granularity matches how the user thinks
about a unit of work (a two-arena encode is one file even though it is two
encodes).

**Risks / open.** None significant; the bar is advisory.

### V.9 Single-file phase split unified with chunk merge in one tab

**Context.** Recordings sometimes arrive as multiple camera chunks (needing
lossless concat) and sometimes as a single already-complete file (needing only
phase splitting). Both then want the same phase-split workflow.

**Decision.** The **Phase split / Merge** tab handles both. Chunk groups are
discovered by `CHUNK_RE` and validated for contiguous indices + matching stream
params before a lossless `concat` demuxer pass. Single video files (not chunks,
not prior outputs — `OUTPUT_RE` excludes `_merged`/`_phase1`/`_phase2`/
`_fragment`) appear in their own "Single files" section with the same phase UI.
For chunked groups the intermediate full merge is deleted after phase clips
succeed; for single files the **source is never deleted** (it is the only copy).
All selection checkboxes default **unchecked** (deliberate safety for a master
student), with Select-all / Deselect-all helpers.

**Why.** One conceptual operation (source → optional phases) regardless of
whether the source needed concatenation first. Unchecked-by-default means
nothing is processed by accident; the source-never-deleted rule for single files
prevents destroying an irreplaceable recording.

**Risks / open.** The "delete chunks after merge" option applies only to chunked
groups; this is stated in the confirm dialog but relies on the user reading it.

---


---

## H. Provenance

> **2026-09-24:** partly implemented. Each encode now writes
> `<name>.encode.json` (source, mode, arenas, crop centre, settings, probe,
> audit, resolved filters and commands) before ffmpeg runs. Still separate
> from `.phases.json` and `.frame_audit.json`, and the fps step writes no
> record, so the single unified sidecar below remains open.

**Context.** One source recording fans out into up to **2 arenas, each
optionally split into 2 analytical phases plus a between-phases fragment**
(phase1 / fragment / phase2) — so at most 2 arenas × 3 pieces = 6 derived
videos, but the 3 pieces are **2 phases + 1 fragment, not 3 phases** (V.3,
V.4). **Filenames are not parsed downstream** — nothing depends on the naming
scheme.

**Decision (PLANNED, not yet implemented).** The encode step *should* persist
the facts it already computes — source path, arena side, crop geometry, phase
bounds, frame-drop audit — to a **unified provenance sidecar** next to each
derived video, and **couple nothing to it** until the statistics layer needs
the design factors.

**Current reality.** No unified provenance sidecar exists. The code writes
**two separate, single-purpose sidecars**:
- **`<path>.frame_audit.json`** — frame-drop audit, keyed on source mtime
  (drop count, duration, detection method).
- **`<key/basename>.phases.json`** — phase config only
  (`{mode, phase1_start, phase1_end, phase2_start, phase2_end}`).

Nothing ties together source + arena side + crop geometry + phase bounds +
audit for a derived video. Arena side and crop geometry live only in the
in-memory `encode_file_configs` during a session and in the output **filename**
(`_left_crop` / `_right_crop`) — which this section says not to rely on. So the
unified sidecar is **recorded intent, not behaviour** (see §7 of `CLAUDE.md`).

**Partially addressed at the analysis end, not at the video end.** The contest
detector now writes its own provenance (F): `*_contests.provenance.json` plus
provenance columns in the frozen CSV, and it carries `recording_id` / `arena` /
`phase` as **columns**. Those nesting factors are obtained by parsing the input
stem **once, at analysis time**, in `interaction_detect.parse_nesting_from_stem`
— the deliberate, single, sanctioned exception to "filenames are not parsed
downstream". Everything downstream reads the columns, never a name; an
unparseable stem yields empty values plus a sidecar warning rather than a
failure.

**Why concede a filename parse at all.** The alternative was worse: without it
the frozen CSV carries no recording, arena or phase identity, so pooling rows
for the nested mixed models this section anticipates would have *forced* every
downstream consumer to parse filenames — the exact coupling H exists to
prevent. Confining the parse to one function at the boundary, and emitting
columns from it, keeps the prohibition intact everywhere else.

**Still open.** This is contest-level provenance only. The **video-level**
unified sidecar (source + arena side + crop geometry + phase bounds + frame
audit, written at encode time) does not exist, so arena/crop facts for a
derived video remain recoverable only from its filename. J.12 stands.

**Why.** Today the experimental design lives only in the author's head and the
stats layer is future work; when it arrives, a nested mixed model will need
recording/arena/phase as grouping factors. Persisting now would be one
`json.dump` of in-hand facts — insurance against the design being
irrecoverable later. Premature coupling (downstream tools depending on
sidecars or filenames) is as bad as no provenance; wire it deliberately only
when the stats layer needs it.

**To resolve.** Either implement the unified sidecar write in the encode step,
or leave as planned — but do not let the wording imply the persistence already
happens.

---

## G.1 (excerpt) — the GUI-only exception

> **Amended 2026-09-24 (author): headless replay added.** The reasoning below
> still holds for *choosing* cut points — that needs a human watching frames,
> and the GUI stays for it. What changed: a choice, once made, is now
> recorded (`<name>.encode.json`, alongside the existing `.phases.json`) and
> can be replayed without the GUI (`stylet.py --headless`), so a
> crop or split can be redone from its record. All logic moved into
> `stylet_core.py` (no GUI import); the GUI was rewritten as a plain tkinter
> front end (`video_standardizer.py`, 640 lines, from 3040), with every
> ffmpeg command, output name and sidecar format pinned to v1 by
> `tests/test_equivalence.py`. J.13(a) — synthetic tests for the helpers — is
> done.

**Exception — `video_pipeline.py` is GUI-only by design (decided).** Arena-crop
and phase-boundary selection require a human watching actual frames to choose
the cut points; that is an irreducibly interactive visual task with no headless
equivalent, so there is nothing to batch. The GUI **is** the tool, not a shell
around batch logic — do **not** extract a `core/`+`batch.py` for it, and do not
treat it as pending refactor. The deterministic helpers (`parse_hms`, crop
geometry, the mtime-cached frame audit, phase/fragment logic, the ffmpeg
progress runner) stay testable in place; that does not require a core. This
takes the video-pipeline split off the refactor backlog (see J.13).


---

## J (excerpt) — open risks for the video tool

12. **Video-pipeline provenance sidecar is decided but not implemented** (H,
    V.1): arena side and crop geometry currently live only in the in-memory
    session config and in filenames (which H says not to rely on). The unified
    source+arena+phase+crop+audit sidecar described in H does not exist; only
    `.frame_audit.json` and `.phases.json` are written, separately. Wire it
    before the stats layer needs grouping factors, or the design is recoverable
    only from filenames.
13. **Video pipeline is GUI-only by design — the headless-core split is OFF the
    backlog** (G.1, V.*). Decided: arena-crop and phase-boundary selection are
    irreducibly interactive (a human picks cut points from real frames), so
    there is no headless equivalent to batch and no `core/`+`batch.py` will be
    built. Two residual watch-items survive the decision, since the split is no
    longer the mechanism that would have covered them: (a) the deterministic
    helpers (`parse_hms`, crop geometry, the frame audit, phase/fragment logic,
    the ffmpeg runner) should still get synthetic tests — **in place**, not via
    an extracted core; (b) the thread-safety invariant (V.7 — all Tk access
    marshalled to the main thread via `root.after`) stays **convention-enforced,
    not structurally enforced**, so every newly-added worker-thread UI update
    must follow the idiom or it segfaults under load.


## Amendments, 2026-10 (flexible standardizer)

### V.7 (amended) Qt replaces tkinter
The GUI is now Qt (PySide6, `stylet_gui.py`). The invariant is unchanged: worker
threads never touch a widget; they emit `Bus.call` with a callable, which Qt
queues to the main thread. ffmpeg pipes are still read as binary.

### V.10 BRAW is ingested once, to a ProRes intermediate
ffmpeg cannot decode Blackmagic RAW; the free Blackmagic RAW SDK can.
`braw/braw2pipe` (built from the SDK) writes rgb48le frames; ingest pipes them
into ffmpeg → ProRes 4444 (`<base>.mov`, BT.709 tagged, written as `.part` and
renamed on success, reused when frame count and size match). Every later stage
then treats the file like camera ProRes, so crops, grids, probing, the audit and
the NVDEC rules have no BRAW special cases. **Why not decode per output:** a
two-arena or grid encode would decode the RAW once per region. **Cost:** disk
(the intermediate is kept; delete it when done). **Open:** the helper is
uncompiled against the SDK; colour (SDK default processing) is not yet
configurable; the camera-side frame-drop audit does not apply to BRAW.

### V.11 Layouts replace the three crop modes (which remain, pinned to v1)
A layout is a set of named regions (box, centre, or grid) with an optional output
size. Per region: crop → scale → format; one resample, **Lanczos a=3 on the CPU**
(it keeps fine features; never GPU scaling); no resample when the region already
has the output size. Aspect distortion and upscaling are warned about, not blocked.
Open: the final `format=` conversion is done by swscale with its default flags,
not Lanczos (chroma only; unchanged to keep outputs identical).

### V.12 The fps stage keeps what the source has
Default output unchanged for 8-bit 4:2:0 sources (same command and name as v1).
A 10-bit or 4:2:2/4:4:4 source keeps its format where the encoder can write it,
with a logged note when it cannot. `<out>.fps.json` is written before ffmpeg and
is a valid job file.

### V.13 Any number of phases; numbered fragments
The two-phase modes could not describe designs with several treatments or
repeated cycles. Mode "Phases" takes an ordered list of phases, each optionally
named. **Unchanged rules:** stream-copy cuts that snap forward [V.3]; the gap
between consecutive phases is always kept [V.4]; times saved to `.phases.json`
before ffmpeg [V.5]. **Naming:** `phase<N>[-name]`; gaps `fragment<N>` (N = the
phase before the gap), so adding a phase never renames existing clips. The old
modes keep `phase1`/`fragment`/`phase2`. Names exclude `_` and `.` because the
folder scan and downstream tools split file names on them. The folder scan
(`OUTPUT_RE`) skips every `phase<N>[-name]` / `fragment[<N>]` / `merged` output.
**Not changed:** time before the first phase and after the last is not written
(as before); touching phases lose up to one GOP between them to the forward
snap (as before) -- leave a gap if that footage matters, it will be kept.
