"""
Video Pipeline
==============
Three-tab GUI combining the video preprocessing pipeline that runs before
DLC analysis:

  Tab 1: Encode / split   - ProRes (or any source) -> DLC-geometry archive
                            (1024x1080, with two-arena split or single-arena
                            crop, full duration, full source fps; chunked or
                            single output)

  Tab 2: Concat chunks    - Lossless merge of chunked outputs from Tab 1
                            into single archive files (stream-copy, fast)

  Tab 3: fps Limiter      - Re-encode DLC archives at a reduced frame rate
                            for analysis-rate-specific DLC inputs

Each tab is independent. All three log to the shared console panel at the
bottom of the window. Long-running operations run in background threads
so the GUI never freezes.
"""

import os
import re
import sys
import json
import time
import shlex
import threading
import subprocess
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ======================================================================
# Module-level shared helpers
# ======================================================================
NVDEC_DECODABLE = {"h264", "hevc", "vp9", "av1",
                   "mpeg2video", "mpeg4", "vc1"}

# DLC target geometry. The model is trained on this exact pixel size.
DLC_W = 1024
DLC_H = 1080


def humansize(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def even(x):
    """Snap to nearest even integer (yuv420p needs even offsets/dimensions)."""
    return int(x) & ~1


def ffprobe_json(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, check=True)
    return json.loads(r.stdout)


def run_ffmpeg(cmd, log_path):
    """
    Shared ffmpeg runner with live progress streaming.
    cmd MUST include `-progress pipe:1 -nostats`.
    Returns ffmpeg's return code.
    """
    with open(log_path, "ab", buffering=0) as log:
        log.write(f"\n=== {datetime.now().isoformat()} ===\n".encode())
        log.write(("CMD: " + " ".join(shlex.quote(c) for c in cmd)
                   + "\n\n").encode())

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)

        def pump_stderr():
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                try:
                    sys.stderr.write(ch.decode("utf-8", errors="replace"))
                    sys.stderr.flush()
                except Exception:
                    pass
                log.write(ch)

        progress = {}

        def pump_stdout():
            last = 0.0
            buf = b""
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    progress[k] = v
                    if k == "progress":
                        now = time.time()
                        if now - last >= 2.0 or v == "end":
                            msg = (f"\r[progress] frame={progress.get('frame','?')} "
                                   f"fps={progress.get('fps','?')} "
                                   f"time={progress.get('out_time','?')} "
                                   f"speed={progress.get('speed','?')}    ")
                            sys.stdout.write(msg)
                            sys.stdout.flush()
                            log.write((msg.strip() + "\n").encode())
                            last = now

        t1 = threading.Thread(target=pump_stderr, daemon=True)
        t2 = threading.Thread(target=pump_stdout, daemon=True)
        t1.start(); t2.start()

        start = time.time()
        while proc.poll() is None:
            time.sleep(10)
            if not progress:
                el = int(time.time() - start)
                sys.stdout.write(f"\r[heartbeat] working {el}s, no frames yet "
                                 f"(decoder warm-up)    ")
                sys.stdout.flush()

        t1.join(timeout=2)
        t2.join(timeout=2)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return proc.returncode


def run_in_thread(target, log_func):
    """Run callable in a daemon thread; print exceptions to log."""
    def wrapper():
        try:
            target()
        except Exception:
            log_func("EXCEPTION:\n" + traceback.format_exc())
    threading.Thread(target=wrapper, daemon=True).start()


# ======================================================================
# Top-level window setup
# ======================================================================
root = tk.Tk()
root.title("Video Pipeline")
root.geometry("1180x980")

# ----------------------------------------------------------------------
# Theme / styling
# ----------------------------------------------------------------------
# Single palette used everywhere. Inspired by clean modern document UIs:
# warm-neutral backgrounds, dark grey text, one accent (a muted blue),
# secondary accents in green and amber.
PALETTE = {
    "bg":            "#f5f4ef",   # warm off-white background
    "panel":         "#ffffff",   # row/card background
    "panel_alt":     "#faf9f4",   # subtle alt panel
    "border":        "#dcd8cf",   # soft border line
    "border_strong": "#b8b3a8",
    "text":          "#262524",   # primary text
    "text_dim":      "#6e6a63",   # secondary text
    "text_faint":    "#9c978e",   # tertiary / hints
    "accent":        "#4a6da7",   # primary action blue
    "accent_dark":   "#34528b",
    "accent_text":   "#ffffff",
    "ok":            "#3f7a4f",   # green for success
    "warn":          "#a85b1d",   # amber for warnings
    "err":           "#a83232",   # red for errors
    "select_bg":     "#e7e3d8",
}

# Use a single coherent font stack. Tk picks the first available.
FONT_UI = ("Inter", 10)
FONT_UI_BOLD = ("Inter", 10, "bold")
FONT_HEADER = ("Inter", 12, "bold")
FONT_SECTION = ("Inter", 11, "bold")
FONT_SMALL = ("Inter", 9)
FONT_MONO = ("JetBrains Mono", 9)
FONT_MONO_SM = ("JetBrains Mono", 8)

# Global tk option_add: applies to every plain tk widget created afterwards.
# This is how you skin standard tk widgets globally.
_opts = {
    "*background": PALETTE["bg"],
    "*foreground": PALETTE["text"],
    "*Label.background": PALETTE["bg"],
    "*Label.foreground": PALETTE["text"],
    "*Label.font": FONT_UI,
    "*Frame.background": PALETTE["bg"],
    "*Text.background": PALETTE["panel"],
    "*Text.foreground": PALETTE["text"],
    "*Text.font": FONT_MONO,
    "*Text.relief": "flat",
    "*Text.borderWidth": 1,
    "*Text.highlightBackground": PALETTE["border"],
    "*Text.highlightThickness": 1,
    "*Entry.background": PALETTE["panel"],
    "*Entry.foreground": PALETTE["text"],
    "*Entry.font": FONT_MONO,
    "*Entry.relief": "flat",
    "*Entry.borderWidth": 1,
    "*Entry.highlightBackground": PALETTE["border"],
    "*Entry.highlightColor": PALETTE["accent"],
    "*Entry.highlightThickness": 1,
    "*Entry.insertBackground": PALETTE["text"],
    "*Checkbutton.background": PALETTE["bg"],
    "*Checkbutton.foreground": PALETTE["text"],
    "*Checkbutton.font": FONT_UI,
    "*Checkbutton.activeBackground": PALETTE["bg"],
    "*Checkbutton.selectColor": PALETTE["panel"],
    "*Radiobutton.background": PALETTE["bg"],
    "*Radiobutton.foreground": PALETTE["text"],
    "*Radiobutton.font": FONT_UI,
    "*Radiobutton.activeBackground": PALETTE["bg"],
    "*Radiobutton.selectColor": PALETTE["panel"],
    "*Menu.background": PALETTE["panel"],
    "*Menu.foreground": PALETTE["text"],
    "*Menu.activeBackground": PALETTE["accent"],
    "*Menu.activeForeground": PALETTE["accent_text"],
    "*Menu.font": FONT_UI,
    "*Menu.relief": "flat",
    "*Menu.borderWidth": 1,
    "*Menubutton.background": PALETTE["panel"],
    "*Menubutton.foreground": PALETTE["text"],
    "*Menubutton.font": FONT_UI,
    "*Menubutton.relief": "flat",
    "*Menubutton.borderWidth": 1,
    "*Menubutton.highlightBackground": PALETTE["border"],
    "*Menubutton.highlightThickness": 1,
    "*Menubutton.padX": 6,
    "*Menubutton.padY": 2,
    "*Button.background": PALETTE["panel_alt"],
    "*Button.foreground": PALETTE["text"],
    "*Button.font": FONT_UI,
    "*Button.relief": "flat",
    "*Button.borderWidth": 1,
    "*Button.highlightBackground": PALETTE["border"],
    "*Button.highlightThickness": 1,
    "*Button.padX": 10,
    "*Button.padY": 4,
    "*Button.activeBackground": PALETTE["select_bg"],
    "*Scale.background": PALETTE["bg"],
    "*Scale.foreground": PALETTE["text"],
    "*Scale.troughColor": PALETTE["panel"],
    "*Scale.font": FONT_SMALL,
    "*Scale.highlightThickness": 0,
    "*LabelFrame.background": PALETTE["bg"],
    "*LabelFrame.foreground": PALETTE["text_dim"],
    "*LabelFrame.font": FONT_UI_BOLD,
    "*LabelFrame.relief": "flat",
    "*LabelFrame.borderWidth": 1,
    "*LabelFrame.highlightBackground": PALETTE["border"],
    "*LabelFrame.highlightThickness": 1,
    "*Canvas.background": "#000000",
    "*Canvas.highlightThickness": 0,
    "*Scrollbar.background": PALETTE["bg"],
    "*Scrollbar.troughColor": PALETTE["bg"],
    "*Scrollbar.activeBackground": PALETTE["border_strong"],
    "*Scrollbar.relief": "flat",
    "*Scrollbar.borderWidth": 0,
    "*Scrollbar.elementBorderWidth": 0,
    "*Scrollbar.width": 12,
}
for k, v in _opts.items():
    root.option_add(k, v)

root.configure(background=PALETTE["bg"])

# ttk-specific styling for the notebook tabs
_style = ttk.Style(root)
try:
    _style.theme_use("clam")
except tk.TclError:
    pass
_style.configure(
    "TNotebook",
    background=PALETTE["bg"],
    borderwidth=0,
    tabmargins=[10, 6, 0, 0],
)
_style.configure(
    "TNotebook.Tab",
    background=PALETTE["panel_alt"],
    foreground=PALETTE["text_dim"],
    padding=[16, 7],
    font=FONT_UI,
    borderwidth=0,
)
_style.map(
    "TNotebook.Tab",
    background=[("selected", PALETTE["bg"])],
    foreground=[("selected", PALETTE["text"])],
    expand=[("selected", [0, 0, 0, 0])],
)


def make_primary_button(parent, text, command, **kwargs):
    """Accent-coloured action button for the main 'do the thing' actions."""
    return tk.Button(
        parent, text=text, command=command,
        background=PALETTE["accent"], foreground=PALETTE["accent_text"],
        activebackground=PALETTE["accent_dark"],
        activeforeground=PALETTE["accent_text"],
        font=FONT_UI_BOLD,
        relief="flat", borderwidth=0,
        highlightthickness=0,
        padx=14, pady=6,
        cursor="hand2",
        **kwargs,
    )


def make_secondary_button(parent, text, command, **kwargs):
    """Quiet button for everything that isn't the primary call-to-action."""
    return tk.Button(
        parent, text=text, command=command,
        background=PALETTE["panel"], foreground=PALETTE["text"],
        activebackground=PALETTE["select_bg"],
        activeforeground=PALETTE["text"],
        font=FONT_UI,
        relief="flat", borderwidth=0,
        highlightbackground=PALETTE["border"],
        highlightthickness=1,
        padx=12, pady=5,
        cursor="hand2",
        **kwargs,
    )


class BatchProgress:
    """A small batch-level progress widget: bar + status label.
    Thread-safe: all updates marshalled via root.after."""

    def __init__(self, parent):
        self.frame = tk.Frame(parent, background=PALETTE["bg"])
        self.frame.pack(fill="x", padx=14, pady=(4, 10))
        # ttk styling for the bar
        _bar_style = ttk.Style(parent)
        _bar_style.configure(
            "Batch.Horizontal.TProgressbar",
            background=PALETTE["accent"],
            troughcolor=PALETTE["panel"],
            bordercolor=PALETTE["border"],
            lightcolor=PALETTE["accent"],
            darkcolor=PALETTE["accent"],
            thickness=12,
        )
        self.bar = ttk.Progressbar(
            self.frame, mode="determinate", length=400, value=0,
            style="Batch.Horizontal.TProgressbar",
        )
        self.bar.pack(side="left", padx=(0, 12))
        self.label = tk.Label(
            self.frame, text="Idle",
            font=FONT_SMALL, fg=PALETTE["text_faint"],
            background=PALETTE["bg"],
            anchor="w",
        )
        self.label.pack(side="left", fill="x", expand=True)

    def _apply(self, fn):
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            try:
                root.after(0, fn)
            except tk.TclError:
                pass

    def start(self, total, label_prefix=""):
        """Begin a batch of `total` items."""
        def _do():
            self.bar.config(maximum=max(total, 1), value=0)
            txt = (f"Starting {total} item{'s' if total != 1 else ''}…"
                   if not label_prefix
                   else f"{label_prefix} · 0 of {total}")
            self.label.config(text=txt, fg=PALETTE["text_dim"])
        self._apply(_do)

    def step(self, completed, total, current_name=""):
        """Advance to `completed` of `total`, optionally with a label."""
        def _do():
            self.bar.config(maximum=max(total, 1), value=completed)
            txt = f"{completed} of {total}"
            if current_name:
                txt += f" · {current_name}"
            self.label.config(text=txt, fg=PALETTE["text_dim"])
        self._apply(_do)

    def done(self, total, failed_count=0):
        """Mark batch complete."""
        def _do():
            self.bar.config(maximum=max(total, 1), value=total)
            if failed_count:
                msg = (f"Done · {total - failed_count} of {total} OK, "
                       f"{failed_count} failed")
                color = PALETTE["warn"]
            else:
                msg = f"Done · {total} of {total}"
                color = PALETTE["ok"]
            self.label.config(text=msg, fg=color)
        self._apply(_do)

    def reset(self):
        def _do():
            self.bar.config(value=0)
            self.label.config(text="Idle", fg=PALETTE["text_faint"])
        self._apply(_do)


