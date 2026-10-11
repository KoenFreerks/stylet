"""
stylet_core.py -- Stylet's logic, without any GUI.

Three stages, kept separate by concern [DD V.1]:
  encode   raw recording -> 1024x1080 arena crop(s) (two-arena split, single
           crop centred on a chosen x, or no crop), full length or chunked
  phases   chunk merge (lossless) and/or phase split: phase1, fragment, phase2
  fps      re-encode at a lower frame rate for analysis

Used by the GUI and by headless job files (stylet.py --headless).
Every ffmpeg command, output name and sidecar format is unchanged from the
original single-file GUI (tests/legacy/video_pipeline_v1.py); the tests pin
that. Rules kept from design_decisions.md:
  - hwaccel cuda only for NVDEC-decodable sources; ProRes decodes on CPU [V.2]
  - phase trims are stream copies whose start snaps FORWARD to the first
    keyframe at or after the requested time, never backward [V.3]
  - the between-phases fragment is always written when there is a gap [V.4]
  - sidecars (.phases.json, .encode.json) are written before ffmpeg runs [V.5]
  - single-file sources are never deleted; merged chunks only on request [V.9]
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

NVDEC_DECODABLE = {"h264", "hevc", "vp9", "av1", "mpeg2video", "mpeg4", "vc1"}
DLC_W, DLC_H = 1024, 1080          # the tracking model's frame size
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi"}
# "Phases" is the general mode: any number of phases, optionally named. The
# other three are the original two-phase modes and keep their exact names.
PHASE_MODES = ("No split", "Phase 1 only", "Phase 1 + Phase 2", "Phases")


# ---------------------------------------------------------------------------
# Prerequisites: ffmpeg, ffprobe, encoders, NVENC, PySide6, BRAW helper
# ---------------------------------------------------------------------------
# Stylet calls ffmpeg and ffprobe as external programs (they are not bundled and
# keep their own licences). These checks say what is missing and how to get it.
CORE_ENCODERS = (("libx264", True, "H.264 on the CPU (the default CPU path)"),
                 ("libx265", False, "H.265 on the CPU"),
                 ("prores_ks", False, "ProRes output"),
                 ("ffv1", False, "lossless FFV1 output"))


def install_hint(platform=None):
    """How to install ffmpeg here, as one short text."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        how = ("winget install Gyan.FFmpeg    (or: choco install ffmpeg, or download a build "
               "from https://ffmpeg.org/download.html and add its bin folder to PATH)")
    elif platform == "darwin":
        how = "brew install ffmpeg"
    else:
        how = "sudo apt install ffmpeg    (Fedora: sudo dnf install ffmpeg; Arch: sudo pacman -S ffmpeg)"
    return (f"Install ffmpeg (it includes ffprobe), then restart Stylet:\n  {how}\n"
            "Any platform with conda: conda install -c conda-forge ffmpeg")


def missing_tools():
    """Names of required programs that are not on the PATH."""
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]


def require_ffmpeg():
    """Raise RuntimeError with install instructions when ffmpeg/ffprobe are absent."""
    miss = missing_tools()
    if miss:
        raise RuntimeError(f"{' and '.join(miss)} not found on the PATH.\n" + install_hint())


def _run_text(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def check_environment(smoke_nvenc=True):
    """[{name, ok, required, detail, hint}] for everything Stylet may use. Never
    raises. smoke_nvenc runs a one-frame NVENC encode (~1 s): a build that lists
    h264_nvenc still fails without a working NVIDIA driver [DD V.6]."""
    out = []

    def add(name, ok, required, detail, hint=""):
        out.append({"name": name, "ok": bool(ok), "required": required,
                    "detail": detail, "hint": hint})
    have = {}
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            rc, text = _run_text([tool, "-version"])
            add(tool, rc == 0, True, (text.splitlines() or [""])[0], "" if rc == 0 else install_hint())
            have[tool] = rc == 0
        else:
            add(tool, False, True, "not found on the PATH", install_hint())
            have[tool] = False
    if have["ffmpeg"]:
        _, enc = _run_text(["ffmpeg", "-hide_banner", "-encoders"])
        names = {ln.split()[1] for ln in enc.splitlines() if len(ln.split()) > 1}
        for name, required, what in CORE_ENCODERS:
            ok = name in names
            add(f"encoder {name}", ok, required, what if ok else f"missing: {what} unavailable",
                "" if ok else "use a full ffmpeg build (apt/brew/conda-forge builds include it)")
        if "h264_nvenc" not in names:
            add("NVENC (GPU)", False, False, "this ffmpeg has no NVENC encoders; use the CPU encoder")
        elif smoke_nvenc:
            rc, text = _run_text(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                  "nullsrc=s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"])
            add("NVENC (GPU)", rc == 0, False,
                "works" if rc == 0 else "listed but not usable: " + (text.strip().splitlines() or ["?"])[-1][:120],
                "" if rc == 0 else "needs an NVIDIA GPU and a matching driver (after a driver "
                "update, reboot); the CPU encoder works without it")
        else:
            add("NVENC (GPU)", True, False, "listed (not tested)")
    import importlib.util
    qt = importlib.util.find_spec("PySide6") is not None
    add("PySide6 (GUI)", qt, False, "installed" if qt else "not installed; --headless still works",
        "" if qt else "pip install PySide6")
    try:
        add("BRAW helper", True, False, find_braw_helper())
    except RuntimeError:
        add("BRAW helper", False, False, "not built (only needed for .braw files)",
            "see braw/build.sh and the README")
    return out