notebook = ttk.Notebook(root)
notebook.pack(fill="both", expand=True, padx=12, pady=(10, 6))

encode_tab = tk.Frame(notebook, background=PALETTE["bg"])
concat_tab = tk.Frame(notebook, background=PALETTE["bg"])
fps_tab = tk.Frame(notebook, background=PALETTE["bg"])
notebook.add(encode_tab, text="Encode / Split")
notebook.add(concat_tab, text="Phase split / Merge")
notebook.add(fps_tab, text="fps Limiter")

# Separator between tab content and log
_log_sep = tk.Frame(root, height=1, background=PALETTE["border"])
_log_sep.pack(fill="x", padx=12)

tk.Label(root, text="Log",
         font=FONT_UI_BOLD, foreground=PALETTE["text_dim"]
         ).pack(pady=(8, 4), anchor="w", padx=18)
console = scrolledtext.ScrolledText(
    root, height=12, state="disabled",
    font=FONT_MONO_SM,
    background=PALETTE["panel"], foreground=PALETTE["text"],
    relief="flat", borderwidth=0,
    highlightbackground=PALETTE["border"], highlightthickness=1,
    insertbackground=PALETTE["text"],
    padx=10, pady=8,
)
console.pack(padx=12, pady=(0, 12), fill="both", expand=False)


def log(msg):
    """Thread-safe log. Tkinter is not thread-safe, so when called from a
    background thread we must marshal the actual widget update onto the
    main thread via root.after. Without this, rapid log calls from worker
    threads can cause libtk segfaults / bus errors under load."""
    def _do_log():
        try:
            console.config(state="normal")
            console.insert(tk.END, msg + "\n")
            console.see(tk.END)
            console.config(state="disabled")
        except tk.TclError:
            # Widget destroyed or in a weird state; just drop the message.
            pass

    if threading.current_thread() is threading.main_thread():
        _do_log()
    else:
        try:
            root.after(0, _do_log)
        except tk.TclError:
            pass


def safe_messagebox(kind, title, message):
    """Thread-safe messagebox. Background threads must not directly call
    messagebox.* on most platforms; marshal to main thread via root.after."""
    func = {
        "info": messagebox.showinfo,
        "warning": messagebox.showwarning,
        "error": messagebox.showerror,
    }.get(kind, messagebox.showinfo)

    if threading.current_thread() is threading.main_thread():
        func(title, message)
    else:
        root.after(0, lambda: func(title, message))


# ======================================================================
# TAB 1: ENCODE / SPLIT
# (from encode_split.py / encode_v3)
# ======================================================================
encode_selected_files = []
encode_file_configs = {}


def encode_probe(path):
    info = ffprobe_json(path)
    duration = float(info["format"]["duration"])
    vstream = next(s for s in info["streams"]
                   if s.get("codec_type") == "video")
    width = int(vstream["width"])
    height = int(vstream["height"])
    codec = vstream.get("codec_name", "").lower()
    rfr = vstream.get("r_frame_rate", "30/1")
    num, den = rfr.split("/")
    fps = float(num) / float(den) if float(den) != 0 else 30.0
    nb_frames_meta = vstream.get("nb_frames")
    try:
        nb_frames_meta = (int(nb_frames_meta)
                          if nb_frames_meta and nb_frames_meta != "N/A"
                          else None)
    except (TypeError, ValueError):
        nb_frames_meta = None
    return {"duration": duration, "width": width, "height": height,
            "codec": codec, "fps": fps, "nb_frames_meta": nb_frames_meta}