def format_check(items):
    lines = []
    for it in items:
        tag = "  OK   " if it["ok"] else ("MISSING" if it["required"] else " n/a   ")
        lines.append(f"[{tag}] {it['name']:<18} {it['detail']}")
        if not it["ok"] and it["hint"]:
            lines += ["          " + h for h in it["hint"].splitlines()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def humansize(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def even(x):
    """Snap down to an even integer (yuv420p needs even offsets/dimensions)."""
    return int(x) & ~1


def parse_hms(s):
    """hh:mm:ss -> seconds. Also accepts ';', '.', ',' as separators (typo
    neighbours of ':'). Raises ValueError on bad input [DD V.5]."""
    s = s.strip()
    if not s:
        raise ValueError("empty time")
    for ch in (";", ".", ","):
        s = s.replace(ch, ":")
    parts = s.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"'{s}' must be hh:mm:ss with digits only")
    h, m, sec = map(int, parts)
    if m >= 60 or sec >= 60:
        raise ValueError(f"'{s}': minutes/seconds must be below 60")
    return h * 3600 + m * 60 + sec


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def quote(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


# ---------------------------------------------------------------------------
# Probing and the frame-drop audit
# ---------------------------------------------------------------------------
def _ratio(text):
    """'30000/1001' or '16:9' -> float, or None when unusable ('0/0', 'N/A')."""
    try:
        n, d = re.split(r"[/:]", str(text))
        n, d = float(n), float(d)
        return n / d if d else None
    except (ValueError, TypeError):
        return None


def parse_probe(info, size_bytes):
    """ffprobe JSON -> the facts the three stages use. Pure, so it is testable
    without a camera file. Raises ValueError when there is no video stream."""
    streams = info.get("streams", [])
    cands = [x for x in streams if x.get("codec_type") == "video"
             and not x.get("disposition", {}).get("attached_pic")]
    if not cands:
        raise ValueError("no video stream found (is this a video file?)")
    v = cands[0]
    rfr = v.get("r_frame_rate", "30/1")
    fps, avg = _ratio(rfr), _ratio(v.get("avg_frame_rate"))
    fps_assumed = False
    if not fps or fps <= 0:
        fps = avg if avg and avg > 0 else None
    if fps is None:
        fps, fps_assumed = 30.0, True
    vfr = bool(avg and fps and abs(avg - fps) / fps > 0.01)
    nbf = v.get("nb_frames")
    try:
        nbf = int(nbf) if nbf and nbf != "N/A" else None
    except (TypeError, ValueError):
        nbf = None
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        duration = None
    pix = v.get("pix_fmt")
    try:
        depth = int(v.get("bits_per_raw_sample"))
    except (TypeError, ValueError):
        m = re.search(r"(9|10|12|14|16)(le|be)?$", pix or "")
        depth = int(m.group(1)) if m else 8
    m = re.match(r"yuva?j?(\d{3})p", pix or "")
    chroma = m.group(1) if m else ("rgb" if (pix or "").startswith(("rgb", "bgr", "gbr")) else None)
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"]))) % 360
    if not rot and v.get("tags", {}).get("rotate"):
        try:
            rot = int(v["tags"]["rotate"]) % 360
        except ValueError:
            pass
    w, h = int(v["width"]), int(v["height"])
    if rot in (90, 270):                       # ffmpeg autorotates: work in displayed pixels
        w, h = h, w
    sar = v.get("sample_aspect_ratio")
    sar_f = _ratio(sar) if sar not in (None, "N/A", "0:1") else 1.0
    tc = (v.get("tags", {}).get("timecode") or info.get("format", {}).get("tags", {}).get("timecode")
          or next((x["tags"]["timecode"] for x in streams if x.get("tags", {}).get("timecode")), None))
    return {"duration": duration, "width": w, "height": h,
            "codec": v.get("codec_name", "").lower(), "fps": fps, "r_frame_rate": rfr,
            "pix_fmt": pix, "nb_frames_meta": nbf, "size_bytes": size_bytes,
            "avg_frame_rate": v.get("avg_frame_rate"), "vfr": vfr, "fps_assumed": fps_assumed,
            "bit_depth": depth, "chroma": chroma, "rotation": rot,
            "stored_width": int(v["width"]), "stored_height": int(v["height"]),
            "sar": sar_f if sar_f else 1.0, "timecode": tc,
            "field_order": v.get("field_order"), "profile": v.get("profile"),
            "color_range": v.get("color_range"), "color_space": v.get("color_space"),
            "color_transfer": v.get("color_transfer"), "color_primaries": v.get("color_primaries"),
            "audio_streams": sum(1 for x in streams if x.get("codec_type") == "audio"),
            "container": info.get("format", {}).get("format_name")}


def probe(path, braw_helper=None):
    """Video stream facts. One probe serves all three stages. A .braw clip is
    probed through the BRAW helper (ffprobe cannot read it)."""
    if is_braw(path):
        return braw_info(path, braw_helper)
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH (install ffmpeg)") from None
    except subprocess.CalledProcessError as e:
        raise ValueError(f"ffprobe cannot read {path}: {(e.stderr or '').strip()[:300]}") from None
    return parse_probe(json.loads(r.stdout), os.path.getsize(path))


def describe_source(info):
    """One line saying what a video is, for the GUI to show on load."""
    bits = [f"{info['width']}x{info['height']}", info["codec"] + (f" {info['profile']}" if info.get("profile") else "")]
    if info.get("pix_fmt"):
        chroma = {"420": "4:2:0", "422": "4:2:2", "444": "4:4:4"}.get(info.get("chroma"), info.get("chroma") or "")
        bits.append(f"{info['pix_fmt']} ({info.get('bit_depth', 8)}-bit {chroma})".replace("  ", " ").replace(" )", ")"))
    bits.append(f"{info['fps']:.3f} fps" + (" VFR" if info.get("vfr") else ""))
    if info.get("duration") is not None:
        bits.append(f"{hms(info['duration'])} ({info['duration']:.1f}s)")
    if info.get("nb_frames_meta"):
        bits.append(f"{info['nb_frames_meta']} frames")
    bits.append(humansize(info["size_bytes"]))
    if info.get("timecode"):
        bits.append(f"TC {info['timecode']}")
    return "  ·  ".join(bits)


def source_warnings(info, pix_fmt="yuv420p"):
    """Things about a source that make a standard encode lossy or surprising."""
    out = []
    if info.get("vfr"):
        out.append(f"variable frame rate (r={info['r_frame_rate']}, avg={info['avg_frame_rate']}): "
                   "frame counts and the drop audit are approximate")
    if info.get("fps_assumed"):
        out.append("frame rate unreadable; 30 fps assumed")
    if info.get("rotation"):
        out.append(f"rotated {info['rotation']} deg: regions are in displayed pixels")
    if info.get("sar") and abs(info["sar"] - 1) > 0.001:
        out.append(f"non-square pixels (SAR {info['sar']:.3f}): crops use stored pixels")
    if info.get("field_order") not in (None, "progressive", "unknown"):
        out.append(f"interlaced source ({info['field_order']}); no deinterlacing is applied")
    if info.get("duration") is None:
        out.append("duration unknown")
    if (info.get("bit_depth") or 8) > 8 and pix_fmt and "10" not in pix_fmt:
        out.append(f"{info['bit_depth']}-bit source -> 8-bit output ({pix_fmt}): "
                   "banding possible; choose a 10-bit pix_fmt to keep it")
    return out


def _nb_frames_from_probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_frames,duration_ts,r_frame_rate",
                            "-of", "json", path],
                           capture_output=True, text=True, check=True, timeout=15)
        s = json.loads(r.stdout)["streams"][0]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError, KeyError, IndexError):
        return None
    for key in ("nb_frames", "duration_ts"):
        v = s.get(key)
        if v and v != "N/A":
            try:
                if int(v) > 0:
                    return int(v)
            except ValueError:
                pass
    return None


def _count_packets(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-count_packets", "-show_entries", "stream=nb_read_packets",
                            "-of", "csv=p=0", path],
                           capture_output=True, text=True, check=True)
        return int(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def audit_frame_drops(path, info, force=False, log=print):
    """Expected vs actual frames; cached in <path>.frame_audit.json, keyed on
    the source mtime (same file format as before, so old caches stay valid)."""
    audit_path = path + ".frame_audit.json"
    mtime = os.path.getmtime(path)
    if not force and os.path.exists(audit_path):
        try:
            with open(audit_path) as f:
                cached = json.load(f)
            if cached.get("mtime_at_audit") == mtime:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    expected = round(info["duration"] * info["fps"])
    t0 = time.time()
    actual, method = info.get("nb_frames_meta"), "metadata:nb_frames"
    if not actual or actual <= 0:
        actual, method = _nb_frames_from_probe(path), "metadata:probe"
    if actual is None:
        log("    no frame count in metadata, counting packets (slow)...")
        actual, method = _count_packets(path), "count_packets"
    if actual is None:
        log(f"    audit failed for {path}")
        return None
    drop = expected - actual
    result = {"path": path, "expected_frames": expected, "actual_frames": actual,
              "drop_count": drop,
              "drop_pct": round((100.0 * drop / expected) if expected else 0.0, 4),
              "duration": info["duration"], "fps_nominal": info["fps"],
              "method": method, "audit_time": datetime.now().isoformat(),
              "audit_duration_sec": round(time.time() - t0, 2), "mtime_at_audit": mtime}
    try:
        with open(audit_path, "w") as f:
            json.dump(result, f, indent=2)
    except OSError as e:
        log(f"    warning: could not write audit cache: {e}")
    return result


# ---------------------------------------------------------------------------
# Stage 1: encode / arena crop
# ---------------------------------------------------------------------------
def default_mode(width, height, for_dialog=True):
    """Starting crop mode for a source. The crop dialog and a direct encode of
    an unconfigured file used slightly different rules in v1; both kept."""
    if width >= 3000:
        return "two_arena"
    if for_dialog:
        if (width, height) in ((1920, 1080), (DLC_W, DLC_H)):
            return "single"
        return "none"
    return "single" if height == DLC_H else "none"


def crop_box(src_w, src_h, mode, click_x):
    """The single-mode crop in source pixels: (x, width, height)."""
    if src_w == DLC_W and src_h == DLC_H:
        return 0, DLC_W, DLC_H
    if src_h == DLC_H:
        return even(max(0, min(src_w - DLC_W, click_x - DLC_W // 2))), DLC_W, DLC_H
    cw = min(even(src_h * DLC_W // DLC_H), src_w)
    return even(max(0, min(src_w - cw, click_x - cw // 2))), cw, src_h


def build_filter_chain(src_w, src_h, mode, click_x, target_h,
                       arena_left=True, arena_right=True):
    """[(label, ffmpeg -vf filter)] for one source."""
    scale = f"scale={DLC_W}:{DLC_H}:flags=lanczos:param0=3"
    if mode == "two_arena":
        half = even(src_w // 2)
        sides = [("left_crop", 0)] * arena_left + [("right_crop", half)] * arena_right
        return [(lab, f"crop={half}:{src_h}:{x}:0,{scale},format=yuv420p")
                for lab, x in sides]
    if mode == "single":
        if src_w == DLC_W and src_h == DLC_H:
            return [("crop", "format=yuv420p")]
        x, cw, ch = crop_box(src_w, src_h, mode, click_x)
        if src_h == DLC_H:
            return [("crop", f"crop={DLC_W}:{DLC_H}:{x}:0,format=yuv420p")]
        return [("crop", f"crop={cw}:{src_h}:{x}:0,{scale},format=yuv420p")]
    if mode == "none":
        if src_h == target_h:
            return [("full", "format=yuv420p")]
        return [("full", f"scale=-2:{target_h}:flags=lanczos:param0=3,format=yuv420p")]
    raise ValueError(f"Unknown mode: {mode}")


# ---------------------------------------------------------------------------
# Layouts: any set of crop regions (single crop, arena split, grid) -> filters
# ---------------------------------------------------------------------------
# One resample per region, always Lanczos a=3 on the CPU: it keeps fine features
# [DD V.2]. A region already at the output size is crop-only (no resample).
SCALER_DEFAULT = {"flags": "lanczos", "param0": 3}
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")     # no "." or "/": names become file names


def centered_region(src_w, src_h, cx, cy, w, h, name="crop"):
    """w x h box centred on (cx, cy), clamped inside the frame, even-aligned."""
    w, h = even(w), even(h)
    x = even(max(0, min(src_w - w, int(cx) - w // 2)))
    y = even(max(0, min(src_h - h, int(cy) - h // 2)))
    return {"name": name, "x": x, "y": y, "w": w, "h": h}


def grid_regions(src_w, src_h, rows, cols, cell_w=None, cell_h=None, x0=0, y0=0,
                 pitch_x=None, pitch_y=None, offsets=None, skip=()):
    """rows x cols cells named r<row>c<col> (1-based, row-major). Cells default
    to an even split of the area right of/below (x0, y0); pitch defaults to the
    cell size. offsets {name: [dx, dy]} nudge single cells; skip drops cells."""
    if rows < 1 or cols < 1:
        raise ValueError("grid needs rows >= 1 and cols >= 1")
    cell_w = even(cell_w or (src_w - x0) // cols)
    cell_h = even(cell_h or (src_h - y0) // rows)
    pitch_x, pitch_y = pitch_x or cell_w, pitch_y or cell_h
    out = []
    for r in range(rows):
        for c in range(cols):
            name = f"r{r + 1}c{c + 1}"
            if name in skip:
                continue
            dx, dy = (offsets or {}).get(name, (0, 0))
            out.append({"name": name, "x": even(x0 + c * pitch_x + dx),
                        "y": even(y0 + r * pitch_y + dy), "w": cell_w, "h": cell_h})
    return out


def resolve_layout(layout, src_w, src_h):
    """layout dict -> (regions, out_w, out_h, scaler). Validates everything.

    layout: {"regions": [{name, x, y, w, h} | {name, cx, cy, w, h}]}
         or {"grid": {rows, cols, cell_w?, cell_h?, x0?, y0?, pitch_x?, pitch_y?,
                      offsets?, skip?}}
         plus optional "output": {"w", "h"} (default: each region's own size)
         and "scaler": {"flags", "param0"} (default Lanczos a=3).
    """
    if ("regions" in layout) == ("grid" in layout):
        raise ValueError("layout needs exactly one of 'regions' or 'grid'")
    if "grid" in layout:
        regions = grid_regions(src_w, src_h, **layout["grid"])
    else:
        regions = []
        for r in layout["regions"]:
            if "cx" in r:
                regions.append(centered_region(src_w, src_h, r["cx"], r["cy"],
                                               r["w"], r["h"], r.get("name", "crop")))
            else:
                regions.append({k: (r[k] if k == "name" else int(r[k]))
                                for k in ("name", "x", "y", "w", "h")})
    if not regions:
        raise ValueError("layout has no regions")
    names = [r["name"] for r in regions]
    if len(set(names)) != len(names):
        raise ValueError("region names must be unique")
    for r in regions:
        if not _NAME_RE.match(r["name"]):
            raise ValueError(f"region name {r['name']!r}: use letters, digits, '_' and '-' only")
        if r["w"] <= 0 or r["h"] <= 0 or r["w"] % 2 or r["h"] % 2 or r["x"] % 2 or r["y"] % 2:
            raise ValueError(f"region {r['name']}: x, y, w, h must be even and w, h > 0")
        if r["x"] < 0 or r["y"] < 0 or r["x"] + r["w"] > src_w or r["y"] + r["h"] > src_h:
            raise ValueError(f"region {r['name']} {r['w']}x{r['h']}+{r['x']}+{r['y']} "
                             f"lies outside the {src_w}x{src_h} frame")
    out = layout.get("output") or {}
    out_w, out_h = out.get("w"), out.get("h")
    if (out_w is None) != (out_h is None):
        raise ValueError("output needs both w and h, or neither")
    if out_w is not None and (int(out_w) <= 0 or int(out_h) <= 0 or int(out_w) % 2 or int(out_h) % 2):
        raise ValueError("output w and h must be even and positive")
    return regions, out_w, out_h, {**SCALER_DEFAULT, **(layout.get("scaler") or {})}


def layout_warnings(layout, src_w, src_h):
    """Aspect distortion and upscaling a layout would cause (warnings only)."""
    regions, out_w, out_h, _ = resolve_layout(layout, src_w, src_h)
    out = []
    if out_w is None:
        return out
    for r in regions:
        skew = (r["w"] / r["h"]) / (int(out_w) / int(out_h)) - 1
        if abs(skew) > 0.01:
            out.append(f"region {r['name']} {r['w']}x{r['h']} -> {out_w}x{out_h}: "
                       f"aspect changes, image distorted by {abs(skew) * 100:.0f}%")
        if int(out_w) > r["w"] or int(out_h) > r["h"]:
            out.append(f"region {r['name']} {r['w']}x{r['h']} is upscaled to {out_w}x{out_h}")
    return out


def layout_filter_chain(layout, src_w, src_h, pix_fmt="yuv420p"):
    """[(name, ffmpeg -vf filter)] for a layout: crop -> scale -> format."""
    regions, out_w, out_h, sc = resolve_layout(layout, src_w, src_h)
    chains = []
    for r in regions:
        parts = []
        if (r["x"], r["y"], r["w"], r["h"]) != (0, 0, src_w, src_h):
            parts.append(f"crop={r['w']}:{r['h']}:{r['x']}:{r['y']}")
        if out_w is not None and (int(out_w), int(out_h)) != (r["w"], r["h"]):
            parts.append(f"scale={int(out_w)}:{int(out_h)}:flags={sc['flags']}:param0={sc['param0']}")
        parts.append(f"format={pix_fmt}")
        chains.append((r["name"], ",".join(parts)))
    return chains


def layout_from_params(p):
    """Form values -> layout dict (what the GUI editor collects).

    {"kind": "box", cx, cy, w, h}          one box centred on (cx, cy)
    {"kind": "grid", rows, cols, cell_w?, cell_h?, x0?, y0?, pitch_x?, pitch_y?}
    plus optional out_w, out_h (both or neither). Zero/None/"" mean "unset"."""
    num = lambda k: int(float(p[k])) if p.get(k) not in (None, "", 0, "0") else None
    if p["kind"] == "box":
        layout = {"regions": [{"name": p.get("name") or "crop", "cx": int(float(p["cx"])),
                               "cy": int(float(p["cy"])), "w": int(float(p["w"])),
                               "h": int(float(p["h"]))}]}
    elif p["kind"] == "grid":
        g = {"rows": int(p["rows"]), "cols": int(p["cols"])}
        for k in ("cell_w", "cell_h", "pitch_x", "pitch_y"):
            if num(k):
                g[k] = num(k)
        for k in ("x0", "y0"):
            g[k] = int(float(p.get(k) or 0))
        layout = {"grid": g}
    else:
        raise ValueError(f"unknown layout kind {p['kind']!r}")
    if num("out_w") or num("out_h"):
        layout["output"] = {"w": num("out_w"), "h": num("out_h")}
    return layout


def preview_cmd(src, vf, t, out_png, width=480):
    """One frame at t seconds through a region's real filter chain, shrunk for
    display. Shows exactly what the encode will produce (same crop/scale)."""
    return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", src,
            "-frames:v", "1", "-vf", f"{vf},scale={int(width)}:-2:flags=lanczos", out_png]


def _encoder_args(use_nvenc, quality, nvenc_preset, cpu_preset, pix_fmt="yuv420p"):
    if use_nvenc:
        return ["-preset", nvenc_preset, "-tune", "hq", "-rc", "vbr", "-cq", str(quality),
                "-b:v", "0", "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1",
                "-aq-strength", "8", "-bf", "3", "-b_ref_mode", "middle", "-gpu", "0",
                "-pix_fmt", pix_fmt]
    return ["-preset", cpu_preset, "-crf", str(quality), "-pix_fmt", pix_fmt]


def _ffmpeg_head(use_nvenc, src_codec, overwrite):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats",
           "-y" if overwrite else "-n"]
    if use_nvenc and src_codec in NVDEC_DECODABLE:        # ProRes: CPU decode [V.2]
        cmd += ["-hwaccel", "cuda"]
    return cmd + ["-thread_queue_size", "1024"]


def build_encode_cmd(*, input_file, output_path, vf_filter, video_codec, use_nvenc,
                     src_codec, quality_value, nvenc_preset, cpu_preset, overwrite,
                     start_sec=None, duration=None, use_segmenter=False,
                     segment_time=None, segment_pattern=None, pix_fmt="yuv420p",
                     codec_args=None, faststart=True):
    cmd = _ffmpeg_head(use_nvenc, src_codec, overwrite)
    if start_sec is not None and duration is not None:
        cmd += ["-ss", str(start_sec), "-t", str(duration)]
    cmd += ["-i", input_file, "-map", "0:v:0", "-an", "-dn", "-sn",
            "-vf", vf_filter, "-c:v", video_codec]
    cmd += codec_args if codec_args is not None else \
        _encoder_args(use_nvenc, quality_value, nvenc_preset, cpu_preset, pix_fmt)
    if use_segmenter:
        cmd += ["-f", "segment", "-segment_time", str(segment_time), "-reset_timestamps", "1"]
        if faststart:
            cmd += ["-segment_format_options", "movflags=+faststart"]
        return cmd + [segment_pattern]
    return cmd + (["-movflags", "+faststart"] if faststart else []) + [output_path]


ENCODE_SETTINGS_DEFAULTS = {"encoder": "nvenc", "codec": "h264", "quality": 20,
                            "nvenc_preset": "p5", "cpu_preset": "medium",
                            "target_h": 1080, "overwrite": True,
                            "pix_fmt": None, "container": None, "prores_profile": 3}

PIX_FMTS = ("yuv420p", "yuv422p", "yuv444p", "yuv420p10le", "yuv422p10le", "yuv444p10le")
# Which encoder, containers and pixel formats each output codec allows. The NVENC
# 10-bit/4:4:4 rows are from NVIDIA's documented support, not tested here.
ENCODE_CODECS = {
    "h264": {"cpu": "libx264", "nvenc": "h264_nvenc", "containers": ("mp4", "mov", "mkv"),
             "pix_cpu": PIX_FMTS, "pix_nvenc": ("yuv420p", "yuv444p"), "pix_auto": "yuv420p"},
    "h265": {"cpu": "libx265", "nvenc": "hevc_nvenc", "containers": ("mp4", "mov", "mkv"),
             "pix_cpu": PIX_FMTS, "pix_nvenc": ("yuv420p", "yuv444p", "yuv420p10le", "yuv444p10le"),
             "pix_auto": "yuv420p"},
    "prores": {"cpu": "prores_ks", "nvenc": None, "containers": ("mov", "mkv"),
               "pix_cpu": ("yuv422p10le", "yuv444p10le"), "pix_auto": "yuv422p10le"},
    "ffv1": {"cpu": "ffv1", "nvenc": None, "containers": ("mkv", "mov"),
             "pix_cpu": PIX_FMTS, "pix_auto": "yuv420p"},
}


def resolve_output_options(s, prefer_pix=None):
    """Settings -> {video_codec, use_nvenc, pix_fmt, container, codec_args, notes}.
    Defaults (h264/h265, 8-bit 4:2:0, mp4) reproduce v1 exactly. Raises
    ValueError for a combination the codec cannot do. With no pix_fmt set,
    prefer_pix (the source's) is kept when this codec/encoder can write it."""
    spec = ENCODE_CODECS.get(s["codec"])
    if spec is None:
        raise ValueError(f"unknown codec {s['codec']!r}; choose from {sorted(ENCODE_CODECS)}")
    notes = []
    use_nvenc = s["encoder"] == "nvenc"
    if use_nvenc and spec["nvenc"] is None:
        use_nvenc = False
        notes.append(f"{s['codec']} has no NVENC encoder: using the CPU encoder")
    allowed = spec["pix_nvenc"] if use_nvenc else spec["pix_cpu"]
    pix = s.get("pix_fmt") or (prefer_pix if prefer_pix in allowed else spec["pix_auto"])
    if not s.get("pix_fmt") and prefer_pix and pix != prefer_pix:
        notes.append(f"source {prefer_pix} cannot be written by this encoder: using {pix}")
    if pix not in allowed:
        raise ValueError(f"{s['codec']} on {'NVENC' if use_nvenc else 'CPU'} cannot write "
                         f"{pix}; choose from {list(allowed)}")
    container = s.get("container") or spec["containers"][0]
    if container not in spec["containers"]:
        raise ValueError(f"{s['codec']} cannot go in .{container}; "
                         f"choose from {list(spec['containers'])}")
    codec_args = None
    if s["codec"] == "prores":
        codec_args = ["-profile:v", str(int(s["prores_profile"])), "-vendor", "apl0",
                      "-pix_fmt", pix]
    elif s["codec"] == "ffv1":
        codec_args = ["-level", "3", "-coder", "1", "-context", "1", "-slicecrc", "1",
                      "-pix_fmt", pix]
    return {"video_codec": spec["nvenc"] if use_nvenc else spec["cpu"], "use_nvenc": use_nvenc,
            "pix_fmt": pix, "container": container, "codec_args": codec_args, "notes": notes}


def plan_encode(entry, settings, info):
    """Resolve one encode entry into ffmpeg jobs. Pure: no I/O.

    entry: {source, mode | layout, click_x?, arenas?, time_mode?, chunk_minutes?, out_dir?}
    Returns (jobs, log_path); each job {label, vf, output, cmd}.
    """
    s = {**ENCODE_SETTINGS_DEFAULTS, **(settings or {})}
    src = entry["source"]
    oo = resolve_output_options(s)
    use_nvenc, video_codec, pix, ext = oo["use_nvenc"], oo["video_codec"], oo["pix_fmt"], oo["container"]
    suffix = "nvenc" if use_nvenc else "cpu"
    arenas = entry.get("arenas", ["left", "right"])
    click_x = entry.get("click_x")
    click_x = info["width"] // 2 if click_x is None else int(click_x)
    if "layout" in entry:
        outputs = layout_filter_chain(entry["layout"], info["width"], info["height"], pix)
    else:
        outputs = build_filter_chain(info["width"], info["height"], entry["mode"], click_x,
                                     int(s["target_h"]), "left" in arenas, "right" in arenas)
        outputs = [(lab, vf[:-len("format=yuv420p")] + f"format={pix}") for lab, vf in outputs]
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = entry.get("out_dir") or os.path.dirname(src)
    chunked = entry.get("time_mode", "full") == "chunked"
    chunk_s = int(float(entry.get("chunk_minutes", 30)) * 60) if chunked else None
    if chunked and chunk_s <= 0:
        raise ValueError("chunk_minutes must be positive")
    jobs = []
    for label, vf in outputs:
        output = os.path.join(out_dir, f"{base}_{label}_{suffix}.{ext}")
        pattern = os.path.join(out_dir, f"{base}_{label}_chunk%03d_{suffix}.{ext}")
        cmd = build_encode_cmd(input_file=src, output_path=output, vf_filter=vf,
                               video_codec=video_codec, use_nvenc=use_nvenc,
                               src_codec=info["codec"], quality_value=s["quality"],
                               nvenc_preset=s["nvenc_preset"], cpu_preset=s["cpu_preset"],
                               overwrite=s["overwrite"], use_segmenter=chunked,
                               segment_time=chunk_s, segment_pattern=pattern, pix_fmt=pix,
                               codec_args=oo["codec_args"], faststart=ext in ("mp4", "mov"))
        jobs.append({"label": label, "vf": vf,
                     "output": pattern if chunked else output, "cmd": cmd})
    return jobs, os.path.join(out_dir, f"{base}_{suffix}_encode.log")


def encode_record_path(entry):
    src = entry["source"]
    out_dir = entry.get("out_dir") or os.path.dirname(src)
    return os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + ".encode.json")


def encode_file(entry, settings, log=print, run=None, audit=True):
    """Encode one source. Writes <base>.encode.json (a valid job file that
    reproduces this encode) BEFORE any ffmpeg runs. Returns failure strings."""
    run = run or run_ffmpeg
    src = entry["source"]
    info = probe(src)
    s = {**ENCODE_SETTINGS_DEFAULTS, **(settings or {})}
    log(f"\n>>> {src}")
    log(f"    {info['width']}x{info['height']} codec={info['codec']} "
        f"dur={info['duration']:.1f}s mode={entry.get('mode', 'layout')}")
    aud = audit_frame_drops(src, info, log=log) if audit else None
    if aud:
        pct = aud["drop_pct"]
        tag = "[OK]" if pct < 0.1 else ("[WARN]" if pct < 1.0 else "[HIGH]")
        log(f"    {tag} frame audit: {aud['actual_frames']} / {aud['expected_frames']} "
            f"packets, drops={aud['drop_count']} ({pct:.2f}%)")
        if pct >= 1.0:
            log("    !! WARNING: drop rate >= 1%. Check USB cable, SSD health, "
                "thermal throttling.")
    oo = resolve_output_options(s)
    for n in oo["notes"]:
        log(f"    note: {n}")
    for n in source_warnings(info, oo["pix_fmt"]):
        log(f"    [WARN] {n}")
    if "layout" in entry:
        for n in layout_warnings(entry["layout"], info["width"], info["height"]):
            log(f"    [WARN] {n}")
    if oo["use_nvenc"] and info["codec"] not in NVDEC_DECODABLE:
        log(f"    note: '{info['codec']}' not NVDEC-decodable -> CPU decode + GPU encode")
    jobs, log_path = plan_encode(entry, s, info)
    if not jobs:
        log("    SKIP: no arenas selected for this file")
        return []
    record = {"version": 1, "tool": "stylet",
              "written": datetime.now().isoformat(timespec="seconds"),
              "encode": [entry], "encode_settings": s,
              "resolved": {"source_info": info, "audit": aud,
                           "outputs": [{k: j[k] for k in ("label", "vf", "output")} for j in jobs],
                           "commands": [quote(j["cmd"]) for j in jobs]}}
    os.makedirs(os.path.dirname(encode_record_path(entry)) or ".", exist_ok=True)
    with open(encode_record_path(entry), "w") as f:
        json.dump(record, f, indent=2)
    failed = []
    for j in jobs:
        log("Running: " + quote(j["cmd"]))
        rc = run(j["cmd"], log_path)
        if rc != 0:
            failed.append(f"{src} [{j['label']}] rc={rc}")
    return failed


# ---------------------------------------------------------------------------
# Stage 2: chunk merge and phase split
# ---------------------------------------------------------------------------
CHUNK_RE = re.compile(r"^(?P<prefix>.+)_chunk(?P<idx>\d+)_(?P<suffix>[^_.]+)\.(?P<ext>\w+)$")
# outputs this tool writes (skipped by the folder scan): merged, phase<N>[-name], fragment[<N>]
OUTPUT_RE = re.compile(r"_(merged|phase\d+(-[A-Za-z0-9-]+)?|fragment\d*)(_[^_.]+)?\.[a-zA-Z0-9]+$")
_KEY_RE = re.compile(r"^(?P<prefix>.+)_(?P<suffix>[^_.]+)\.(?P<ext>\w+)$")


def find_groups_and_others(folder):
    """(chunk groups {key: [(idx, path)]}, single videos, other file names)."""
    groups, singles, others = defaultdict(list), [], []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        m = CHUNK_RE.match(name)
        if m:
            groups[f"{m['prefix']}_{m['suffix']}.{m['ext']}"].append((int(m["idx"]), full))
        elif os.path.splitext(name)[1].lower() in VIDEO_EXTS and not OUTPUT_RE.search(name):
            singles.append(full)
        else:
            others.append(name)
    return {k: sorted(v) for k, v in groups.items()}, singles, others


def validate_group(items, probe_fn=None):
    probe_fn = probe_fn or probe
    if not items:
        return False, {"error": "empty group"}
    idx = [i for i, _ in items]
    if idx != list(range(len(items))):
        missing = sorted(set(range(len(items))) - set(idx))
        return False, {"error": f"missing chunks {missing} (have {idx})"}
    keys = ("codec", "width", "height", "pix_fmt", "r_frame_rate")
    first, total = None, 0.0
    for i, path in items:
        try:
            m = probe_fn(path)
        except subprocess.CalledProcessError as e:
            return False, {"error": f"probe failed on chunk{i:03d}: {e}"}
        g = {k: m[k] for k in keys}
        if first is None:
            first, meta0 = g, m
        elif g != first:
            return False, {"error": f"chunk{i:03d} stream params differ"}
        total += m["duration"] or 0.0
    return True, {"n_chunks": len(items), "codec": meta0["codec"], "width": meta0["width"],
                  "height": meta0["height"], "fps": meta0["r_frame_rate"],
                  "total_size_bytes": sum(os.path.getsize(p) for _, p in items),
                  "merged_duration_sec": total}


def merged_name(key):
    m = _KEY_RE.match(key)
    return f"{m['prefix']}_merged_{m['suffix']}.{m['ext']}" if m else key.replace(".", "_merged.", 1)


def group_phase_name(key, phase):
    """'A006_left_crop_nvenc.mp4', 'phase1' -> 'A006_left_crop_merged_phase1_nvenc.mp4'."""
    m = _KEY_RE.match(key)
    if m:
        return f"{m['prefix']}_merged_{phase}_{m['suffix']}.{m['ext']}"
    base, ext = os.path.splitext(key)
    return f"{base}_merged_{phase}{ext}"


def single_phase_path(path, phase):
    """'/x/A006_full_nvenc.mp4', 'phase1' -> '/x/A006_full_nvenc_phase1.mp4'."""
    base, ext = os.path.splitext(path)
    return f"{base}_{phase}{ext}"


def group_phases_json(folder, key):
    return os.path.join(folder, f"{os.path.splitext(key)[0]}.phases.json")


def single_phases_json(path):
    return f"{os.path.splitext(path)[0]}.phases.json"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path, data, log=print):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        log(f"  warning: could not save {os.path.basename(path)}: {e}")
        return False


_PHASE_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")      # becomes part of a file name


def validate_phases(cfg, duration):
    """The general "Phases" mode: cfg["phases"] = [{"name"?, "start", "end"}, ...].
    Phases must not overlap and must run in time order; the last phase's end may
    be blank = end of the video. Returns (True, {"phases": [{label, start, end}]})
    or (False, message)."""
    items = cfg.get("phases") or []
    if not items:
        return False, "Phases: add at least one phase"
    out, prev_end, seen = [], 0.0, set()
    for i, ph in enumerate(items, 1):
        tag = f"Phase {i}"
        name = (ph.get("name") or "").strip()
        if name and not _PHASE_NAME_RE.match(name):
            return False, f"{tag}: name {name!r} may only use letters, digits and '-'"
        label = f"phase{i}" + (f"-{name}" if name else "")
        if label in seen:
            return False, f"{tag}: duplicate name"
        seen.add(label)
        try:
            start = parse_hms(ph.get("start", ""))
            if not str(ph.get("end", "")).strip() and i == len(items):
                if not duration:
                    return False, f"{tag}: end is blank (= end of video) but the duration is unknown"
                end = float(duration)
            else:
                end = parse_hms(ph.get("end", ""))
        except ValueError as e:
            return False, f"{tag}: {e}"
        if end <= start:
            return False, f"{tag}: end must be after start"
        if i > 1 and start < prev_end:
            return False, (f"{tag} start ({hms(start)}) must be >= Phase {i - 1} end "
                           f"({hms(prev_end)}): phases may not overlap")
        if duration and end > duration + 1:
            return False, f"{tag} end ({hms(end)}) exceeds merged duration ({hms(duration)})"
        out.append({"label": label, "start": start, "end": end})
        prev_end = end
    return True, {"phases": out}


def even_phases(start_sec, end_sec, n, names=None):
    """n equal consecutive phases between two times, as "Phases" config items."""
    if n < 1 or end_sec <= start_sec:
        raise ValueError("need n >= 1 and end after start")
    step = (end_sec - start_sec) / n
    return [{"name": (names[i] if names and i < len(names) else ""),
             "start": hms(start_sec + i * step), "end": hms(start_sec + (i + 1) * step)}
            for i in range(n)]


def validate_phase_config(cfg, duration):
    """(True, None | {"p1": (s, e), "p2": (s, e) | None}) or (False, message).
    mode "Phases" -> (True, {"phases": [...]}), see validate_phases."""
    mode = cfg.get("mode", "No split")
    if mode == "No split":
        return True, None
    if mode == "Phases":
        return validate_phases(cfg, duration)
    try:
        p1s, p1e = parse_hms(cfg.get("phase1_start", "")), parse_hms(cfg.get("phase1_end", ""))
    except ValueError as e:
        return False, f"Phase 1: {e}"
    if p1e <= p1s:
        return False, "Phase 1: end must be after start"
    if duration and p1e > duration + 1:
        return False, f"Phase 1 end ({hms(p1e)}) exceeds merged duration ({hms(duration)})"
    if mode == "Phase 1 only":
        return True, {"p1": (p1s, p1e), "p2": None}
    try:
        p2s, p2e = parse_hms(cfg.get("phase2_start", "")), parse_hms(cfg.get("phase2_end", ""))
    except ValueError as e:
        return False, f"Phase 2: {e}"
    if p2e <= p2s:
        return False, "Phase 2: end must be after start"
    if p2s < p1e:
        return False, f"Phase 2 start ({hms(p2s)}) must be >= Phase 1 end ({hms(p1e)})"
    if duration and p2e > duration + 1:
        return False, f"Phase 2 end ({hms(p2e)}) exceeds merged duration ({hms(duration)})"
    return True, {"p1": (p1s, p1e), "p2": (p2s, p2e)}


def phase_segments(mode, secs):
    """[(label, start, end)] to cut, in order. The fragment between the phases
    is always included when there is a gap -- never optional [DD V.4]."""
    if mode == "No split" or secs is None:
        return []
    if mode == "Phases":
        out, ph = [], secs["phases"]
        for i, p in enumerate(ph, 1):
            out.append((p["label"], p["start"], p["end"]))
            if i < len(ph) and ph[i]["start"] > p["end"]:       # the gap is always kept
                out.append((f"fragment{i}", p["end"], ph[i]["start"]))
        return out
    out = [("phase1", *secs["p1"])]
    if mode == "Phase 1 + Phase 2":
        (_, p1e), (p2s, p2e) = secs["p1"], secs["p2"]
        if p2s > p1e:
            out.append(("fragment", p1e, p2s))
        out.append(("phase2", p2s, p2e))
    return out


def keyframe_times(path, start_sec, window_sec=120.0):
    lo = max(0.0, start_sec)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "packet=pts_time,flags", "-of", "csv=print_section=0",
                        "-read_intervals", f"{lo}%{lo + window_sec}", path],
                       capture_output=True, text=True)
    times = []
    for line in r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0].strip()))
            except ValueError:
                pass
    return sorted(times)


def first_keyframe_at_or_after(path, start_sec):
    for window in (120.0, 600.0):
        for t in keyframe_times(path, start_sec, window):
            if t >= start_sec - 1e-6:
                return t
    return None


def trim_plan(kf, start_sec, end_sec):
    """(seek, duration) for a stream-copy trim. The start snaps FORWARD to the
    keyframe kf >= start_sec, never backward: a copy can only start on a
    keyframe, and a backward snap pulled handling-period frames into phase 2
    [DD V.3]. The clip may start up to one GOP late; never early. kf None (no
    keyframe found, e.g. a sub-GOP fragment) falls back to the requested start.
    Returns None when the snapped start is at or after the end."""
    seek, dur = (start_sec, end_sec - start_sec) if kf is None else (kf + 0.05, end_sec - kf)
    return None if dur <= 0 else (seek, dur)


def trim_cmd(src, out, seek, dur):
    return ["ffmpeg", "-hide_banner", "-y", "-nostdin", "-ss", f"{seek:.6f}", "-i", src,
            "-t", f"{dur:.6f}", "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", out]


def concat_cmd(list_path, out):
    return ["ffmpeg", "-hide_banner", "-y", "-nostdin", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", "-movflags", "+faststart", out]


def run_quiet(cmd, log=print):
    log(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        log(f"  FAILED rc={r.returncode}")
        log(r.stderr.decode("utf-8", errors="replace")[-2000:])
    return r.returncode == 0


def trim_phase(src, out, start_sec, end_sec, log=print, run=None, kf_fn=None):
    kf = (kf_fn or first_keyframe_at_or_after)(src, start_sec)
    if kf is None:
        log(f"  note: no keyframe >= {start_sec:.3f}s within search window; "
            f"backward-snap fallback for {os.path.basename(out)}")
    plan = trim_plan(kf, start_sec, end_sec)
    if plan is None:
        anchor = kf if kf is not None else start_sec
        log(f"  TRIM FAILED: snapped start ({anchor:.3f}s) is at/after end "
            f"({end_sec:.3f}s) for {os.path.basename(out)}")
        return False
    return (run or run_quiet)(trim_cmd(src, out, *plan), log)


def split_phases(src, cfg, secs, name_fn, log=print, run=None, kf_fn=None):
    """Cut phase1 / fragment / phase2 from src. Returns the written paths, or
    None if a trim failed. The source itself is never touched."""
    written = []
    for label, a, b in phase_segments(cfg["mode"], secs):
        out = name_fn(label)
        log(f"    trimming {label} [{hms(a)} - {hms(b)}] -> {os.path.basename(out)}")
        if not trim_phase(src, out, a, b, log, run, kf_fn):
            return None
        written.append(out)
    return written


def merge_chunks(items, out, log=print, run=None):
    list_path = out + ".concat_list.txt"
    with open(list_path, "w") as f:
        for _, p in items:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    try:
        return (run or run_quiet)(concat_cmd(list_path, out), log)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def process_group(folder, key, items, cfg, delete_chunks=False, log=print, probe_fn=None):
    """Merge one chunk group, then optionally split it into phases. Sidecar
    first [V.5]; the full merge is removed only when phase clips replace it;
    chunks are deleted only on request and only if every output is >= 1 KB."""
    ok, info = validate_group(items, probe_fn)
    if not ok:
        return [f"{key} ({info['error']})"]
    pok, secs = validate_phase_config(cfg, info["merged_duration_sec"])
    if not pok:
        return [f"{key}: {secs}"]
    save_json(group_phases_json(folder, key), cfg, log)
    merged = os.path.join(folder, merged_name(key))
    log(f"\n>>> [chunked] {key}\n    merging -> {os.path.basename(merged)} "
        f"({info['n_chunks']} chunks, {humansize(info['total_size_bytes'])})")
    if not merge_chunks(items, merged, log):
        return [f"{key} (full merge)"]
    clips = split_phases(merged, cfg, secs, lambda lab: os.path.join(folder, group_phase_name(key, lab)), log)
    if clips is None:
        return [f"{key} (phase trim)"]
    if clips:
        try:
            os.remove(merged)
            log(f"    removed intermediate full merge: {os.path.basename(merged)}")
        except OSError as e:
            log(f"    could not remove full merge {os.path.basename(merged)}: {e}")
    finals = clips or [merged]
    if delete_chunks:
        if all(os.path.exists(p) and os.path.getsize(p) >= 1024 for p in finals):
            for _, p in items:
                try:
                    os.remove(p)
                    log(f"  removed {os.path.basename(p)}")
                except OSError as e:
                    log(f"  could not remove {p}: {e}")
        else:
            log(f"  skip delete for {key} (outputs suspect)")
    return []


def process_single(path, cfg, log=print):
    """Phase-split one video. The source is never deleted [V.9]."""
    if cfg.get("mode", "No split") == "No split":
        return []
    pok, secs = validate_phase_config(cfg, probe(path)["duration"])
    if not pok:
        return [f"{os.path.basename(path)}: {secs}"]
    save_json(single_phases_json(path), cfg, log)
    log(f"\n>>> [single] {os.path.basename(path)}")
    clips = split_phases(path, cfg, secs, lambda lab: single_phase_path(path, lab), log)
    return [f"{os.path.basename(path)} (single-file phase trim)"] if clips is None else []


# ---------------------------------------------------------------------------
# Stage 3: fps limiter
# ---------------------------------------------------------------------------
FPS_SETTINGS_DEFAULTS = {"encoder": "nvenc", "quality": 20, "nvenc_preset": "p5",
                         "cpu_preset": "medium", "overwrite": True,
                         "codec": "h264", "pix_fmt": None, "container": None,
                         "prores_profile": 3}


def build_fps_cmd(input_file, output_path, target_fps, src_codec, use_nvenc, quality,
                  nvenc_preset, cpu_preset, overwrite, pix_fmt="yuv420p",
                  video_codec=None, codec_args=None, faststart=True):
    cmd = _ffmpeg_head(use_nvenc, src_codec, overwrite)
    cmd += ["-i", input_file, "-map", "0:v:0", "-an", "-dn", "-sn",
            "-vf", f"fps={target_fps},format={pix_fmt}",
            "-c:v", video_codec or ("h264_nvenc" if use_nvenc else "libx264")]
    cmd += codec_args if codec_args is not None else \
        _encoder_args(use_nvenc, quality, nvenc_preset, cpu_preset, pix_fmt)
    return cmd + (["-movflags", "+faststart"] if faststart else []) + [output_path]


def fps_output_name(input_path, target_fps, ext="mp4"):
    base = re.sub(r"_\d+(?:\.\d+)?fps$", "", os.path.splitext(input_path)[0])
    fps_str = f"{int(target_fps)}" if target_fps == int(target_fps) else f"{target_fps:g}"
    return f"{base}_{fps_str}fps.{ext}"


def fps_record_path(out):
    return os.path.splitext(out)[0] + ".fps.json"


def plan_fps(path, target_fps, settings, info):
    """Resolve one fps step. Pure. Keeps the source's bit depth and chroma when
    the encoder can write them (the archive is not silently flattened to 8-bit
    4:2:0); an 8-bit 4:2:0 source gives exactly the v1 command and name."""
    s = {**FPS_SETTINGS_DEFAULTS, **(settings or {})}
    oo = resolve_output_options(s, prefer_pix=info.get("pix_fmt"))
    out = fps_output_name(path, target_fps, oo["container"])
    cmd = build_fps_cmd(path, out, target_fps, info["codec"], oo["use_nvenc"], s["quality"],
                        s["nvenc_preset"], s["cpu_preset"], s["overwrite"], oo["pix_fmt"],
                        oo["video_codec"], oo["codec_args"], oo["container"] in ("mp4", "mov"))
    return {"output": out, "cmd": cmd, "settings": s, "options": oo}


def reduce_fps(path, target_fps, settings, log=print, run=None, info=None, write_record=True):
    """Returns ("skipped", msg) | ("failed", msg) | ("ok", output). Writes
    <out>.fps.json (a valid job file) before ffmpeg starts [DD V.5, H]."""
    info = info or probe(path)
    if target_fps >= info["fps"]:
        return "skipped", f"{os.path.basename(path)} (source {info['fps']:.2f}fps)"
    pl = plan_fps(path, target_fps, settings, info)
    out, cmd = pl["output"], pl["cmd"]
    use_nvenc = pl["options"]["use_nvenc"]
    log(f"\n>>> {path}\n    {info['codec']} {info['fps']:.2f}fps -> {target_fps:.2f}fps")
    for n in pl["options"]["notes"] + source_warnings(info, pl["options"]["pix_fmt"]):
        log(f"    note: {n}")
    if use_nvenc and info["codec"] not in NVDEC_DECODABLE:
        log(f"    note: '{info['codec']}' not NVDEC-decodable -> CPU decode + GPU encode")
    record = {"version": 1, "tool": "stylet",
              "written": datetime.now().isoformat(timespec="seconds"),
              "fps": [{"source": path, "target_fps": target_fps}], "fps_settings": pl["settings"],
              "resolved": {"source_info": info, "output": out, "command": quote(cmd)}}
    if write_record:                                # a dry run writes nothing
        with open(fps_record_path(out), "w") as f:
            json.dump(record, f, indent=2)
    if run is None:
        log("Running: " + quote(cmd))
    rc = (run or run_ffmpeg)(cmd, os.path.splitext(out)[0] + "_fps.log")
    return ("ok", out) if rc == 0 else ("failed", f"{path} [rc={rc}]")


# ---------------------------------------------------------------------------
# ffmpeg runner with live progress (terminal + per-run log file)
# ---------------------------------------------------------------------------
def run_ffmpeg(cmd, log_path, producer=None):
    """cmd must include `-progress pipe:1 -nostats`. Returns ffmpeg's rc (or the
    producer's, when ffmpeg succeeded but the producer failed).
    Pipes are read as bytes and decoded with errors='replace' [DD V.7].
    producer: argv whose stdout feeds ffmpeg's stdin (the BRAW decoder)."""
    with open(log_path, "ab", buffering=0) as log:
        log.write(f"\n=== {datetime.now().isoformat()} ===\nCMD: {quote(cmd)}\n\n".encode())
        prod = None
        if producer:
            log.write(f"PRODUCER: {quote(producer)}\n\n".encode())
            prod = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=log)
        proc = subprocess.Popen(cmd, stdin=prod.stdout if prod else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if prod:
            prod.stdout.close()                 # ffmpeg owns the read end now
        progress = {}

        def pump_err():
            for ch in iter(lambda: proc.stderr.read(1), b""):
                sys.stderr.write(ch.decode("utf-8", errors="replace"))
                log.write(ch)

        def pump_out():
            last, buf = 0.0, b""
            for chunk in iter(lambda: proc.stdout.read(256), b""):
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    k, _, v = line.decode("utf-8", errors="replace").strip().partition("=")
                    if not v:
                        continue
                    progress[k] = v
                    if k == "progress" and (time.time() - last >= 2.0 or v == "end"):
                        msg = (f"[progress] frame={progress.get('frame', '?')} "
                               f"fps={progress.get('fps', '?')} time={progress.get('out_time', '?')} "
                               f"speed={progress.get('speed', '?')}")
                        sys.stdout.write("\r" + msg + "    ")
                        sys.stdout.flush()
                        log.write((msg + "\n").encode())
                        last = time.time()

        threads = [threading.Thread(target=f, daemon=True) for f in (pump_err, pump_out)]
        for t in threads:
            t.start()
        start = time.time()
        beat = start
        while proc.poll() is None:
            time.sleep(0.25)                    # a short job must not wait for the heartbeat
            if not progress and time.time() - beat >= 10:
                beat = time.time()
                sys.stdout.write(f"\r[heartbeat] working {int(time.time() - start)}s, "
                                 "no frames yet (decoder warm-up)    ")
                sys.stdout.flush()
        for t in threads:
            t.join(timeout=2)
        sys.stdout.write("\n")
        if prod:
            prc = prod.wait()
            if proc.returncode == 0 and prc != 0:
                return prc
        return proc.returncode


# ---------------------------------------------------------------------------
# Stage 0: BRAW ingest [DD V.10]
# ---------------------------------------------------------------------------
# ffmpeg cannot decode Blackmagic RAW. The free Blackmagic RAW SDK can, through
# the small helper braw/braw2pipe (build it with braw/build.sh). Ingest decodes
# a clip ONCE into a ProRes intermediate next to it (<base>.mov); every other
# stage then works on that file exactly as on a camera-ProRes recording, so
# crops, grids, probing, the audit and NVDEC rules need no BRAW special cases.
BRAW_EXT = ".braw"
INGEST_SETTINGS_DEFAULTS = {"helper": None, "scale": 1, "profile": 4, "overwrite": False}
BRAW_SCALES = (1, 2, 4, 8)                       # full, half, quarter, eighth resolution


def is_braw(path):
    return os.path.splitext(path)[1].lower() == BRAW_EXT


def find_braw_helper(explicit=None):
    """explicit path, else $STYLET_BRAW_HELPER, else braw2pipe on PATH, else
    braw/braw2pipe in this repo. Raises RuntimeError with build instructions."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "braw", "braw2pipe")
    for cand in (explicit, os.environ.get("STYLET_BRAW_HELPER"), shutil.which("braw2pipe"), here):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError(
        "BRAW needs the helper 'braw2pipe', which is built against the free Blackmagic RAW SDK: "
        "download the SDK from blackmagicdesign.com, run braw/build.sh <SDK>/Linux, export "
        "BRAW_SDK_LIBS=<SDK>/Linux/Libraries, then put braw2pipe on PATH or set STYLET_BRAW_HELPER.")


def braw_info(path, helper=None, scale=1):
    """Clip facts from `braw2pipe --info`, in the same shape as probe()."""
    helper = find_braw_helper(helper)
    r = subprocess.run([helper, "--info", "--scale", str(scale), path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"braw2pipe cannot read {path}: {(r.stderr or r.stdout).strip()[:300]}")
    j = json.loads(r.stdout)
    num, den = int(j["fps_num"]), int(j["fps_den"])
    fake = {"streams": [{"codec_type": "video", "codec_name": "braw", "width": j["width"],
                         "height": j["height"], "pix_fmt": "rgb48le",
                         "r_frame_rate": f"{num}/{den}", "avg_frame_rate": f"{num}/{den}",
                         "nb_frames": str(j["frames"]),
                         "bits_per_raw_sample": str(j.get("bit_depth", 12)),
                         "tags": {"timecode": j["timecode"]} if j.get("timecode") else {}}],
            "format": {"duration": str(j["frames"] * den / num)}}
    info = parse_probe(fake, os.path.getsize(path))
    info["braw_scale"] = scale
    return info


def ingest_path(src, out_dir=None):
    return os.path.join(out_dir or os.path.dirname(src),
                        os.path.splitext(os.path.basename(src))[0] + ".mov")


def ingest_record_path(src, out_dir=None):
    return os.path.splitext(ingest_path(src, out_dir))[0] + ".ingest.json"


def plan_ingest(entry, settings, info, helper):
    """Pure. Returns {output, part, producer, cmd}: the helper's frames
    (rgb48le) piped into ffmpeg, which writes ProRes to <output>.part first."""
    s = {**INGEST_SETTINGS_DEFAULTS, **(settings or {})}
    scale, profile = int(s["scale"]), int(s["profile"])
    if scale not in BRAW_SCALES:
        raise ValueError(f"braw scale must be one of {BRAW_SCALES}")
    if not 0 <= profile <= 5:
        raise ValueError("prores profile must be 0..5 (4 = 4444, 5 = 4444 XQ)")
    src = entry["source"]
    out = ingest_path(src, entry.get("out_dir"))
    pix = "yuv444p10le" if profile >= 4 else "yuv422p10le"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats",
           "-y" if s["overwrite"] else "-n",
           "-f", "rawvideo", "-pix_fmt", "rgb48le",
           "-video_size", f"{info['width']}x{info['height']}", "-framerate", info["r_frame_rate"],
           "-i", "-", "-an",
           "-vf", f"scale=out_color_matrix=bt709:out_range=tv,format={pix}",
           "-c:v", "prores_ks", "-profile:v", str(profile), "-vendor", "apl0", "-pix_fmt", pix,
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-f", "mov", out + ".part"]
    return {"output": out, "part": out + ".part", "settings": s,
            "producer": [helper, "--scale", str(scale), src], "cmd": cmd}


def ingest_braw(entry, settings, log=print, run=None, helper=None):
    """BRAW clip -> ProRes intermediate. Returns (output_path, failures).
    An existing intermediate with the clip's frame count is reused unless
    overwrite is set. Writes <base>.ingest.json (a valid job file) before the
    decode starts, and renames the .part file only on success [DD V.5]."""
    s = {**INGEST_SETTINGS_DEFAULTS, **(settings or {})}
    src = entry["source"]
    helper = find_braw_helper(helper or s["helper"])
    info = braw_info(src, helper, int(s["scale"]))
    pl = plan_ingest(entry, s, info, helper)
    out = pl["output"]
    log(f"\n>>> BRAW ingest {src}\n    {info['width']}x{info['height']} "
        f"{info['nb_frames_meta']} frames @ {info['r_frame_rate']} (scale 1/{s['scale']})")
    if os.path.exists(out) and not s["overwrite"]:
        try:
            have = probe(out)
            if have["nb_frames_meta"] == info["nb_frames_meta"] and \
                    (have["width"], have["height"]) == (info["width"], info["height"]):
                log(f"    reusing existing intermediate {out}")
                return out, []
        except (ValueError, RuntimeError, OSError):
            pass
        log("    existing file does not match this clip: re-decoding")
        pl = plan_ingest(entry, {**s, "overwrite": True}, info, helper)
    record = {"version": 1, "tool": "stylet",
              "written": datetime.now().isoformat(timespec="seconds"),
              "ingest": [{k: v for k, v in entry.items() if k in ("source", "out_dir")}],
              "ingest_settings": {**s, "helper": None},
              "resolved": {"source_info": info, "output": out, "producer": quote(pl["producer"]),
                           "command": quote(pl["cmd"])}}
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(ingest_record_path(src, entry.get("out_dir")), "w") as f:
        json.dump(record, f, indent=2)
    if os.path.exists(pl["part"]):
        os.remove(pl["part"])                       # left by an interrupted run
    log("Running: " + quote(pl["producer"]) + " | " + quote(pl["cmd"]))
    rc = (run or run_ffmpeg)(pl["cmd"], os.path.splitext(out)[0] + "_ingest.log", pl["producer"])
    if rc != 0:
        if os.path.exists(pl["part"]):
            os.remove(pl["part"])
        return out, [f"{src} [ingest rc={rc}]"]
    os.replace(pl["part"], out)
    return out, []


# ---------------------------------------------------------------------------
# Headless job files
# ---------------------------------------------------------------------------
def run_job(job, log=print, dry_run=False):
    """Run a job dict. Sections run in stage order and are all optional:

    {"ingest": [{"source": <.braw>, "out_dir"}],
     "ingest_settings": {"helper", "scale": 1|2|4|8, "profile": 0..5, "overwrite"},
     "encode": [{"source", "mode": "two_arena"|"single"|"none" | "layout": {...},
                 "arenas": ["left","right"], "click_x": <px>,
                 "time_mode": "full"|"chunked", "chunk_minutes": 30, "out_dir"}],
     "encode_settings": {"encoder": "nvenc"|"cpu", "codec": "h264"|"h265",
                         "quality", "nvenc_preset", "cpu_preset", "target_h", "overwrite"},
     "phases": [{"source": <video>} | {"folder", "group", "delete_chunks"},
                plus "mode", "phase1_start", ... or nothing to use the saved
                .phases.json],
     "fps": [{"source", "target_fps"}], "fps_settings": {...}}

    A <name>.encode.json written by an encode is itself a valid job file.
    dry_run prints every ffmpeg command instead of running it.
    """
    fake = (lambda cmd, *_: (log("DRY RUN: " + quote(cmd)), 0)[1]) if dry_run else None
    fake_q = (lambda cmd, lg=log: (lg("DRY RUN: " + quote(cmd)), True)[1]) if dry_run else None
    failed = []
    ingested = {}                                # .braw source -> its intermediate
    for entry in job.get("ingest", []):
        out = ingest_path(entry["source"], entry.get("out_dir"))
        if dry_run:
            helper = find_braw_helper((job.get("ingest_settings") or {}).get("helper"))
            pl = plan_ingest(entry, job.get("ingest_settings"),
                             braw_info(entry["source"], helper,
                                       int((job.get("ingest_settings") or {}).get("scale", 1))),
                             helper)
            log("DRY RUN: " + quote(pl["producer"]) + " | " + quote(pl["cmd"]))
        else:
            out, bad = ingest_braw(entry, job.get("ingest_settings"), log)
            if bad:
                failed += bad
                continue
        ingested[entry["source"]] = out
    for entry in job.get("encode", []):
        if is_braw(entry["source"]):
            if entry["source"] not in ingested:
                raise ValueError(f"{entry['source']} is BRAW: add it to the job's 'ingest' "
                                 "section (or ingest it first)")
            entry = {**entry, "source": ingested[entry["source"]]}
            if dry_run and not os.path.exists(entry["source"]):
                log(f"DRY RUN: would encode {entry['source']} after ingest")
                continue
        if "mode" not in entry and "layout" not in entry:
            raise ValueError(f"encode entry for {entry.get('source')} needs an explicit "
                             "'mode' or a 'layout'")
        if dry_run:
            jobs, _ = plan_encode(entry, job.get("encode_settings"), probe(entry["source"]))
            for j in jobs:
                fake(j["cmd"])
        else:
            failed += encode_file(entry, job.get("encode_settings"), log)
    for p in job.get("phases", []):
        cfg = {k: p[k] for k in ("mode", "phases", "phase1_start", "phase1_end",
                                 "phase2_start", "phase2_end") if k in p}
        if "group" in p:
            folder, key = p["folder"], p["group"]
            cfg = cfg or load_json(group_phases_json(folder, key))
            groups, _, _ = find_groups_and_others(folder)
            if key not in groups:
                failed.append(f"{key}: no such chunk group in {folder}")
                continue
            if dry_run:
                log(f"DRY RUN: merge {len(groups[key])} chunks of {key}, then {cfg.get('mode')}")
                continue
            failed += process_group(folder, key, groups[key], cfg,
                                    p.get("delete_chunks", False), log)
        else:
            src = p["source"]
            cfg = cfg or load_json(single_phases_json(src))
            if dry_run:
                pok, secs = validate_phase_config(cfg, probe(src)["duration"])
                if not pok:
                    failed.append(f"{src}: {secs}")
                    continue
                split_phases(src, cfg, secs, lambda lab, s=src: single_phase_path(s, lab),
                             log, run=fake_q)
                continue
            failed += process_single(src, cfg, log)
    for e in job.get("fps", []):
        status, msg = reduce_fps(e["source"], float(e["target_fps"]),
                                 job.get("fps_settings"), log, run=fake, write_record=not dry_run)
        if status == "failed":
            failed.append(msg)
        elif status == "skipped":
            log(f"  skipped (already at/below target): {msg}")
    return failed