def _read_nb_frames_meta(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=nb_frames,duration_ts,r_frame_rate",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        s = json.loads(r.stdout)["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return None
    nbf = s.get("nb_frames")
    if nbf and nbf != "N/A":
        try:
            n = int(nbf)
            if n > 0:
                return n
        except ValueError:
            pass
    dts = s.get("duration_ts")
    if dts and dts != "N/A":
        try:
            n = int(dts)
            if n > 0:
                return n
        except ValueError:
            pass
    return None


def _count_packets(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-count_packets", "-show_entries", "stream=nb_read_packets",
           "-of", "csv=p=0", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return int(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def audit_frame_drops(path, basic_info, force=False):
    audit_path = path + ".frame_audit.json"
    src_mtime = os.path.getmtime(path)

    if not force and os.path.exists(audit_path):
        try:
            with open(audit_path) as f:
                cached = json.load(f)
            if cached.get("mtime_at_audit") == src_mtime:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    expected = round(basic_info["duration"] * basic_info["fps"])
    t0 = time.time()
    method = None
    actual = None

    nbf_meta = basic_info.get("nb_frames_meta")
    if nbf_meta and nbf_meta > 0:
        actual = nbf_meta
        method = "metadata:nb_frames"
    else:
        actual = _read_nb_frames_meta(path)
        if actual is not None:
            method = "metadata:probe"

    if actual is None:
        log("    no frame count in metadata, counting packets (slow)...")
        actual = _count_packets(path)
        method = "count_packets"

    if actual is None:
        log(f"    audit failed for {path}")
        return None

    drop_count = expected - actual
    drop_pct = (100.0 * drop_count / expected) if expected else 0.0
    result = {
        "path": path,
        "expected_frames": expected,
        "actual_frames": actual,
        "drop_count": drop_count,
        "drop_pct": round(drop_pct, 4),
        "duration": basic_info["duration"],
        "fps_nominal": basic_info["fps"],
        "method": method,
        "audit_time": datetime.now().isoformat(),
        "audit_duration_sec": round(time.time() - t0, 2),
        "mtime_at_audit": src_mtime,
    }
    try:
        with open(audit_path, "w") as f:
            json.dump(result, f, indent=2)
    except OSError as e:
        log(f"    warning: could not write audit cache: {e}")
    return result


def encode_build_filter_chain(src_w, src_h, mode, click_x, target_h,
                              arena_left=True, arena_right=True):
    outputs = []
    if mode == "two_arena":
        half_w = even(src_w // 2)
        candidates = []
        if arena_left:
            candidates.append(("left_crop", 0))
        if arena_right:
            candidates.append(("right_crop", half_w))
        for label, x_off in candidates:
            f = (f"crop={half_w}:{src_h}:{x_off}:0,"
                 f"scale={DLC_W}:{DLC_H}:flags=lanczos:param0=3,"
                 f"format=yuv420p")
            outputs.append((label, f))
    elif mode == "single":
        if src_w == DLC_W and src_h == DLC_H:
            outputs.append(("crop", "format=yuv420p"))
        elif src_h == DLC_H:
            x = even(max(0, min(src_w - DLC_W, click_x - DLC_W // 2)))
            f = f"crop={DLC_W}:{DLC_H}:{x}:0,format=yuv420p"
            outputs.append(("crop", f))
        else:
            crop_w = even(src_h * DLC_W // DLC_H)
            crop_w = min(crop_w, src_w)
            x = even(max(0, min(src_w - crop_w, click_x - crop_w // 2)))
            f = (f"crop={crop_w}:{src_h}:{x}:0,"
                 f"scale={DLC_W}:{DLC_H}:flags=lanczos:param0=3,"
                 f"format=yuv420p")
            outputs.append(("crop", f))
    elif mode == "none":
        if src_h == target_h:
            outputs.append(("full", "format=yuv420p"))
        else:
            f = (f"scale=-2:{target_h}:flags=lanczos:param0=3,"
                 f"format=yuv420p")
            outputs.append(("full", f))
    else:
        raise ValueError(f"Unknown mode: {mode}")
    return outputs


def encode_build_cmd(*, input_file, output_path, vf_filter, video_codec,
                    use_nvenc, src_codec, quality_value,
                    nvenc_preset, cpu_preset, overwrite,
                    start_sec=None, duration=None,
                    use_segmenter=False, segment_time=None,
                    segment_pattern=None):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin",
           "-progress", "pipe:1", "-nostats"]
    cmd.append("-y" if overwrite else "-n")
    if use_nvenc and src_codec in NVDEC_DECODABLE:
        cmd.extend(["-hwaccel", "cuda"])
    cmd.extend(["-thread_queue_size", "1024"])
    if start_sec is not None and duration is not None:
        cmd.extend(["-ss", str(start_sec), "-t", str(duration)])
    cmd.extend(["-i", input_file,
                "-map", "0:v:0",
                "-an", "-dn", "-sn",
                "-vf", vf_filter,
                "-c:v", video_codec])
    if use_nvenc:
        cmd.extend([
            "-preset", nvenc_preset,
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(quality_value),
            "-b:v", "0",
            "-rc-lookahead", "32",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
            "-aq-strength", "8",
            "-bf", "3",
            "-b_ref_mode", "middle",
            "-gpu", "0",
            "-pix_fmt", "yuv420p",
        ])
    else:
        cmd.extend([
            "-preset", cpu_preset,
            "-crf", str(quality_value),
            "-pix_fmt", "yuv420p",
        ])
    if use_segmenter:
        cmd.extend([
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-reset_timestamps", "1",
            "-segment_format_options", "movflags=+faststart",
            segment_pattern,
        ])
    else:
        cmd.extend(["-movflags", "+faststart", output_path])
    return cmd


def encode_extract_thumbnail(input_file, out_png, t_seconds):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", str(t_seconds), "-i", input_file,
           "-frames:v", "1",
           "-vf", "scale=1280:-2:flags=lanczos",
           out_png]
    subprocess.run(cmd, check=True)


def encode_select_videos():
    files = filedialog.askopenfilenames(filetypes=[
        ("Video files", "*.mov *.mp4 *.avi *.mkv"),
        ("All files", "*.*")])
    if files:
        encode_selected_files.clear()
        encode_selected_files.extend(files)
        encode_file_configs.clear()
        encode_video_list.config(state="normal")
        encode_video_list.delete("1.0", tk.END)
        for f in encode_selected_files:
            encode_video_list.insert(tk.END, f + "\n")
        encode_video_list.config(state="disabled")


def encode_configure_crops_dialog():
    if not encode_selected_files:
        messagebox.showerror("Error", "Select files first.")
        return

    for f in encode_selected_files:
        if f not in encode_file_configs:
            try:
                info = encode_probe(f)
            except Exception as e:
                messagebox.showerror("Probe failed", f"{f}\n{e}")
                return
            if info["width"] >= 3000:
                default_mode = "two_arena"
            elif info["width"] == 1920 and info["height"] == 1080:
                default_mode = "single"
            elif info["width"] == DLC_W and info["height"] == DLC_H:
                default_mode = "single"
            else:
                default_mode = "none"
            encode_file_configs[f] = {
                "mode": default_mode,
                "click_x": info["width"] // 2,
                "audit": None,
                "audit_pending": True,
                "arena_left": True,
                "arena_right": True,
                **info,
            }

    win = tk.Toplevel(root)
    win.title("Configure crops")
    win.geometry("1320x920")
    win.transient(root)
    win.grab_set()

    state = {"idx": 0, "img": None, "scale_factor": 1.0,
             "src_w": None, "src_h": None,
             "suppress_trace": False}

    top = tk.Frame(win); top.pack(fill="x", pady=4)
    canvas = tk.Canvas(win, bg="black", width=1280, height=720)
    canvas.pack(pady=4)
    info_lbl = tk.Label(win, text="", font=("Arial", 10))
    info_lbl.pack()
    mode_frame = tk.Frame(win); mode_frame.pack(pady=4)
    nav = tk.Frame(win); nav.pack(pady=8)

    mode_var = tk.StringVar()
    tk.Label(mode_frame, text="Mode: ").pack(side="left")
    for label, val in (("Two-arena split", "two_arena"),
                       ("Single (click center)", "single"),
                       ("No crop", "none")):
        tk.Radiobutton(mode_frame, text=label, variable=mode_var,
                       value=val).pack(side="left", padx=4)

    arena_frame = tk.Frame(win); arena_frame.pack(pady=2)
    tk.Label(arena_frame, text="  Encode: ").pack(side="left")
    arena_left_var = tk.BooleanVar(value=True)
    arena_right_var = tk.BooleanVar(value=True)
    arena_left_cb = tk.Checkbutton(arena_frame, text="Left arena",
                                   variable=arena_left_var)
    arena_left_cb.pack(side="left", padx=4)
    arena_right_cb = tk.Checkbutton(arena_frame, text="Right arena",
                                    variable=arena_right_var)
    arena_right_cb.pack(side="left", padx=4)

    def update_arena_widget_state():
        en = "normal" if mode_var.get() == "two_arena" else "disabled"
        arena_left_cb.config(state=en)
        arena_right_cb.config(state=en)

    def update_info_label():
        if state.get("idx") is None or state["idx"] >= len(encode_selected_files):
            return
        path = encode_selected_files[state["idx"]]
        cfg = encode_file_configs[path]
        audit = cfg.get("audit")
        if cfg.get("audit_pending") and audit is None:
            drop_str = "drops=auditing..."
            drop_color = "grey"
        elif audit is None:
            drop_str = "drops=?"
            drop_color = "grey"
        else:
            pct = audit["drop_pct"]
            drop_str = (f"drops={audit['drop_count']}/{audit['expected_frames']} "
                        f"({pct:.2f}%) [{audit['method']}]")
            if pct >= 1.0:
                drop_color = "red"
            elif pct >= 0.1:
                drop_color = "orange"
            elif pct < 0:
                drop_color = "purple"
            else:
                drop_color = "darkgreen"
        info_lbl.config(
            text=(f"[{state['idx']+1}/{len(encode_selected_files)}]  "
                  f"{os.path.basename(path)}    "
                  f"{cfg['width']}x{cfg['height']} {cfg['codec']} "
                  f"{cfg['duration']:.1f}s {cfg['fps']:.2f}fps    "
                  f"{drop_str}"),
            fg=drop_color,
        )
        title_lbl.config(text=path)

    def redraw_overlay():
        canvas.delete("overlay")
        path = encode_selected_files[state["idx"]]
        cfg = encode_file_configs[path]
        sf = state["scale_factor"]
        sw, sh = cfg["width"], cfg["height"]
        if mode_var.get() == "two_arena":
            half = sw // 2
            color_l = "lime" if cfg.get("arena_left", True) else "grey50"
            label_l = "LEFT" if cfg.get("arena_left", True) else "LEFT (skip)"
            canvas.create_rectangle(0, 0, half * sf, sh * sf,
                                    outline=color_l, width=2,
                                    tags="overlay")
            canvas.create_text(half * sf / 2, 20, text=label_l,
                               fill=color_l, font=("Arial", 12, "bold"),
                               tags="overlay")
            color_r = "lime" if cfg.get("arena_right", True) else "grey50"
            label_r = "RIGHT" if cfg.get("arena_right", True) else "RIGHT (skip)"
            canvas.create_rectangle(half * sf, 0, sw * sf, sh * sf,
                                    outline=color_r, width=2,
                                    tags="overlay")
            canvas.create_text(half * sf + half * sf / 2, 20, text=label_r,
                               fill=color_r, font=("Arial", 12, "bold"),
                               tags="overlay")
        elif mode_var.get() == "single":
            if sw == DLC_W and sh == DLC_H:
                cw, ch, x = DLC_W, DLC_H, 0
            elif sh == DLC_H:
                cw, ch = DLC_W, DLC_H
                x = max(0, min(sw - cw, cfg["click_x"] - cw // 2))
            else:
                cw = min(sw, sh * DLC_W // DLC_H)
                ch = sh
                x = max(0, min(sw - cw, cfg["click_x"] - cw // 2))
            canvas.create_rectangle(x * sf, 0, (x + cw) * sf, ch * sf,
                                    outline="cyan", width=2, tags="overlay")
            cx = cfg["click_x"] * sf
            canvas.create_line(cx, 0, cx, ch * sf,
                               fill="yellow", dash=(4, 4), tags="overlay")
        elif mode_var.get() == "none":
            canvas.create_rectangle(0, 0, sw * sf, sh * sf,
                                    outline="white", width=2, tags="overlay")

    def redraw_overlay_and_info():
        redraw_overlay()
        update_info_label()

    def show_current():
        path = encode_selected_files[state["idx"]]
        cfg = encode_file_configs[path]
        state["src_w"], state["src_h"] = cfg["width"], cfg["height"]

        # Sync UI from config without triggering write-back traces. Otherwise
        # the trace callbacks read the *previous* file's var values for fields
        # we haven't synced yet, transiently corrupting the new file's config.
        # We also force the checkboxes to redraw (some Tk versions don't
        # refresh the visual state when var.set() is given an unchanged value).
        state["suppress_trace"] = True
        try:
            mode_var.set(cfg["mode"])
            arena_left_var.set(cfg.get("arena_left", True))
            arena_right_var.set(cfg.get("arena_right", True))
        finally:
            state["suppress_trace"] = False

        update_arena_widget_state()

        # Force checkbutton visual refresh. Setting variable= bound vars
        # should auto-refresh the widget, but we've seen cases where the
        # checkbox displays a stale state after navigation. Toggling the
        # var off and on guarantees Tk redraws the indicator. We do this
        # under suppress_trace so it doesn't fire write-back.
        state["suppress_trace"] = True
        try:
            for v in (arena_left_var, arena_right_var):
                cur = v.get()
                v.set(not cur)
                v.set(cur)
        finally:
            state["suppress_trace"] = False
        win.update_idletasks()

        tmp = "/tmp/video_pipeline_thumb.png"
        try:
            encode_extract_thumbnail(path, tmp, cfg["duration"] / 2)
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Thumbnail failed", str(e))
            return

        img = tk.PhotoImage(file=tmp)
        state["img"] = img
        state["scale_factor"] = img.width() / cfg["width"]

        canvas.config(width=img.width(), height=img.height())
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=img)
        redraw_overlay()
        update_info_label()

    def on_click(event):
        if mode_var.get() != "single":
            return
        path = encode_selected_files[state["idx"]]
        cfg = encode_file_configs[path]
        cfg["click_x"] = int(event.x / state["scale_factor"])
        redraw_overlay()

    def on_mode_change(*_):
        if state.get("suppress_trace"):
            return
        path = encode_selected_files[state["idx"]]
        encode_file_configs[path]["mode"] = mode_var.get()
        update_arena_widget_state()
        redraw_overlay()

    def on_arena_change(*_):
        if state.get("suppress_trace"):
            return
        path = encode_selected_files[state["idx"]]
        encode_file_configs[path]["arena_left"] = arena_left_var.get()
        encode_file_configs[path]["arena_right"] = arena_right_var.get()
        redraw_overlay()

    mode_var.trace_add("write", on_mode_change)
    arena_left_var.trace_add("write", on_arena_change)
    arena_right_var.trace_add("write", on_arena_change)
    canvas.bind("<Button-1>", on_click)

    title_lbl = tk.Label(top, text="", font=("Arial", 9), fg="grey")
    title_lbl.pack(side="top")

    def go_prev():
        if state["idx"] > 0:
            state["idx"] -= 1
            show_current()

    def go_next():
        if state["idx"] < len(encode_selected_files) - 1:
            state["idx"] += 1
            show_current()

    def apply_to_all():
        m = mode_var.get()
        cur_path = encode_selected_files[state["idx"]]
        cx = encode_file_configs[cur_path]["click_x"]
        al = encode_file_configs[cur_path].get("arena_left", True)
        ar = encode_file_configs[cur_path].get("arena_right", True)
        for f in encode_selected_files:
            encode_file_configs[f]["mode"] = m
            if m == "two_arena":
                encode_file_configs[f]["arena_left"] = al
                encode_file_configs[f]["arena_right"] = ar
            if m == "single":
                target_w = encode_file_configs[f]["width"]
                cur_w = encode_file_configs[cur_path]["width"]
                if target_w == cur_w:
                    encode_file_configs[f]["click_x"] = cx
        messagebox.showinfo("Applied", "Mode applied to all files.")

    tk.Button(nav, text="<< Prev", command=go_prev).pack(side="left", padx=4)
    tk.Button(nav, text="Next >>", command=go_next).pack(side="left", padx=4)
    tk.Button(nav, text="Apply mode to all",
              command=apply_to_all).pack(side="left", padx=12)
    tk.Button(nav, text="Done", command=win.destroy,
              bg="green", fg="white").pack(side="left", padx=12)

    show_current()

    # Background audit worker
    def _audit_worker():
        for f in encode_selected_files:
            cfg = encode_file_configs.get(f)
            if cfg is None or cfg.get("audit") is not None:
                continue
            try:
                cfg["audit"] = audit_frame_drops(f, cfg)
            except Exception as e:
                log(f"    audit error on {f}: {e}")
                cfg["audit"] = None
            cfg["audit_pending"] = False
            try:
                if (state.get("idx") is not None and
                        state["idx"] < len(encode_selected_files) and
                        encode_selected_files[state["idx"]] == f):
                    win.after(0, redraw_overlay_and_info)
            except Exception:
                pass

    threading.Thread(target=_audit_worker, daemon=True).start()


def encode_process_videos():
    if not encode_selected_files:
        messagebox.showerror("Error", "Select files first.")
        return

    for f in encode_selected_files:
        if f not in encode_file_configs:
            try:
                info = encode_probe(f)
            except Exception as e:
                messagebox.showerror("Probe failed", f"{f}\n{e}")
                return
            mode = ("two_arena" if info["width"] >= 3000 else
                    ("single" if info["height"] == DLC_H else "none"))
            encode_file_configs[f] = {
                "mode": mode,
                "click_x": info["width"] // 2,
                "audit": None,
                "audit_pending": False,
                "arena_left": True,
                "arena_right": True,
                **info,
            }
        cfg = encode_file_configs[f]
        if cfg.get("audit") is None and not cfg.get("audit_pending"):
            try:
                cfg["audit"] = audit_frame_drops(f, cfg)
            except Exception as e:
                log(f"    audit error on {f}: {e}")

    selected_codec = encode_codec_var.get()
    selected_encoder = encode_encoder_var.get()
    quality_value = encode_quality_slider.get()
    overwrite = encode_overwrite_var.get()
    target_h = int(encode_target_h_var.get())
    mode = encode_time_mode_var.get()

    use_nvenc = selected_encoder == "NVENC (GPU)"
    if selected_codec == "H.264":
        video_codec = "h264_nvenc" if use_nvenc else "libx264"
    elif selected_codec == "H.265":
        video_codec = "hevc_nvenc" if use_nvenc else "libx265"
    else:
        messagebox.showerror("Error", "Invalid codec.")
        return

    chunk_seconds = None
    if mode == "Full video, chunked":
        try:
            chunk_seconds = int(float(encode_chunk_entry.get().strip()) * 60)
            if chunk_seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error",
                                 "Chunk length must be positive minutes.")
            return

    files = list(encode_selected_files)
    nvenc_preset = encode_nvenc_preset_var.get()
    cpu_preset = encode_cpu_preset_var.get()
    enc_suffix = "nvenc" if use_nvenc else "cpu"

    def task():
        failed = []
        total = len(files)
        encode_progress.start(total, "Encoding")
        for i, input_file in enumerate(files):
            cfg = encode_file_configs[input_file]
            base = os.path.splitext(os.path.basename(input_file))[0]
            out_dir = os.path.dirname(input_file)
            log_path = os.path.join(out_dir,
                                    f"{base}_{enc_suffix}_encode.log")

            encode_progress.step(i, total, os.path.basename(input_file))
            log(f"\n>>> {input_file}")
            log(f"    {cfg['width']}x{cfg['height']} codec={cfg['codec']} "
                f"dur={cfg['duration']:.1f}s mode={cfg['mode']}")
            audit = cfg.get("audit")
            if audit:
                pct = audit["drop_pct"]
                tag = ("[OK]" if pct < 0.1
                       else ("[WARN]" if pct < 1.0 else "[HIGH]"))
                log(f"    {tag} frame audit: {audit['actual_frames']} / "
                    f"{audit['expected_frames']} packets, "
                    f"drops={audit['drop_count']} ({pct:.2f}%)")
                if pct >= 1.0:
                    log(f"    !! WARNING: drop rate >= 1%. Check USB cable, "
                        f"SSD health, thermal throttling.")
            if use_nvenc and cfg["codec"] not in NVDEC_DECODABLE:
                log(f"    note: '{cfg['codec']}' not NVDEC-decodable -> "
                    f"CPU decode + GPU encode")

            try:
                outputs = encode_build_filter_chain(
                    cfg["width"], cfg["height"], cfg["mode"],
                    cfg["click_x"], target_h,
                    arena_left=cfg.get("arena_left", True),
                    arena_right=cfg.get("arena_right", True),
                )
            except Exception as e:
                failed.append(f"{input_file} [filter build: {e}]")
                continue

            if not outputs:
                log("    SKIP: no arenas selected for this file")
                continue

            jobs = []
            for label, vf in outputs:
                if mode == "Full video":
                    jobs.append({"label": label, "vf": vf,
                                 "start": None, "duration": None,
                                 "segmenter": False})
                elif mode == "Full video, chunked":
                    pattern = os.path.join(
                        out_dir,
                        f"{base}_{label}_chunk%03d_{enc_suffix}.mp4")
                    jobs.append({"label": label, "vf": vf,
                                 "start": None, "duration": None,
                                 "segmenter": True,
                                 "segment_time": chunk_seconds,
                                 "segment_pattern": pattern})

            for job in jobs:
                output_path = os.path.join(
                    out_dir,
                    f"{base}_{job['label']}_{enc_suffix}.mp4")
                cmd = encode_build_cmd(
                    input_file=input_file,
                    output_path=output_path,
                    vf_filter=job["vf"],
                    video_codec=video_codec,
                    use_nvenc=use_nvenc,
                    src_codec=cfg["codec"],
                    quality_value=quality_value,
                    nvenc_preset=nvenc_preset,
                    cpu_preset=cpu_preset,
                    overwrite=overwrite,
                    start_sec=job["start"],
                    duration=job["duration"],
                    use_segmenter=job["segmenter"],
                    segment_time=job.get("segment_time"),
                    segment_pattern=job.get("segment_pattern"),
                )
                log("Running: " + " ".join(shlex.quote(c) for c in cmd))
                try:
                    rc = run_ffmpeg(cmd, log_path)
                except FileNotFoundError:
                    safe_messagebox("error", "Error",
                        "ffmpeg/ffprobe not found on PATH.")
                    return
                if rc != 0:
                    failed.append(f"{input_file} [{job['label']}] rc={rc}")

        # Count unique input files that had at least one failure
        failed_files = set()
        for f in failed:
            for src in files:
                if src in f:
                    failed_files.add(src)
                    break
        encode_progress.done(total, failed_count=len(failed_files))

        if failed:
            safe_messagebox("error", "Some failed", "\n".join(failed))
        else:
            safe_messagebox("info", "Done", "All jobs completed.")

    run_in_thread(task, log)


# ---- Encode tab UI ----
tk.Label(encode_tab, text="Selected videos",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(pady=(14, 6), anchor="w", padx=14)
encode_video_list = tk.Text(
    encode_tab, width=110, height=7, state="disabled",
    font=FONT_MONO_SM,
    background=PALETTE["panel"], foreground=PALETTE["text"],
    relief="flat", borderwidth=0,
    highlightbackground=PALETTE["border"], highlightthickness=1,
    padx=10, pady=8,
)
encode_video_list.pack(padx=14, fill="x")
_enc_top_btns = tk.Frame(encode_tab, background=PALETTE["bg"])
_enc_top_btns.pack(pady=(8, 12), padx=14, anchor="w")
make_secondary_button(_enc_top_btns, "Browse videos...",
                      encode_select_videos).pack(side="left", padx=(0, 8))
make_secondary_button(_enc_top_btns, "Configure crops...",
                      encode_configure_crops_dialog
                      ).pack(side="left")

settings = tk.LabelFrame(encode_tab, text="  Encoder settings  ",
                         padx=12, pady=10,
                         background=PALETTE["bg"])
settings.pack(fill="x", padx=14, pady=(0, 10))

tk.Label(settings, text="Encoder:",
         foreground=PALETTE["text_dim"]
         ).grid(row=0, column=0, sticky="e", padx=4, pady=3)
encode_encoder_var = tk.StringVar(value="NVENC (GPU)")
tk.OptionMenu(settings, encode_encoder_var,
              "CPU", "NVENC (GPU)").grid(row=0, column=1, sticky="w", padx=4)

tk.Label(settings, text="Codec:",
         foreground=PALETTE["text_dim"]
         ).grid(row=0, column=2, sticky="e", padx=4, pady=3)
encode_codec_var = tk.StringVar(value="H.264")
tk.OptionMenu(settings, encode_codec_var,
              "H.264", "H.265").grid(row=0, column=3, sticky="w", padx=4)

tk.Label(settings, text="Quality (CQ/CRF, lower=better):",
         foreground=PALETTE["text_dim"]
         ).grid(row=1, column=0, sticky="e", padx=4, pady=3)
encode_quality_slider = tk.Scale(settings, from_=18, to=28,
                                 orient="horizontal", length=180,
                                 troughcolor=PALETTE["panel"],
                                 activebackground=PALETTE["accent"],
                                 highlightthickness=0,
                                 background=PALETTE["bg"])
encode_quality_slider.set(20)
encode_quality_slider.grid(row=1, column=1, sticky="w", padx=4)

tk.Label(settings, text="NVENC preset:",
         foreground=PALETTE["text_dim"]
         ).grid(row=1, column=2, sticky="e", padx=4, pady=3)
encode_nvenc_preset_var = tk.StringVar(value="p5")
tk.OptionMenu(settings, encode_nvenc_preset_var,
              "p1", "p2", "p3", "p4", "p5", "p6", "p7"
              ).grid(row=1, column=3, sticky="w", padx=4)

tk.Label(settings, text="CPU preset:",
         foreground=PALETTE["text_dim"]
         ).grid(row=2, column=0, sticky="e", padx=4, pady=3)
encode_cpu_preset_var = tk.StringVar(value="medium")
tk.OptionMenu(settings, encode_cpu_preset_var,
              "ultrafast", "superfast", "veryfast", "faster",
              "fast", "medium", "slow", "slower", "veryslow"
              ).grid(row=2, column=1, sticky="w", padx=4)

tk.Label(settings, text="Output height ('No crop' mode only):",
         foreground=PALETTE["text_dim"]
         ).grid(row=2, column=2, sticky="e", padx=4, pady=3)
encode_target_h_var = tk.StringVar(value="1080")
tk.OptionMenu(settings, encode_target_h_var,
              "2160", "1440", "1080", "720", "480"
              ).grid(row=2, column=3, sticky="w", padx=4)

encode_overwrite_var = tk.BooleanVar(value=True)
tk.Checkbutton(settings, text="Overwrite existing output files",
               variable=encode_overwrite_var
               ).grid(row=3, column=0, columnspan=2, sticky="w",
                      padx=4, pady=(6, 0))

tk.Label(encode_tab, text="Time mode",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(pady=(10, 4), anchor="w", padx=14)
_time_row = tk.Frame(encode_tab, background=PALETTE["bg"])
_time_row.pack(anchor="w", padx=14)
encode_time_mode_var = tk.StringVar(value="Full video")
tk.OptionMenu(_time_row, encode_time_mode_var,
              "Full video", "Full video, chunked"
              ).pack(side="left", padx=(0, 12))
tk.Label(_time_row, text="Chunk length (min):",
         foreground=PALETTE["text_dim"]).pack(side="left")
encode_chunk_entry = tk.Entry(_time_row, width=8)
encode_chunk_entry.insert(0, "30")
encode_chunk_entry.pack(side="left", padx=(6, 0))

make_primary_button(encode_tab, "Encode videos",
                    encode_process_videos).pack(pady=18)

encode_progress = BatchProgress(encode_tab)


# ======================================================================
# TAB 2: CONCAT CHUNKS
# (from concat_chunks.py)
# ======================================================================
CHUNK_RE = re.compile(
    r"^(?P<prefix>.+)_chunk(?P<idx>\d+)_(?P<suffix>[^_.]+)\.(?P<ext>\w+)$"
)

concat_state = {
    "folder": None,
    "groups": {},
    "validations": {},
    "selections": {},
    "other_files": [],
    "existing_outputs": {},
    "phases": {},          # group_key -> dict(mode, p1_start, p1_end, p2_start, p2_end)
    "phase_vars": {},      # group_key -> dict of tk Vars (rebuilt each refresh)
    "single_files": {},    # path -> info dict from probe
    "single_phase_vars": {},  # path -> dict of tk Vars (rebuilt each refresh)
    "single_selections": {},  # path -> tk.BooleanVar (only matters when mode != "No split")
}


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi"}

# Match output files we've produced previously, so we don't offer to
# phase-split a file that's already a phase-split output.
OUTPUT_RE = re.compile(
    r"_(merged|phase1|phase2|fragment)(_[^_.]+)?\.[a-zA-Z0-9]+$"
)


def concat_find_groups_and_others(folder):
    """
    Returns (groups, single_files, other_files):
      - groups: dict[group_key] -> list of (idx, filepath); chunked outputs.
      - single_files: list of paths to video files that are NOT chunks AND
                      NOT output files from prior runs. Eligible for phase
                      split.
      - other_files: everything else (sidecar JSONs, logs, prior outputs,
                     non-video files). Listed as untouched.
    """
    groups = defaultdict(list)
    single_files = []
    other_files = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue

        m = CHUNK_RE.match(name)
        if m:
            key = f"{m['prefix']}_{m['suffix']}.{m['ext']}"
            groups[key].append((int(m["idx"]), full))
            continue

        # Not a chunk. Is it a video file?
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS and not OUTPUT_RE.search(name):
            single_files.append(full)
        else:
            other_files.append(name)

    result = {}
    for key, items in groups.items():
        items.sort(key=lambda t: t[0])
        result[key] = items
    return result, single_files, other_files


def concat_probe_codec_geometry(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", "-select_streams", "v:0", path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    s = data["streams"][0]
    duration = None
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    return {
        "codec": s.get("codec_name"),
        "width": s.get("width"),
        "height": s.get("height"),
        "pix_fmt": s.get("pix_fmt"),
        "r_frame_rate": s.get("r_frame_rate"),
        "duration": duration,
    }


def concat_validate_group(items):
    if not items:
        return False, {"error": "empty group"}
    indices = [idx for idx, _ in items]
    expected = list(range(len(items)))
    if indices != expected:
        missing = sorted(set(expected) - set(indices))
        return False, {"error": f"missing chunks {missing} (have {indices})"}
    try:
        first_meta = concat_probe_codec_geometry(items[0][1])
    except subprocess.CalledProcessError as e:
        return False, {"error": f"probe failed on chunk000: {e}"}
    # Geometry comparison key (exclude duration)
    def geom(m):
        return {k: m[k] for k in ("codec", "width", "height",
                                  "pix_fmt", "r_frame_rate")}
    first_geom = geom(first_meta)
    total_duration = first_meta["duration"] or 0.0
    for idx, path in items[1:]:
        try:
            m = concat_probe_codec_geometry(path)
        except subprocess.CalledProcessError as e:
            return False, {"error": f"probe failed on chunk{idx:03d}: {e}"}
        if geom(m) != first_geom:
            return False, {"error": f"chunk{idx:03d} stream params differ"}
        total_duration += (m["duration"] or 0.0)
    total_size = sum(os.path.getsize(p) for _, p in items)
    return True, {
        "n_chunks": len(items),
        "codec": first_meta["codec"],
        "width": first_meta["width"],
        "height": first_meta["height"],
        "fps": first_meta["r_frame_rate"],
        "total_size_bytes": total_size,
        "merged_duration_sec": total_duration,
    }


def concat_output_name_for_group(key):
    m = re.match(r"^(?P<prefix>.+)_(?P<suffix>[^_.]+)\.(?P<ext>\w+)$", key)
    if m:
        return f"{m['prefix']}_merged_{m['suffix']}.{m['ext']}"
    return key.replace(".", "_merged.", 1)


def concat_phase_output_name(key, phase_label):
    """e.g. 'A006_left_crop_nvenc.mp4', 'phase1' -> 'A006_left_crop_merged_phase1_nvenc.mp4'."""
    m = re.match(r"^(?P<prefix>.+)_(?P<suffix>[^_.]+)\.(?P<ext>\w+)$", key)
    if m:
        return f"{m['prefix']}_merged_{phase_label}_{m['suffix']}.{m['ext']}"
    base, ext = os.path.splitext(key)
    return f"{base}_merged_{phase_label}{ext}"


def concat_phases_json_path(folder, key):
    """Phase sidecar lives next to the chunks; one per group."""
    # Strip extension from key for cleaner sidecar name
    base = os.path.splitext(key)[0]
    return os.path.join(folder, f"{base}.phases.json")


def concat_load_phase_json(folder, key):
    """Returns dict (possibly empty) loaded from the sidecar, or {} on miss/error."""
    p = concat_phases_json_path(folder, key)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def concat_save_phase_json(folder, key, data):
    p = concat_phases_json_path(folder, key)
    try:
        with open(p, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        log(f"  warning: could not save phases sidecar: {e}")
        return False


def concat_probe_single_video(path):
    """Probe a single (non-chunk) video for phase-split eligibility.
    Returns (ok, info_dict). info contains codec/width/height/duration/size."""
    try:
        cmd = ["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", "-select_streams", "v:0", path]
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(r.stdout)
        s = data["streams"][0]
        duration = None
        try:
            duration = float(data.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            pass
        return True, {
            "codec": s.get("codec_name"),
            "width": s.get("width"),
            "height": s.get("height"),
            "fps": s.get("r_frame_rate"),
            "duration_sec": duration,
            "size_bytes": os.path.getsize(path),
        }
    except (subprocess.CalledProcessError,
            json.JSONDecodeError, KeyError, IndexError) as e:
        return False, {"error": f"probe failed: {e}"}


def concat_single_phase_output_name(path, phase_label):
    """e.g. '/x/A006_full_nvenc.mp4', 'phase1' -> '/x/A006_full_nvenc_phase1.mp4'."""
    base, ext = os.path.splitext(path)
    return f"{base}_{phase_label}{ext}"


def concat_single_phases_json_path(path):
    """Sidecar for a single video lives next to it: <basename>.phases.json"""
    base = os.path.splitext(path)[0]
    return f"{base}.phases.json"


def concat_load_single_phase_json(path):
    p = concat_single_phases_json_path(path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def concat_save_single_phase_json(path, data):
    p = concat_single_phases_json_path(path)
    try:
        with open(p, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        log(f"  warning: could not save phases sidecar: {e}")
        return False


def concat_read_phase_config(key):
    """Read current phase Vars for a group into a plain dict."""
    pv = concat_state["phase_vars"].get(key)
    if not pv:
        return {"mode": "No split"}
    return {
        "mode": pv["mode"].get(),
        "phase1_start": pv["p1s"].get().strip(),
        "phase1_end": pv["p1e"].get().strip(),
        "phase2_start": pv["p2s"].get().strip(),
        "phase2_end": pv["p2e"].get().strip(),
    }


def concat_read_single_phase_config(path):
    """Read current phase Vars for a single video into a plain dict."""
    pv = concat_state["single_phase_vars"].get(path)
    if not pv:
        return {"mode": "No split"}
    return {
        "mode": pv["mode"].get(),
        "phase1_start": pv["p1s"].get().strip(),
        "phase1_end": pv["p1e"].get().strip(),
        "phase2_start": pv["p2s"].get().strip(),
        "phase2_end": pv["p2e"].get().strip(),
    }


def concat_validate_phase_config(cfg, merged_duration):
    """
    Validate timestamps. Returns (ok, message_or_seconds_dict).
    On success returns (True, {"p1": (s, e), "p2": (s, e) or None}).
    On failure returns (False, error_message).
    """
    mode = cfg.get("mode", "No split")
    if mode == "No split":
        return True, None

    try:
        p1s = parse_hms(cfg.get("phase1_start", ""))
        p1e = parse_hms(cfg.get("phase1_end", ""))
    except ValueError as e:
        return False, f"Phase 1: {e}"
    if p1e <= p1s:
        return False, "Phase 1: end must be after start"
    if merged_duration and p1e > merged_duration + 1:
        return False, (f"Phase 1 end ({hms(p1e)}) exceeds "
                       f"merged duration ({hms(merged_duration)})")

    if mode == "Phase 1 only":
        return True, {"p1": (p1s, p1e), "p2": None}

    # Phase 1 + Phase 2
    try:
        p2s = parse_hms(cfg.get("phase2_start", ""))
        p2e = parse_hms(cfg.get("phase2_end", ""))
    except ValueError as e:
        return False, f"Phase 2: {e}"
    if p2e <= p2s:
        return False, "Phase 2: end must be after start"
    if p2s < p1e:
        return False, (f"Phase 2 start ({hms(p2s)}) must be >= "
                       f"Phase 1 end ({hms(p1e)})")
    if merged_duration and p2e > merged_duration + 1:
        return False, (f"Phase 2 end ({hms(p2e)}) exceeds "
                       f"merged duration ({hms(merged_duration)})")

    return True, {"p1": (p1s, p1e), "p2": (p2s, p2e)}


def parse_hms(s):
    """Parse hh:mm:ss to seconds. Raises ValueError on bad input.
    Also accepts ';', '.', ',' as separators since they're common typo
    neighbours of ':' (especially on certain keyboard layouts)."""
    s = s.strip()
    if not s:
        raise ValueError("empty time")
    # Normalize common typo separators to ':'
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
    """Inverse of parse_hms for default values."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def concat_run(items, output_path):
    list_path = output_path + ".concat_list.txt"
    with open(list_path, "w") as f:
        for _, path in items:
            safe = path.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostdin",
           "-f", "concat", "-safe", "0",
           "-i", list_path,
           "-c", "copy",
           "-movflags", "+faststart",
           output_path]
    log(f"  $ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, bufsize=0)
    err_lines = []

    def pump():
        while True:
            ch = proc.stderr.read(256)
            if not ch:
                break
            err_lines.append(ch.decode("utf-8", errors="replace"))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=2)
    try:
        os.remove(list_path)
    except OSError:
        pass
    if proc.returncode != 0:
        log(f"  FAILED rc={proc.returncode}")
        log("".join(err_lines)[-2000:])
        return False
    return True


def probe_keyframe_times(path, start_sec, window_sec=120.0):
    """
    Sorted video keyframe presentation times (s) found in
    [start_sec, start_sec + window_sec], via ffprobe packet flags. The window is
    restricted with -read_intervals so this stays fast on multi-hour archives.
    (-read_intervals seeks to the keyframe *before* the window start, so callers
    must filter to times >= start_sec themselves.)
    """
    lo = max(0.0, start_sec)
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags",
           "-of", "csv=print_section=0",
           "-read_intervals", f"{lo}%{lo + window_sec}",
           path]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    times = []
    for line in r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        pts, flags = parts[0].strip(), parts[1]
        if "K" not in flags:            # keyframe packets carry the 'K' flag
            continue
        try:
            times.append(float(pts))
        except ValueError:
            continue
    return sorted(times)


def first_keyframe_at_or_after(path, start_sec):
    """
    The first keyframe time >= start_sec (the forward-snap cut point), or None
    if no keyframe is found at/after start_sec within the search window (e.g. a
    handling fragment shorter than one GOP). Widens the window once before None.
    """
    for window in (120.0, 600.0):
        for t in probe_keyframe_times(path, start_sec, window):
            if t >= start_sec - 1e-6:
                return t
    return None


def concat_trim_phase(merged_path, output_path, start_sec, end_sec):
    """
    Lossless stream-copy trim (no re-encode). The start snaps FORWARD to the
    first keyframe at or after start_sec, never backward; the end is held at
    end_sec. -avoid_negative_ts make_zero rewrites output PTS to start at 0.

    Why forward, not backward: a stream copy can only start on a keyframe, and
    these archives have a long default GOP (~8 s). Snapping backward let a
    phase-2 clip begin before its requested time and pull handling-period frames
    (the brush in view) into the analysed footage -- a data-integrity bug.
    Forward-snap guarantees the clip never begins before the requested time.
    Trade-off: the start is now up to one GOP LATE, by a variable amount (the
    gap to the next keyframe); a few dropped lead-in seconds are harmless,
    early contamination is not. See design_decisions.md V.3.

    Falls back to a backward-snap trim (and logs it) if no keyframe is found
    at/after start_sec within the search window (e.g. a sub-GOP fragment).
    """
    kf = first_keyframe_at_or_after(merged_path, start_sec)
    if kf is None:
        log(f"  note: no keyframe >= {start_sec:.3f}s within search window; "
            f"backward-snap fallback for {os.path.basename(output_path)}")
        seek_sec, duration = start_sec, end_sec - start_sec
    else:
        # +0.05 s so the input seek lands ON this keyframe, not the previous one
        # (guards against float rounding); safely inside the GOP.
        seek_sec, duration = kf + 0.05, end_sec - kf
    if duration <= 0:
        anchor = kf if kf is not None else start_sec
        log(f"  TRIM FAILED: snapped start ({anchor:.3f}s) is at/after end "
            f"({end_sec:.3f}s) for {os.path.basename(output_path)}")
        return False
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostdin",
           "-ss", f"{seek_sec:.6f}",
           "-i", merged_path,
           "-t", f"{duration:.6f}",
           "-c", "copy",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart",
           output_path]
    log(f"  $ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, bufsize=0)
    err_lines = []

    def pump():
        while True:
            ch = proc.stderr.read(256)
            if not ch:
                break
            err_lines.append(ch.decode("utf-8", errors="replace"))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        log(f"  TRIM FAILED rc={proc.returncode}")
        log("".join(err_lines)[-2000:])
        return False
    return True


def concat_browse_folder():
    folder = filedialog.askdirectory(
        title="Select folder with chunked outputs")
    if not folder:
        return
    concat_state["folder"] = folder
    concat_folder_label.config(text=folder)
    concat_refresh_plan()


def concat_refresh_plan():
    folder = concat_state["folder"]
    if not folder:
        return
    groups, single_files, others = concat_find_groups_and_others(folder)
    concat_state["groups"] = groups
    concat_state["other_files"] = others
    concat_state["selections"].clear()
    concat_state["validations"].clear()
    concat_state["existing_outputs"].clear()
    concat_state["phase_vars"].clear()
    concat_state["single_files"].clear()
    concat_state["single_phase_vars"].clear()
    concat_state["single_selections"].clear()

    for w in concat_plan_frame.winfo_children():
        w.destroy()

    if not groups and not single_files and not others:
        tk.Label(concat_plan_frame, text="(empty folder)",
                 fg=PALETTE["text_faint"],
                 background=PALETTE["panel"]).pack(pady=20)
        return

    concat_status_label.config(text="Validating chunk groups…",
                               fg=PALETTE["text_dim"])
    root.update_idletasks()

    valid_groups = {}
    invalid_groups = {}
    for key, items in groups.items():
        ok, info = concat_validate_group(items)
        concat_state["validations"][key] = (ok, info)
        if ok:
            valid_groups[key] = (items, info)
            out_name = concat_output_name_for_group(key)
            concat_state["existing_outputs"][key] = os.path.exists(
                os.path.join(folder, out_name))
        else:
            invalid_groups[key] = (items, info)

    if valid_groups:
        tk.Label(concat_plan_frame,
                 text=f"Chunked groups  ({len(valid_groups)})",
                 font=FONT_SECTION, fg=PALETTE["ok"],
                 background=PALETTE["panel"], anchor="w"
                 ).pack(fill="x", padx=12, pady=(10, 4))
        for key, (items, info) in sorted(valid_groups.items()):
            var = tk.BooleanVar(value=False)
            concat_state["selections"][key] = var
            out_name = concat_output_name_for_group(key)
            exists = concat_state["existing_outputs"][key]
            exists_tag = "  · output exists, will overwrite" if exists else ""
            row = tk.Frame(concat_plan_frame,
                           background=PALETTE["panel_alt"],
                           highlightbackground=PALETTE["border"],
                           highlightthickness=1)
            row.pack(fill="x", padx=12, pady=4, anchor="w")

            # Top: chunks summary
            top = tk.Frame(row, background=PALETTE["panel_alt"])
            top.pack(fill="x", padx=8, pady=(6, 2))
            tk.Checkbutton(top, variable=var,
                           background=PALETTE["panel_alt"],
                           activebackground=PALETTE["panel_alt"]
                           ).pack(side="left", anchor="n")
            tf = tk.Frame(top, background=PALETTE["panel_alt"])
            tf.pack(side="left", fill="x", expand=True, anchor="w")
            tk.Label(tf,
                     text=f"{info['n_chunks']} chunks  →  "
                          f"{out_name}{exists_tag}",
                     anchor="w", font=FONT_UI_BOLD,
                     fg=(PALETTE["warn"] if exists else PALETTE["text"]),
                     background=PALETTE["panel_alt"]
                     ).pack(fill="x")
            duration_str = (hms(info['merged_duration_sec'])
                            if info.get('merged_duration_sec') else "?")
            tk.Label(tf,
                     text=(f"   {info['codec']} "
                           f"{info['width']}x{info['height']} "
                           f"@ {info['fps']}    total: "
                           f"{humansize(info['total_size_bytes'])}    "
                           f"merged duration: {duration_str}"),
                     anchor="w", font=FONT_MONO_SM, fg=PALETTE["text_dim"],
                     background=PALETTE["panel_alt"]
                     ).pack(fill="x")
            chunks_str = ", ".join(os.path.basename(p) for _, p in items)
            tk.Label(tf, text=f"   chunks: {chunks_str}",
                     anchor="w", font=FONT_MONO_SM, fg=PALETTE["text_faint"],
                     background=PALETTE["panel_alt"],
                     wraplength=850, justify="left").pack(fill="x")

            # Phase config row. Load existing JSON or use empty default.
            phase_data = concat_load_phase_json(folder, key)
            mode_var = tk.StringVar(value=phase_data.get("mode", "No split"))
            p1s_var = tk.StringVar(value=phase_data.get("phase1_start", "00:00:00"))
            p1e_var = tk.StringVar(value=phase_data.get("phase1_end", ""))
            p2s_var = tk.StringVar(value=phase_data.get("phase2_start", ""))
            p2e_var = tk.StringVar(value=phase_data.get("phase2_end", ""))

            concat_state["phase_vars"][key] = {
                "mode": mode_var,
                "p1s": p1s_var, "p1e": p1e_var,
                "p2s": p2s_var, "p2e": p2e_var,
            }

            phase_row = tk.Frame(row, background=PALETTE["panel_alt"])
            phase_row.pack(fill="x", padx=30, pady=(0, 8))

            tk.Label(phase_row, text="Phase split:",
                     font=FONT_SMALL, fg=PALETTE["text_dim"],
                     background=PALETTE["panel_alt"]
                     ).pack(side="left", padx=(0, 6))
            tk.OptionMenu(phase_row, mode_var,
                          "No split",
                          "Phase 1 only",
                          "Phase 1 + Phase 2").pack(side="left")

            # Phase 1 fields
            p1_frame = tk.Frame(phase_row, background=PALETTE["panel_alt"])
            p1_frame.pack(side="left", padx=10)
            tk.Label(p1_frame, text="P1",
                     font=FONT_SMALL, fg=PALETTE["text_dim"],
                     background=PALETTE["panel_alt"]
                     ).pack(side="left", padx=(0, 4))
            p1s_entry = tk.Entry(p1_frame, textvariable=p1s_var, width=10,
                                 font=FONT_MONO)
            p1s_entry.pack(side="left", padx=2)
            tk.Label(p1_frame, text="–",
                     font=FONT_SMALL, fg=PALETTE["text_faint"],
                     background=PALETTE["panel_alt"]).pack(side="left")
            p1e_entry = tk.Entry(p1_frame, textvariable=p1e_var, width=10,
                                 font=FONT_MONO)
            p1e_entry.pack(side="left", padx=2)

            # Phase 2 fields
            p2_frame = tk.Frame(phase_row, background=PALETTE["panel_alt"])
            p2_frame.pack(side="left", padx=10)
            tk.Label(p2_frame, text="P2",
                     font=FONT_SMALL, fg=PALETTE["text_dim"],
                     background=PALETTE["panel_alt"]
                     ).pack(side="left", padx=(0, 4))
            p2s_entry = tk.Entry(p2_frame, textvariable=p2s_var, width=10,
                                 font=FONT_MONO)
            p2s_entry.pack(side="left", padx=2)
            tk.Label(p2_frame, text="–",
                     font=FONT_SMALL, fg=PALETTE["text_faint"],
                     background=PALETTE["panel_alt"]).pack(side="left")
            p2e_entry = tk.Entry(p2_frame, textvariable=p2e_var, width=10,
                                 font=FONT_MONO)
            p2e_entry.pack(side="left", padx=2)

            # Enable/disable based on mode
            def make_state_updater(m_var, p1_widgets, p2_widgets):
                def update(*_):
                    mv = m_var.get()
                    p1_state = "normal" if mv != "No split" else "disabled"
                    p2_state = "normal" if mv == "Phase 1 + Phase 2" else "disabled"
                    for w in p1_widgets:
                        w.config(state=p1_state)
                    for w in p2_widgets:
                        w.config(state=p2_state)
                return update

            updater = make_state_updater(
                mode_var,
                (p1s_entry, p1e_entry),
                (p2s_entry, p2e_entry),
            )
            mode_var.trace_add("write", updater)
            updater()  # initial

            # Auto-save phase config to JSON sidecar whenever the user
            # tabs/clicks out of a field. Belt-and-braces against losing
            # typed timestamps if the tool crashes or is closed.
            def make_autosave(k):
                def _autosave(*_):
                    try:
                        cfg = concat_read_phase_config(k)
                        concat_save_phase_json(folder, k, cfg)
                    except Exception:
                        pass  # don't propagate; this is best-effort
                return _autosave

            autosave = make_autosave(key)
            for w in (p1s_entry, p1e_entry, p2s_entry, p2e_entry):
                w.bind("<FocusOut>", autosave)
            mode_var.trace_add("write", lambda *_, _f=autosave: _f())

    # ---- Section: SINGLE FILES (eligible for phase split) ----
    if single_files:
        # Probe each first
        for path in single_files:
            ok, info = concat_probe_single_video(path)
            concat_state["single_files"][path] = (ok, info)

        valid_singles = [(p, i[1]) for p, i in
                         concat_state["single_files"].items() if i[0]]
        invalid_singles = [(p, i[1]) for p, i in
                           concat_state["single_files"].items() if not i[0]]

        if valid_singles:
            tk.Label(
                concat_plan_frame,
                text=(f"Single files  ({len(valid_singles)} "
                      f"eligible for phase split)"),
                font=FONT_SECTION, fg=PALETTE["ok"],
                background=PALETTE["panel"], anchor="w"
            ).pack(fill="x", padx=12, pady=(14, 4))

            for path, info in sorted(valid_singles):
                row = tk.Frame(concat_plan_frame,
                               background=PALETTE["panel_alt"],
                               highlightbackground=PALETTE["border"],
                               highlightthickness=1)
                row.pack(fill="x", padx=12, pady=4, anchor="w")

                top = tk.Frame(row, background=PALETTE["panel_alt"])
                top.pack(fill="x", padx=8, pady=(6, 2))
                sel_var = tk.BooleanVar(value=False)
                concat_state["single_selections"][path] = sel_var
                tk.Checkbutton(top, variable=sel_var,
                               background=PALETTE["panel_alt"],
                               activebackground=PALETTE["panel_alt"]
                               ).pack(side="left", anchor="n")
                tf = tk.Frame(top, background=PALETTE["panel_alt"])
                tf.pack(side="left", fill="x", expand=True, anchor="w")

                tk.Label(tf,
                         text=os.path.basename(path),
                         anchor="w", font=FONT_UI_BOLD,
                         background=PALETTE["panel_alt"]
                         ).pack(fill="x")
                duration_str = (hms(info["duration_sec"])
                                if info.get("duration_sec") else "?")
                size_str = humansize(info["size_bytes"])
                tk.Label(tf,
                         text=(f"   {info.get('codec','?')} "
                               f"{info.get('width','?')}x{info.get('height','?')} "
                               f"@ {info.get('fps','?')}    "
                               f"size: {size_str}    "
                               f"duration: {duration_str}"),
                         anchor="w", font=FONT_MONO_SM,
                         fg=PALETTE["text_dim"],
                         background=PALETTE["panel_alt"]
                         ).pack(fill="x")

                # Load existing phase JSON if any
                phase_data = concat_load_single_phase_json(path)
                mode_var = tk.StringVar(
                    value=phase_data.get("mode", "No split"))
                p1s_var = tk.StringVar(
                    value=phase_data.get("phase1_start", "00:00:00"))
                p1e_var = tk.StringVar(value=phase_data.get("phase1_end", ""))
                p2s_var = tk.StringVar(value=phase_data.get("phase2_start", ""))
                p2e_var = tk.StringVar(value=phase_data.get("phase2_end", ""))

                concat_state["single_phase_vars"][path] = {
                    "mode": mode_var,
                    "p1s": p1s_var, "p1e": p1e_var,
                    "p2s": p2s_var, "p2e": p2e_var,
                }

                phase_row = tk.Frame(row, background=PALETTE["panel_alt"])
                phase_row.pack(fill="x", padx=30, pady=(0, 8))
                tk.Label(phase_row, text="Phase split:",
                         font=FONT_SMALL, fg=PALETTE["text_dim"],
                         background=PALETTE["panel_alt"]
                         ).pack(side="left", padx=(0, 6))
                tk.OptionMenu(phase_row, mode_var,
                              "No split",
                              "Phase 1 only",
                              "Phase 1 + Phase 2"
                              ).pack(side="left")

                p1_frame = tk.Frame(phase_row,
                                    background=PALETTE["panel_alt"])
                p1_frame.pack(side="left", padx=10)
                tk.Label(p1_frame, text="P1",
                         font=FONT_SMALL, fg=PALETTE["text_dim"],
                         background=PALETTE["panel_alt"]
                         ).pack(side="left", padx=(0, 4))
                p1s_entry = tk.Entry(p1_frame, textvariable=p1s_var,
                                     width=10, font=FONT_MONO)
                p1s_entry.pack(side="left", padx=2)
                tk.Label(p1_frame, text="–",
                         font=FONT_SMALL, fg=PALETTE["text_faint"],
                         background=PALETTE["panel_alt"]
                         ).pack(side="left")
                p1e_entry = tk.Entry(p1_frame, textvariable=p1e_var,
                                     width=10, font=FONT_MONO)
                p1e_entry.pack(side="left", padx=2)

                p2_frame = tk.Frame(phase_row,
                                    background=PALETTE["panel_alt"])
                p2_frame.pack(side="left", padx=10)
                tk.Label(p2_frame, text="P2",
                         font=FONT_SMALL, fg=PALETTE["text_dim"],
                         background=PALETTE["panel_alt"]
                         ).pack(side="left", padx=(0, 4))
                p2s_entry = tk.Entry(p2_frame, textvariable=p2s_var,
                                     width=10, font=FONT_MONO)
                p2s_entry.pack(side="left", padx=2)
                tk.Label(p2_frame, text="–",
                         font=FONT_SMALL, fg=PALETTE["text_faint"],
                         background=PALETTE["panel_alt"]
                         ).pack(side="left")
                p2e_entry = tk.Entry(p2_frame, textvariable=p2e_var,
                                     width=10, font=FONT_MONO)
                p2e_entry.pack(side="left", padx=2)

                def make_state_updater(m_var, p1_widgets, p2_widgets):
                    def update(*_):
                        mv = m_var.get()
                        p1_state = ("normal" if mv != "No split"
                                    else "disabled")
                        p2_state = ("normal" if mv == "Phase 1 + Phase 2"
                                    else "disabled")
                        for w in p1_widgets:
                            w.config(state=p1_state)
                        for w in p2_widgets:
                            w.config(state=p2_state)
                    return update

                updater = make_state_updater(
                    mode_var,
                    (p1s_entry, p1e_entry),
                    (p2s_entry, p2e_entry),
                )
                mode_var.trace_add("write", updater)
                updater()

                # Auto-save phase config on focus-out
                def make_single_autosave(p):
                    def _autosave(*_):
                        try:
                            cfg = concat_read_single_phase_config(p)
                            concat_save_single_phase_json(p, cfg)
                        except Exception:
                            pass
                    return _autosave

                autosave = make_single_autosave(path)
                for w in (p1s_entry, p1e_entry, p2s_entry, p2e_entry):
                    w.bind("<FocusOut>", autosave)
                mode_var.trace_add("write",
                                   lambda *_, _f=autosave: _f())

        if invalid_singles:
            tk.Label(
                concat_plan_frame,
                text=(f"Single files with probe errors  "
                      f"({len(invalid_singles)})"),
                font=FONT_SECTION, fg=PALETTE["err"],
                background=PALETTE["panel"], anchor="w"
            ).pack(fill="x", padx=12, pady=(12, 4))
            for path, info in sorted(invalid_singles):
                row = tk.Frame(concat_plan_frame,
                               background=PALETTE["panel"])
                row.pack(fill="x", padx=30, pady=1, anchor="w")
                tk.Label(row, text=os.path.basename(path), anchor="w",
                         font=FONT_UI_BOLD, fg=PALETTE["err"],
                         background=PALETTE["panel"]
                         ).pack(fill="x")
                tk.Label(row, text=f"   {info.get('error','unknown')}",
                         anchor="w", font=FONT_MONO_SM, fg=PALETTE["err"],
                         background=PALETTE["panel"]
                         ).pack(fill="x")

    if invalid_groups:
        tk.Label(concat_plan_frame,
                 text=(f"Skipped — chunk groups with problems  "
                       f"({len(invalid_groups)})"),
                 font=FONT_SECTION, fg=PALETTE["err"],
                 background=PALETTE["panel"], anchor="w"
                 ).pack(fill="x", padx=12, pady=(14, 4))
        for key, (items, info) in sorted(invalid_groups.items()):
            row = tk.Frame(concat_plan_frame, background=PALETTE["panel"])
            row.pack(fill="x", padx=30, pady=1, anchor="w")
            tk.Label(row, text=key, anchor="w",
                     font=FONT_UI_BOLD, fg=PALETTE["err"],
                     background=PALETTE["panel"]
                     ).pack(fill="x")
            tk.Label(row, text=f"   reason: {info.get('error', 'unknown')}",
                     anchor="w", font=FONT_MONO_SM, fg=PALETTE["err"],
                     background=PALETTE["panel"]
                     ).pack(fill="x")

    if others:
        tk.Label(concat_plan_frame,
                 text=(f"Untouched — sidecars, logs, prior outputs, etc.  "
                       f"({len(others)})"),
                 font=FONT_SECTION, fg=PALETTE["text_dim"],
                 background=PALETTE["panel"], anchor="w"
                 ).pack(fill="x", padx=12, pady=(14, 4))
        by_ext = defaultdict(list)
        for name in others:
            _, ext = os.path.splitext(name)
            by_ext[ext.lower()].append(name)
        for ext in sorted(by_ext.keys()):
            names = by_ext[ext]
            tk.Label(concat_plan_frame,
                     text=f"   {ext or '(no ext)'}: {len(names)} file(s)",
                     anchor="w", font=FONT_UI_BOLD, fg=PALETTE["text_dim"],
                     background=PALETTE["panel"]
                     ).pack(fill="x", padx=30)
            if len(names) <= 8:
                for n in names:
                    tk.Label(concat_plan_frame, text=f"      {n}",
                             anchor="w", font=FONT_MONO_SM,
                             fg=PALETTE["text_faint"],
                             background=PALETTE["panel"]
                             ).pack(fill="x", padx=30)
            else:
                tk.Label(concat_plan_frame,
                         text=(f"      {names[0]}, {names[1]}, "
                               f"... (+{len(names)-2} more)"),
                         anchor="w", font=FONT_MONO_SM,
                         fg=PALETTE["text_faint"],
                         background=PALETTE["panel"]
                         ).pack(fill="x", padx=30)

    n_valid = len(valid_groups)
    total_chunks_to_merge = sum(len(items)
                                for items, _ in valid_groups.values())
    total_size = sum(info["total_size_bytes"]
                     for _, info in valid_groups.values())
    n_singles = sum(1 for _, (ok, _)
                    in concat_state["single_files"].items() if ok)
    concat_status_label.config(
        text=(f"Plan: merge {n_valid} group(s) "
              f"({total_chunks_to_merge} chunks, "
              f"{humansize(total_size)} total) + "
              f"{n_singles} single file(s) eligible for phase split \u2014 "
              f"{len(invalid_groups)} skipped \u2014 "
              f"{len(others)} other file(s) untouched"),
        fg=PALETTE["text_dim"],
    )


def concat_show_confirmation_and_merge():
    folder = concat_state["folder"]
    if not folder:
        messagebox.showerror("Error", "Select a folder first.")
        return
    selected = []
    phase_problems = []
    for key, var in concat_state["selections"].items():
        if not var.get():
            continue
        ok, info = concat_state["validations"][key]
        if not ok:
            continue
        # Read and validate phase config
        phase_cfg = concat_read_phase_config(key)
        merged_duration = info.get("merged_duration_sec", 0)
        pok, presult = concat_validate_phase_config(phase_cfg,
                                                    merged_duration)
        if not pok:
            phase_problems.append(f"  {key}: {presult}")
            continue
        selected.append((key, concat_state["groups"][key], info,
                         phase_cfg, presult))

    # Single files: only include those with mode != "No split" AND selected.
    single_jobs = []
    for path, sel_var in concat_state["single_selections"].items():
        if not sel_var.get():
            continue
        ok, info = concat_state["single_files"].get(path, (False, {}))
        if not ok:
            continue
        cfg = concat_read_single_phase_config(path)
        if cfg.get("mode", "No split") == "No split":
            continue  # nothing to do
        duration = info.get("duration_sec", 0)
        pok, presult = concat_validate_phase_config(cfg, duration)
        if not pok:
            phase_problems.append(
                f"  {os.path.basename(path)}: {presult}")
            continue
        single_jobs.append((path, info, cfg, presult))

    if phase_problems:
        messagebox.showerror(
            "Phase config errors",
            "Fix these before merging:\n\n" + "\n".join(phase_problems))
        return

    if not selected and not single_jobs:
        messagebox.showinfo(
            "Nothing to do",
            "No groups selected and no single files have phase split enabled.")
        return

    delete_after = concat_delete_var.get()
    lines = [f"You are about to perform the following actions in:",
             f"  {folder}", ""]

    # Chunked groups
    if selected:
        lines.append(f"PROCESS {len(selected)} chunked group(s):")
        for key, items, info, phase_cfg, phase_seconds in selected:
            out_name = concat_output_name_for_group(key)
            exists = concat_state["existing_outputs"].get(key, False)
            suffix = "  (OVERWRITE existing)" if exists else ""
            lines.append("")
            lines.append(f"  {key}  ({info['n_chunks']} chunks, "
                         f"{humansize(info['total_size_bytes'])}, "
                         f"merged duration "
                         f"{hms(info.get('merged_duration_sec', 0))})")
            mode = phase_cfg["mode"]
            if mode == "No split":
                lines.append(f"    -> {out_name}{suffix}  "
                             f"(full merge kept)")
            elif mode == "Phase 1 only":
                p1s, p1e = phase_seconds["p1"]
                p1_name = concat_phase_output_name(key, "phase1")
                lines.append(f"    -> {out_name}{suffix}  "
                             f"(intermediate, deleted after)")
                lines.append(f"    -> {p1_name}  "
                             f"({hms(p1s)} - {hms(p1e)}, "
                             f"length {hms(p1e - p1s)})")
            else:
                p1s, p1e = phase_seconds["p1"]
                p2s, p2e = phase_seconds["p2"]
                p1_name = concat_phase_output_name(key, "phase1")
                p2_name = concat_phase_output_name(key, "phase2")
                lines.append(f"    -> {out_name}{suffix}  "
                             f"(intermediate, deleted after)")
                lines.append(f"    -> {p1_name}  "
                             f"({hms(p1s)} - {hms(p1e)}, "
                             f"length {hms(p1e - p1s)})")
                if p2s > p1e:
                    frag_name = concat_phase_output_name(key, "fragment")
                    lines.append(f"    -> {frag_name}  "
                                 f"({hms(p1e)} - {hms(p2s)}, "
                                 f"length {hms(p2s - p1e)})  "
                                 f"[always kept]")
                lines.append(f"    -> {p2_name}  "
                             f"({hms(p2s)} - {hms(p2e)}, "
                             f"length {hms(p2e - p2s)})")

    # Single files
    if single_jobs:
        lines.append("")
        lines.append(f"PHASE-SPLIT {len(single_jobs)} single file(s) "
                     f"(source files NOT deleted):")
        for path, info, cfg, presult in single_jobs:
            duration = info.get("duration_sec", 0)
            lines.append("")
            lines.append(f"  {os.path.basename(path)}  "
                         f"(duration {hms(duration)})")
            mode = cfg["mode"]
            if mode == "Phase 1 only":
                p1s, p1e = presult["p1"]
                p1_path = concat_single_phase_output_name(path, "phase1")
                p1_name = os.path.basename(p1_path)
                exists = "  (OVERWRITE)" if os.path.exists(p1_path) else ""
                lines.append(f"    -> {p1_name}{exists}  "
                             f"({hms(p1s)} - {hms(p1e)}, "
                             f"length {hms(p1e - p1s)})")
            else:
                p1s, p1e = presult["p1"]
                p2s, p2e = presult["p2"]
                p1_path = concat_single_phase_output_name(path, "phase1")
                p2_path = concat_single_phase_output_name(path, "phase2")
                p1_name = os.path.basename(p1_path)
                p2_name = os.path.basename(p2_path)
                p1_ex = "  (OVERWRITE)" if os.path.exists(p1_path) else ""
                p2_ex = "  (OVERWRITE)" if os.path.exists(p2_path) else ""
                lines.append(f"    -> {p1_name}{p1_ex}  "
                             f"({hms(p1s)} - {hms(p1e)}, "
                             f"length {hms(p1e - p1s)})")
                if p2s > p1e:
                    frag_path = concat_single_phase_output_name(
                        path, "fragment")
                    frag_name = os.path.basename(frag_path)
                    frag_ex = ("  (OVERWRITE)"
                               if os.path.exists(frag_path) else "")
                    lines.append(f"    -> {frag_name}{frag_ex}  "
                                 f"({hms(p1e)} - {hms(p2s)}, "
                                 f"length {hms(p2s - p1e)})  "
                                 f"[always kept]")
                lines.append(f"    -> {p2_name}{p2_ex}  "
                             f"({hms(p2s)} - {hms(p2e)}, "
                             f"length {hms(p2e - p2s)})")

    if delete_after and selected:
        n_to_delete = sum(info["n_chunks"]
                          for _, _, info, _, _ in selected)
        lines.append("")
        lines.append(f"DELETE {n_to_delete} chunk file(s) "
                     f"after each successful merge "
                     f"(applies to chunked groups only).")
    elif selected:
        lines.append("")
        lines.append("KEEP all chunk files (delete-after disabled).")

    other_count = len(concat_state["other_files"])
    skipped_count = sum(1 for k, (ok, _)
                        in concat_state["validations"].items() if not ok)
    deselected_groups = sum(1 for k, v
                            in concat_state["selections"].items()
                            if not v.get())
    n_singles_total = sum(1 for _, (ok, _)
                          in concat_state["single_files"].items() if ok)
    n_singles_nosplit = n_singles_total - len(single_jobs)
    lines.append("")
    lines.append("LEAVE UNTOUCHED:")
    lines.append(f"  - {other_count} non-video file(s)")
    lines.append(f"  - {skipped_count} unmergeable group(s)")
    if deselected_groups:
        lines.append(f"  - {deselected_groups} group(s) you deselected")
    if n_singles_nosplit:
        lines.append(f"  - {n_singles_nosplit} single file(s) with "
                     f"'No split' (default; nothing to do)")

    if not messagebox.askyesno("Confirm plan",
                               "\n".join(lines) + "\n\nProceed?"):
        return

    # Persist phase JSON sidecars BEFORE any ffmpeg runs. This way if anything
    # crashes (out-of-disk, USB hiccup, NVENC driver issue, etc) the typed
    # timestamps are not lost - they will be reloaded on the next refresh
    # so the user only needs to re-confirm, not re-enter.
    for key, _items, _info, phase_cfg, _phase_seconds in selected:
        concat_save_phase_json(folder, key, phase_cfg)
    for path, _info, cfg, _presult in single_jobs:
        concat_save_single_phase_json(path, cfg)
    log(f"\nSaved {len(selected) + len(single_jobs)} phase sidecar(s) "
        f"before starting work.")

    def task():
        failed = []
        completed = []  # list of dicts describing what to clean up
        total_jobs = len(selected) + len(single_jobs)
        concat_progress.start(total_jobs, "Processing")
        job_index = 0

        for key, items, info, phase_cfg, phase_seconds in selected:
            output_name = concat_output_name_for_group(key)
            output_path = os.path.join(folder, output_name)

            concat_progress.step(job_index, total_jobs, key)
            log(f"\n>>> [chunked] {key}")
            log(f"    merging -> {output_name} ({info['n_chunks']} chunks, "
                f"{humansize(info['total_size_bytes'])})")
            ok = concat_run(items, output_path)
            if not ok:
                failed.append(f"{key} (full merge)")
                job_index += 1
                continue

            mode = phase_cfg["mode"]
            phase_outputs = []
            phase_failed = False

            if mode in ("Phase 1 only", "Phase 1 + Phase 2"):
                p1s, p1e = phase_seconds["p1"]
                p1_path = os.path.join(folder,
                                       concat_phase_output_name(key, "phase1"))
                log(f"    trimming phase1 [{hms(p1s)} - {hms(p1e)}] "
                    f"-> {os.path.basename(p1_path)}")
                if concat_trim_phase(output_path, p1_path, p1s, p1e):
                    phase_outputs.append(p1_path)
                else:
                    phase_failed = True

            if mode == "Phase 1 + Phase 2" and not phase_failed:
                p1s, p1e = phase_seconds["p1"]
                p2s, p2e = phase_seconds["p2"]
                # Fragment between phase 1 end and phase 2 start.
                # Always produced (never optional) when there is a gap.
                if p2s > p1e:
                    frag_path = os.path.join(
                        folder, concat_phase_output_name(key, "fragment"))
                    log(f"    trimming fragment [{hms(p1e)} - {hms(p2s)}] "
                        f"-> {os.path.basename(frag_path)}")
                    if concat_trim_phase(output_path, frag_path, p1e, p2s):
                        phase_outputs.append(frag_path)
                    else:
                        phase_failed = True

            if mode == "Phase 1 + Phase 2" and not phase_failed:
                p2s, p2e = phase_seconds["p2"]
                p2_path = os.path.join(folder,
                                       concat_phase_output_name(key, "phase2"))
                log(f"    trimming phase2 [{hms(p2s)} - {hms(p2e)}] "
                    f"-> {os.path.basename(p2_path)}")
                if concat_trim_phase(output_path, p2_path, p2s, p2e):
                    phase_outputs.append(p2_path)
                else:
                    phase_failed = True

            if phase_failed:
                failed.append(f"{key} (phase trim)")
                job_index += 1
                continue

            out_size = os.path.getsize(output_path)
            log(f"    merge OK ({humansize(out_size)})")
            for pp in phase_outputs:
                log(f"    phase clip OK: {os.path.basename(pp)} "
                    f"({humansize(os.path.getsize(pp))})")

            if phase_outputs and mode != "No split":
                try:
                    os.remove(output_path)
                    log(f"    removed intermediate full merge: "
                        f"{output_name}")
                except OSError as e:
                    log(f"    could not remove full merge "
                        f"{output_name}: {e}")

            completed.append({
                "key": key,
                "items": items,
                "full_merge_path": output_path,
                "phase_outputs": phase_outputs,
                "mode": mode,
            })
            job_index += 1

        # Process single files
        for path, info, cfg, presult in single_jobs:
            concat_progress.step(job_index, total_jobs, os.path.basename(path))
            log(f"\n>>> [single] {os.path.basename(path)}")
            mode = cfg["mode"]
            phase_failed = False

            if mode in ("Phase 1 only", "Phase 1 + Phase 2"):
                p1s, p1e = presult["p1"]
                p1_path = concat_single_phase_output_name(path, "phase1")
                log(f"    trimming phase1 [{hms(p1s)} - {hms(p1e)}] "
                    f"-> {os.path.basename(p1_path)}")
                if not concat_trim_phase(path, p1_path, p1s, p1e):
                    phase_failed = True

            if mode == "Phase 1 + Phase 2" and not phase_failed:
                p1s, p1e = presult["p1"]
                p2s, p2e = presult["p2"]
                # Fragment between phase 1 end and phase 2 start.
                if p2s > p1e:
                    frag_path = concat_single_phase_output_name(
                        path, "fragment")
                    log(f"    trimming fragment [{hms(p1e)} - {hms(p2s)}] "
                        f"-> {os.path.basename(frag_path)}")
                    if not concat_trim_phase(path, frag_path, p1e, p2s):
                        phase_failed = True

            if mode == "Phase 1 + Phase 2" and not phase_failed:
                p2s, p2e = presult["p2"]
                p2_path = concat_single_phase_output_name(path, "phase2")
                log(f"    trimming phase2 [{hms(p2s)} - {hms(p2e)}] "
                    f"-> {os.path.basename(p2_path)}")
                if not concat_trim_phase(path, p2_path, p2s, p2e):
                    phase_failed = True

            if phase_failed:
                failed.append(
                    f"{os.path.basename(path)} (single-file phase trim)")
                job_index += 1
                continue
            log(f"    OK")
            job_index += 1

        if delete_after and completed:
            log("\nDeleting chunks for successfully processed groups...")
            for entry in completed:
                if entry["mode"] == "No split":
                    final_paths = [entry["full_merge_path"]]
                else:
                    final_paths = entry["phase_outputs"]
                if not all(os.path.exists(p)
                           and os.path.getsize(p) >= 1024
                           for p in final_paths):
                    log(f"  skip delete for {entry['key']} "
                        f"(outputs suspect)")
                    continue
                for _, path in entry["items"]:
                    try:
                        os.remove(path)
                        log(f"  removed {os.path.basename(path)}")
                    except OSError as e:
                        log(f"  could not remove {path}: {e}")

        log("\nDone.")
        root.after(0, concat_refresh_plan)
        concat_progress.done(total_jobs, failed_count=len(failed))
        if failed:
            safe_messagebox("warning", "Some failed",
                            "Failed:\n" + "\n".join(failed))
        else:
            n_done = len(completed) + len(single_jobs)
            safe_messagebox(
                "info", "Done",
                f"Processed {n_done} item(s) successfully "
                f"({len(completed)} chunked groups, "
                f"{len(single_jobs)} single files).")

    run_in_thread(task, log)


# ---- Concat tab UI ----
concat_top = tk.Frame(concat_tab, background=PALETTE["bg"])
concat_top.pack(fill="x", pady=(14, 8), padx=14)
make_secondary_button(concat_top, "Browse folder...",
                      concat_browse_folder).pack(side="left", padx=(0, 8))
make_secondary_button(concat_top, "Re-scan",
                      concat_refresh_plan).pack(side="left")
concat_folder_label = tk.Label(concat_top, text="(no folder selected)",
                               foreground=PALETTE["text_faint"],
                               font=FONT_SMALL)
concat_folder_label.pack(side="left", padx=12)

tk.Label(concat_tab, text="Plan",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(pady=(8, 4), anchor="w", padx=14)

concat_canvas_frame = tk.Frame(
    concat_tab, background=PALETTE["panel"],
    highlightbackground=PALETTE["border"], highlightthickness=1)
concat_canvas_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
concat_canvas = tk.Canvas(concat_canvas_frame, height=380,
                          background=PALETTE["panel"],
                          highlightthickness=0)
concat_scrollbar = tk.Scrollbar(concat_canvas_frame, orient="vertical",
                                command=concat_canvas.yview)
concat_canvas.configure(yscrollcommand=concat_scrollbar.set)
concat_scrollbar.pack(side="right", fill="y")
concat_canvas.pack(side="left", fill="both", expand=True)
concat_plan_frame = tk.Frame(concat_canvas, background=PALETTE["panel"])
concat_canvas.create_window((0, 0), window=concat_plan_frame, anchor="nw")


def _on_concat_configure(_):
    concat_canvas.configure(scrollregion=concat_canvas.bbox("all"))


concat_plan_frame.bind("<Configure>", _on_concat_configure)


def _on_concat_mw(event):
    if notebook.index(notebook.select()) != 1:
        return
    concat_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


def _on_concat_b4(event):
    if notebook.index(notebook.select()) != 1:
        return
    concat_canvas.yview_scroll(-1, "units")


def _on_concat_b5(event):
    if notebook.index(notebook.select()) != 1:
        return
    concat_canvas.yview_scroll(1, "units")


concat_canvas.bind_all("<MouseWheel>", _on_concat_mw)
concat_canvas.bind_all("<Button-4>", _on_concat_b4)
concat_canvas.bind_all("<Button-5>", _on_concat_b5)

concat_status_label = tk.Label(concat_tab, text="",
                               foreground=PALETTE["text_faint"],
                               font=FONT_SMALL)
concat_status_label.pack(pady=(4, 6), padx=14, anchor="w")


def concat_select_all():
    for v in concat_state["selections"].values():
        v.set(True)
    for v in concat_state["single_selections"].values():
        v.set(True)


def concat_deselect_all():
    for v in concat_state["selections"].values():
        v.set(False)
    for v in concat_state["single_selections"].values():
        v.set(False)


concat_ctrl = tk.Frame(concat_tab, background=PALETTE["bg"])
concat_ctrl.pack(pady=(2, 14), padx=14, fill="x")
make_secondary_button(concat_ctrl, "Select all",
                      concat_select_all).pack(side="left", padx=(0, 6))
make_secondary_button(concat_ctrl, "Deselect all",
                      concat_deselect_all).pack(side="left", padx=(0, 14))
concat_delete_var = tk.BooleanVar(value=False)
tk.Checkbutton(concat_ctrl, text="Delete chunks after successful merge",
               variable=concat_delete_var).pack(side="left")
make_primary_button(concat_ctrl, "Review and process...",
                    concat_show_confirmation_and_merge
                    ).pack(side="right")

concat_progress = BatchProgress(concat_tab)


# ======================================================================
# TAB 3: FPS LIMITER
# (from fps_limiter.py)
# ======================================================================
fps_selected_files = []
fps_file_info = {}


def fps_probe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, check=True)
    info = json.loads(r.stdout)
    duration = float(info["format"]["duration"])
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    rfr = v.get("r_frame_rate", "30/1")
    num, den = rfr.split("/")
    fps_val = float(num) / float(den) if float(den) != 0 else 30.0
    return {
        "duration": duration,
        "width": int(v["width"]),
        "height": int(v["height"]),
        "codec": v.get("codec_name", "").lower(),
        "fps": fps_val,
    }


def fps_build_cmd(input_file, output_path, target_fps, src_codec,
                  use_nvenc, quality, nvenc_preset, cpu_preset, overwrite):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin",
           "-progress", "pipe:1", "-nostats"]
    cmd.append("-y" if overwrite else "-n")
    if use_nvenc and src_codec in NVDEC_DECODABLE:
        cmd.extend(["-hwaccel", "cuda"])
    cmd.extend(["-thread_queue_size", "1024",
                "-i", input_file,
                "-map", "0:v:0",
                "-an", "-dn", "-sn",
                "-vf", f"fps={target_fps},format=yuv420p"])
    if use_nvenc:
        cmd.extend([
            "-c:v", "h264_nvenc",
            "-preset", nvenc_preset,
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(quality),
            "-b:v", "0",
            "-rc-lookahead", "32",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
            "-aq-strength", "8",
            "-bf", "3",
            "-b_ref_mode", "middle",
            "-gpu", "0",
            "-pix_fmt", "yuv420p",
        ])
    else:
        cmd.extend([
            "-c:v", "libx264",
            "-preset", cpu_preset,
            "-crf", str(quality),
            "-pix_fmt", "yuv420p",
        ])
    cmd.extend(["-movflags", "+faststart", output_path])
    return cmd


def fps_select_videos():
    files = filedialog.askopenfilenames(filetypes=[
        ("Video files", "*.mov *.mp4 *.avi *.mkv"),
        ("All files", "*.*")])
    if not files:
        return
    fps_selected_files.clear()
    fps_selected_files.extend(files)
    fps_file_info.clear()

    fps_video_list.config(state="normal")
    fps_video_list.delete("1.0", tk.END)
    for f in fps_selected_files:
        try:
            info = fps_probe(f)
            fps_file_info[f] = info
            line = (f"{f}\n    {info['width']}x{info['height']} "
                    f"{info['codec']} {info['fps']:.2f}fps "
                    f"{info['duration']:.1f}s\n")
        except Exception as e:
            line = f"{f}\n    [probe failed: {e}]\n"
        fps_video_list.insert(tk.END, line)
    fps_video_list.config(state="disabled")
    fps_update_plan()


def fps_build_output_name(input_path, target_fps):
    base, ext = os.path.splitext(input_path)
    base = re.sub(r"_\d+(?:\.\d+)?fps$", "", base)
    fps_str = (f"{int(target_fps)}" if target_fps == int(target_fps)
               else f"{target_fps:g}")
    return f"{base}_{fps_str}fps.mp4"


def fps_update_plan(*_):
    if not fps_selected_files:
        fps_plan_label.config(text="(no files selected)",
                              fg=PALETTE["text_faint"])
        return
    try:
        target = float(fps_entry.get())
    except ValueError:
        fps_plan_label.config(text="(invalid fps target)",
                              fg=PALETTE["err"])
        return

    lines = []
    n_skip = 0
    for f in fps_selected_files:
        info = fps_file_info.get(f)
        if not info:
            continue
        src_fps = info["fps"]
        effective = min(target, src_fps)
        out_name = fps_build_output_name(f, effective)
        if effective < target:
            note = f" (capped, source is {src_fps:.2f}fps)"
        else:
            note = ""
        if effective >= src_fps:
            note += " — source already at/below target, would be no-op (skipped)"
            n_skip += 1
            continue
        lines.append(f"  {os.path.basename(f)}  →  "
                     f"{os.path.basename(out_name)}"
                     f"  ({src_fps:.2f} → {effective:.2f}fps){note}")

    if not lines:
        fps_plan_label.config(
            text=(f"All {len(fps_selected_files)} file(s) are already at or "
                  f"below target fps. Nothing to do."),
            fg=PALETTE["warn"])
    else:
        summary = (f"Will produce {len(lines)} file(s); "
                   f"{n_skip} skipped (already at/below target).")
        fps_plan_label.config(text=summary + "\n" + "\n".join(lines),
                              fg=PALETTE["text_dim"], justify="left")


def fps_process():
    if not fps_selected_files:
        messagebox.showerror("Error", "Select files first.")
        return
    try:
        target_fps = float(fps_entry.get())
        if target_fps <= 0:
            raise ValueError
    except ValueError:
        messagebox.showerror("Error",
                             "Target fps must be a positive number.")
        return

    use_nvenc = fps_encoder_var.get() == "NVENC (GPU)"
    quality = fps_quality_slider.get()
    overwrite = fps_overwrite_var.get()
    nvenc_preset = fps_nvenc_preset_var.get()
    cpu_preset = fps_cpu_preset_var.get()
    files = list(fps_selected_files)

    def task():
        failed = []
        skipped = []
        total = len(files)
        fps_progress.start(total, "Reducing fps")
        for i, f in enumerate(files):
            fps_progress.step(i, total, os.path.basename(f))
            info = fps_file_info.get(f)
            if not info:
                try:
                    info = fps_probe(f)
                    fps_file_info[f] = info
                except Exception as e:
                    failed.append(f"{f} [probe: {e}]")
                    continue
            src_fps = info["fps"]
            if target_fps >= src_fps:
                skipped.append(f"{os.path.basename(f)} "
                               f"(source {src_fps:.2f}fps)")
                continue
            out_path = fps_build_output_name(f, target_fps)
            log_path = os.path.splitext(out_path)[0] + "_fps.log"
            log(f"\n>>> {f}")
            log(f"    {info['codec']} {src_fps:.2f}fps -> "
                f"{target_fps:.2f}fps")
            if use_nvenc and info["codec"] not in NVDEC_DECODABLE:
                log(f"    note: '{info['codec']}' not NVDEC-decodable -> "
                    f"CPU decode + GPU encode")
            cmd = fps_build_cmd(
                input_file=f,
                output_path=out_path,
                target_fps=target_fps,
                src_codec=info["codec"],
                use_nvenc=use_nvenc,
                quality=quality,
                nvenc_preset=nvenc_preset,
                cpu_preset=cpu_preset,
                overwrite=overwrite,
            )
            log("Running: " + " ".join(shlex.quote(c) for c in cmd))
            try:
                rc = run_ffmpeg(cmd, log_path)
            except FileNotFoundError:
                safe_messagebox("error", "Error",
                                "ffmpeg not found on PATH.")
                return
            if rc != 0:
                failed.append(f"{f} [rc={rc}]")

        fps_progress.done(total, failed_count=len(failed))
        msg_parts = []
        if skipped:
            msg_parts.append("Skipped (already at/below target):\n  "
                             + "\n  ".join(skipped))
        if failed:
            msg_parts.append("Failed:\n  " + "\n  ".join(failed))
        if not msg_parts:
            safe_messagebox("info", "Done", "All files processed.")
        elif failed:
            safe_messagebox("warning", "Done with issues",
                            "\n\n".join(msg_parts))
        else:
            safe_messagebox("info", "Done", "\n\n".join(msg_parts))

    run_in_thread(task, log)


# ---- fps tab UI ----
tk.Label(fps_tab, text="Selected videos",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(pady=(14, 6), anchor="w", padx=14)
fps_video_list = tk.Text(
    fps_tab, width=110, height=7, state="disabled",
    font=FONT_MONO_SM,
    background=PALETTE["panel"], foreground=PALETTE["text"],
    relief="flat", borderwidth=0,
    highlightbackground=PALETTE["border"], highlightthickness=1,
    padx=10, pady=8,
)
fps_video_list.pack(padx=14, fill="x")
_fps_top = tk.Frame(fps_tab, background=PALETTE["bg"])
_fps_top.pack(pady=(8, 12), padx=14, anchor="w")
make_secondary_button(_fps_top, "Browse videos...",
                      fps_select_videos).pack(side="left")

_fps_target_row = tk.Frame(fps_tab, background=PALETTE["bg"])
_fps_target_row.pack(pady=(4, 8), padx=14, anchor="w")
tk.Label(_fps_target_row, text="Target fps",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(side="left", padx=(0, 8))
fps_entry = tk.Entry(_fps_target_row, width=8, font=FONT_UI)
fps_entry.insert(0, "5")
fps_entry.pack(side="left")
fps_entry.bind("<KeyRelease>", fps_update_plan)

fps_settings = tk.LabelFrame(fps_tab, text="  Encoder settings  ",
                             padx=12, pady=10,
                             background=PALETTE["bg"])
fps_settings.pack(fill="x", padx=14, pady=(8, 10))

tk.Label(fps_settings, text="Encoder:",
         foreground=PALETTE["text_dim"]).grid(row=0, column=0,
                                              sticky="e", padx=4)
fps_encoder_var = tk.StringVar(value="NVENC (GPU)")
tk.OptionMenu(fps_settings, fps_encoder_var,
              "CPU", "NVENC (GPU)").grid(row=0, column=1,
                                          sticky="w", padx=4)

tk.Label(fps_settings, text="Quality (CQ/CRF, lower=better):",
         foreground=PALETTE["text_dim"]
         ).grid(row=0, column=2, sticky="e", padx=4)
fps_quality_slider = tk.Scale(fps_settings, from_=18, to=28,
                              orient="horizontal", length=180,
                              troughcolor=PALETTE["panel"],
                              activebackground=PALETTE["accent"],
                              highlightthickness=0,
                              background=PALETTE["bg"])
fps_quality_slider.set(20)
fps_quality_slider.grid(row=0, column=3, sticky="w", padx=4)

tk.Label(fps_settings, text="NVENC preset:",
         foreground=PALETTE["text_dim"]
         ).grid(row=1, column=0, sticky="e", padx=4)
fps_nvenc_preset_var = tk.StringVar(value="p5")
tk.OptionMenu(fps_settings, fps_nvenc_preset_var,
              "p1", "p2", "p3", "p4", "p5", "p6", "p7"
              ).grid(row=1, column=1, sticky="w", padx=4)

tk.Label(fps_settings, text="CPU preset:",
         foreground=PALETTE["text_dim"]
         ).grid(row=1, column=2, sticky="e", padx=4)
fps_cpu_preset_var = tk.StringVar(value="medium")
tk.OptionMenu(fps_settings, fps_cpu_preset_var,
              "ultrafast", "superfast", "veryfast", "faster",
              "fast", "medium", "slow", "slower", "veryslow"
              ).grid(row=1, column=3, sticky="w", padx=4)

fps_overwrite_var = tk.BooleanVar(value=True)
tk.Checkbutton(fps_settings, text="Overwrite existing output files",
               variable=fps_overwrite_var
               ).grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

tk.Label(fps_tab, text="Plan",
         font=FONT_SECTION, foreground=PALETTE["text_dim"]
         ).pack(pady=(8, 4), anchor="w", padx=14)
fps_plan_label = tk.Label(fps_tab, text="(no files selected)",
                          foreground=PALETTE["text_faint"],
                          justify="left", anchor="w",
                          font=FONT_SMALL,
                          wraplength=1000)
fps_plan_label.pack(padx=14, fill="x")

make_primary_button(fps_tab, "Reduce fps",
                    fps_process).pack(pady=18)

fps_progress = BatchProgress(fps_tab)


# ======================================================================
# Done
# ======================================================================
log("Video Pipeline ready.")
log("Tabs: Encode / Split   ·   Phase split / Merge   ·   fps Limiter")
log("All long operations run in background threads; switch tabs anytime.\n")

root.mainloop()
