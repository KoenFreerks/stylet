"""
stylet_gui.py -- the Qt (PySide6) front end of Stylet.

Three tabs: Encode / Split (crops, grids, with a frame to click on), Phase
split / Merge, fps Limiter. All logic is in stylet_core.py; this file only
collects choices and shows progress.

Qt widgets are touched only from the main thread. Worker threads never call a
widget: they emit Bus.call with a callable, which Qt queues to the main thread
[DD V.7].
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import traceback

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

import stylet_core as core

ENCODERS = {"NVENC (GPU)": "nvenc", "CPU": "cpu"}
CODECS = {"H.264": "h264", "H.265": "h265", "ProRes (CPU)": "prores",
          "FFV1 lossless (CPU)": "ffv1"}
PIX_CHOICES = ["auto"] + list(core.PIX_FMTS)
CONTAINERS = ["auto", "mp4", "mov", "mkv"]
NVENC_PRESETS = [f"p{i}" for i in range(1, 8)]
CPU_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
               "medium", "slow", "slower", "veryslow"]
VIDEO_FILTER = "Video files (*.mov *.mp4 *.avi *.mkv);;All files (*)"
ENCODE_FILTER = "Video files (*.mov *.mp4 *.avi *.mkv *.braw);;All files (*)"
BRAW_SCALES = {"Full resolution": 1, "Half": 2, "Quarter": 4, "Eighth": 8}
BRAW_PROFILES = {"ProRes 4444": 4, "ProRes 4444 XQ": 5, "ProRes 422 HQ": 3}
MONO = QFont("Monospace", 9)
MONO.setStyleHint(QFont.Monospace)


class Bus(QObject):
    """Worker threads emit `call` with a callable; it runs on the main thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


BUS = Bus()


def ui(fn):
    BUS.call.emit(fn)


def background(task, log):
    def wrap():
        try:
            task()
        except Exception:
            log("EXCEPTION:\n" + traceback.format_exc())
    threading.Thread(target=wrap, daemon=True).start()


def combo(items, current=None):
    c = QComboBox()
    c.addItems(list(items))
    if current is not None:
        c.setCurrentText(current)
    return c


def hbox(*widgets, stretch=True):
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    if stretch:
        row.addStretch()
    return row


class Status(QLabel):
    def __init__(self):
        super().__init__("Idle")
        self.setStyleSheet("color: grey")

    def set(self, text, color="black"):
        ui(lambda: (self.setText(text), self.setStyleSheet(f"color: {color}")))


class FileList(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(MONO)
        self.setMaximumHeight(130)

    def show_lines(self, lines):
        self.setPlainText("\n".join(lines))


def run_ok(label):
    b = QPushButton(label)
    f = b.font(); f.setBold(True); b.setFont(f)
    return b


class EncoderSettings(QGroupBox):
    """Encoder, codec, pixel format, container. read() -> core settings dict."""

    def __init__(self, with_height):
        super().__init__("Encoder settings")
        self.with_height = with_height
        g = QGridLayout(self)
        self.encoder = combo(ENCODERS, "NVENC (GPU)")
        self.codec = combo(CODECS, "H.264")
        self.quality = QSpinBox(); self.quality.setRange(0, 51); self.quality.setValue(20)
        self.nvenc = combo(NVENC_PRESETS, "p5")
        self.cpu = combo(CPU_PRESETS, "medium")
        self.pix = combo(PIX_CHOICES, "auto")
        self.container = combo(CONTAINERS, "auto")
        self.height = combo(["2160", "1440", "1080", "720", "480"], "1080")
        self.overwrite = QCheckBox("Overwrite existing output files"); self.overwrite.setChecked(True)
        cells = [("Encoder:", self.encoder), ("Codec:", self.codec),
                 ("Quality (CQ/CRF, lower = better):", self.quality),
                 ("NVENC preset:", self.nvenc), ("CPU preset:", self.cpu),
                 ("Pixel format (auto = keep/8-bit):", self.pix), ("Container:", self.container)]
        if with_height:
            cells.append(("Output height ('No crop' only):", self.height))
        for i, (text, w) in enumerate(cells):
            g.addWidget(QLabel(text), i // 2, (i % 2) * 2, Qt.AlignRight)
            g.addWidget(w, i // 2, (i % 2) * 2 + 1, Qt.AlignLeft)
        g.addWidget(self.overwrite, (len(cells) + 1) // 2, 0, 1, 4)

    def prefer_cpu(self):
        self.encoder.setCurrentText("CPU")

    def read(self):
        pf, ct = self.pix.currentText(), self.container.currentText()
        s = {"encoder": ENCODERS[self.encoder.currentText()], "quality": self.quality.value(),
             "nvenc_preset": self.nvenc.currentText(), "cpu_preset": self.cpu.currentText(),
             "overwrite": self.overwrite.isChecked(), "codec": CODECS[self.codec.currentText()],
             "pix_fmt": None if pf == "auto" else pf, "container": None if ct == "auto" else ct}
        if self.with_height:
            s["target_h"] = int(self.height.currentText())
        return s


# ===========================================================================
# Crop / layout dialog
# ===========================================================================
class FrameView(QWidget):
    """A frame with overlay items in source pixels; a click reports source px."""
    clicked = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(800, 450)
        self.pix, self.src, self.items = None, (1, 1), []

    def set_frame(self, pix, src_w, src_h):
        self.pix, self.src = pix, (src_w, src_h)
        self.update()

    def _geom(self):
        sw, sh = self.src
        sf = min(self.width() / sw, self.height() / sh)
        return sf, (self.width() - sw * sf) / 2, (self.height() - sh * sf) / 2

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self.pix is None:
            return
        sf, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self.src[0] * sf, self.src[1] * sf), self.pix,
                     QRectF(self.pix.rect()))
        for kind, x, y, w, h, color, label in self.items:
            pen = QPen(QColor(color)); pen.setWidth(2)
            if kind == "vline":
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            if kind == "rect":
                p.drawRect(QRectF(ox + x * sf, oy + y * sf, w * sf, h * sf))
            else:
                p.drawLine(QPointF(ox + x * sf, oy), QPointF(ox + x * sf, oy + h * sf))
            if label:
                p.setFont(QFont("Arial", 11, QFont.Bold))
                p.drawText(QPointF(ox + (x + 6) * sf, oy + (y + 6) * sf + 14), label)

    def mousePressEvent(self, e):
        if self.pix is None:
            return
        sf, ox, oy = self._geom()
        x, y = (e.position().x() - ox) / sf, (e.position().y() - oy) / sf
        if 0 <= x < self.src[0] and 0 <= y < self.src[1]:
            self.clicked.emit(int(x), int(y))


LAYOUT_FIELDS = (("w", "w"), ("h", "h"), ("rows", "rows"), ("cols", "cols"),
                 ("cell_w", "cell w"), ("cell_h", "cell h"), ("x0", "x0"), ("y0", "y0"),
                 ("pitch_x", "pitch x"), ("pitch_y", "pitch y"),
                 ("out_w", "out w"), ("out_h", "out h"))


class CropDialog(QDialog):
    def __init__(self, parent, files, cfgs, log):
        super().__init__(parent)
        self.setWindowTitle("Configure crops")
        self.files, self.cfgs, self.log = files, cfgs, log
        self.i, self.quiet, self.alive = 0, False, True
        v = QVBoxLayout(self)
        self.title = QLabel(); self.title.setStyleSheet("color: grey")
        self.view = FrameView()
        self.info = QLabel()
        v.addWidget(self.title); v.addWidget(self.view, 1); v.addWidget(self.info)

        self.modes = {}
        bar = QHBoxLayout()
        for text, val in (("Two-arena split", "two_arena"), ("Single (click centre)", "single"),
                          ("No crop", "none"), ("Layout (box / grid)", "layout")):
            r = QRadioButton(text); self.modes[val] = r
            r.toggled.connect(self.on_change); bar.addWidget(r)
        self.left, self.right = QCheckBox("Left arena"), QCheckBox("Right arena")
        for c in (self.left, self.right):
            c.toggled.connect(self.on_change); bar.addWidget(c)
        bar.addStretch(); v.addLayout(bar)

        lay = QHBoxLayout()
        self.kinds = {"box": QRadioButton("Box (click = centre)"), "grid": QRadioButton("Grid")}
        self.fields = {}
        for r in self.kinds.values():
            r.toggled.connect(self.on_layout_change); lay.addWidget(r)
        for key, text in LAYOUT_FIELDS:
            e = QLineEdit(); e.setFixedWidth(52); e.textChanged.connect(self.on_layout_change)
            self.fields[key] = e
            lay.addWidget(QLabel(text)); lay.addWidget(e)
        lay.addStretch(); v.addLayout(lay)
        self.err = QLabel(); self.err.setStyleSheet("color: red")
        self.warn = QLabel(); self.warn.setStyleSheet("color: darkorange")
        v.addWidget(self.err); v.addWidget(self.warn)

        nav = QHBoxLayout()
        for text, fn in (("<< Prev", lambda: self.step(-1)), ("Next >>", lambda: self.step(1)),
                         ("Preview outputs", self.preview_outputs),
                         ("Apply mode to all", self.apply_all), ("Done", self.accept)):
            b = QPushButton(text); b.clicked.connect(fn); nav.addWidget(b)
        v.addLayout(nav)
        self.view.clicked.connect(self.on_click)
        self.finished.connect(lambda _: setattr(self, "alive", False))
        self.show_current()
        threading.Thread(target=self.audit_all, daemon=True).start()
        self.resize(1000, 760)

    def cur(self):
        return self.cfgs[self.files[self.i]]

    def info_text(self):
        c, a = self.cur(), self.cur()["audit"]
        i = c["info"]
        if a is None:
            drop, col = ("drops=auditing..." if c["audit_pending"] else "drops=?"), "grey"
        else:
            p = a["drop_pct"]
            drop = f"drops={a['drop_count']}/{a['expected_frames']} ({p:.2f}%) [{a['method']}]"
            col = "red" if p >= 1 else "darkorange" if p >= 0.1 else "purple" if p < 0 else "darkgreen"
        self.info.setStyleSheet(f"color: {col}")
        self.info.setText(f"[{self.i + 1}/{len(self.files)}]  {os.path.basename(self.files[self.i])}"
                          f"    {i['width']}x{i['height']} {i['codec']} {i['duration']:.1f}s "
                          f"{i['fps']:.2f}fps    {drop}")
        self.title.setText(self.files[self.i])

    def overlay(self):
        c = self.cur()
        w, h = c["info"]["width"], c["info"]["height"]
        items, self.err_text, self.warn_text = [], "", ""
        if c["mode"] == "two_arena":
            half = w // 2
            for x0, side in ((0, "left"), (half, "right")):
                on = c[f"arena_{side}"]
                items.append(("rect", x0, 0, half, h, "lime" if on else "gray",
                              side.upper() + ("" if on else " (skip)")))
        elif c["mode"] == "single":
            x, cw, ch = core.crop_box(w, h, "single", c["click_x"])
            items += [("rect", x, 0, cw, ch, "cyan", ""),
                      ("vline", c["click_x"], 0, 0, ch, "yellow", "")]
        elif c["mode"] == "layout":
            try:
                lay = core.layout_from_params(c["layout_params"])
                for r in core.resolve_layout(lay, w, h)[0]:
                    items.append(("rect", r["x"], r["y"], r["w"], r["h"], "cyan", r["name"]))
                self.warn_text = "\n".join(core.layout_warnings(lay, w, h)[:3])
            except (ValueError, KeyError) as e:
                self.err_text = str(e)
        else:
            items.append(("rect", 0, 0, w, h, "white", ""))
        self.view.items = items
        self.view.update()
        self.err.setText(self.err_text); self.warn.setText(self.warn_text)
        for k in ("left", "right"):
            getattr(self, k).setEnabled(c["mode"] == "two_arena")
        self.info_text()

    def show_current(self):
        c, path = self.cur(), self.files[self.i]
        self.quiet = True                           # sync widgets without writing back
        self.modes[c["mode"]].setChecked(True)
        self.left.setChecked(c["arena_left"]); self.right.setChecked(c["arena_right"])
        lp = c["layout_params"]
        self.kinds[lp["kind"]].setChecked(True)
        for k, e in self.fields.items():
            e.setText(str(lp.get(k, "")))
        self.quiet = False
        thumb = os.path.join(tempfile.gettempdir(), "stylet_thumb.png")
        try:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", str(c["info"]["duration"] / 2), "-i", path, "-frames:v", "1",
                            "-vf", "scale=1280:-2:flags=lanczos", thumb],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            QMessageBox.critical(self, "Thumbnail failed", (e.stderr or b"").decode(errors="replace"))
            return
        self.view.set_frame(QPixmap(thumb), c["info"]["width"], c["info"]["height"])
        self.overlay()

    def on_change(self, *_):
        if not self.quiet:
            c = self.cur()
            on = [k for k, r in self.modes.items() if r.isChecked()]
            if not on:                              # the old radio un-checking first
                return
            c["mode"] = on[0]
            c["arena_left"], c["arena_right"] = self.left.isChecked(), self.right.isChecked()
            self.overlay()

    def on_layout_change(self, *_):
        if not self.quiet:
            lp = self.cur()["layout_params"]
            lp["kind"] = "grid" if self.kinds["grid"].isChecked() else "box"
            lp.update({k: e.text().strip() for k, e in self.fields.items()})
            self.overlay()

    def on_click(self, x, y):
        c = self.cur()
        if c["mode"] == "single":
            c["click_x"] = x
        elif c["mode"] == "layout" and c["layout_params"]["kind"] == "box":
            c["layout_params"]["cx"], c["layout_params"]["cy"] = x, y
        else:
            return
        self.overlay()

    def step(self, d):
        if 0 <= self.i + d < len(self.files):
            self.i += d
            self.show_current()

    def apply_all(self):
        c = self.cur()
        for f in self.files:
            o = self.cfgs[f]
            o["mode"] = c["mode"]
            same = (o["info"]["width"], o["info"]["height"]) == (c["info"]["width"], c["info"]["height"])
            if c["mode"] == "two_arena":
                o["arena_left"], o["arena_right"] = c["arena_left"], c["arena_right"]
            if c["mode"] == "single" and o["info"]["width"] == c["info"]["width"]:
                o["click_x"] = c["click_x"]
            if c["mode"] == "layout" and same:
                o["layout_params"] = dict(c["layout_params"])
        QMessageBox.information(self, "Applied", "Mode applied to all files.")

    def preview_outputs(self):
        c, path = self.cur(), self.files[self.i]
        w, h = c["info"]["width"], c["info"]["height"]
        try:
            if c["mode"] == "layout":
                chains = core.layout_filter_chain(core.layout_from_params(c["layout_params"]), w, h)
            elif c["mode"] == "none":
                chains = [("full", "format=yuv420p")]
            else:
                chains = core.build_filter_chain(w, h, c["mode"], c["click_x"], 1080,
                                                 c["arena_left"], c["arena_right"])
        except (ValueError, KeyError) as e:
            QMessageBox.critical(self, "Layout error", str(e)); return
        dlg = QDialog(self); dlg.setWindowTitle("Output preview (real filters, mid-video frame)")
        g = QGridLayout(dlg)
        for i, (label, vf) in enumerate(chains):
            png = os.path.join(tempfile.gettempdir(), f"stylet_prev_{i}.png")
            try:
                subprocess.run(core.preview_cmd(path, vf, c["info"]["duration"] / 2, png, 360),
                               check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                QMessageBox.critical(self, "Preview failed", (e.stderr or b"").decode(errors="replace"))
                return
            img = QLabel(); img.setPixmap(QPixmap(png))
            g.addWidget(img, (i // 4) * 2, i % 4); g.addWidget(QLabel(label), (i // 4) * 2 + 1, i % 4)
        dlg.show()

    def audit_all(self):                            # background; UI via the bus
        for f in list(self.files):
            c = self.cfgs[f]
            if c["audit"] is None:
                try:
                    c["audit"] = core.audit_frame_drops(f, c["info"], log=self.log)
                except Exception as e:
                    self.log(f"    audit error on {f}: {e}")
                c["audit_pending"] = False
                ui(lambda: self.alive and self.overlay())


# ===========================================================================
# Tab 1: encode / split
# ===========================================================================
class EncodeTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log, self.files, self.cfgs, self.infos = log, [], {}, {}
        v = QVBoxLayout(self)
        self.list = FileList(); v.addWidget(self.list)
        browse, crops = QPushButton("Browse videos..."), QPushButton("Configure crops...")
        browse.clicked.connect(self.browse); crops.clicked.connect(self.crop_dialog)
        v.addLayout(hbox(browse, crops))
        braw = QGroupBox("BRAW ingest (BRAW clips are first decoded once to a ProRes intermediate)")
        bl = QHBoxLayout(braw)
        self.braw_scale = combo(BRAW_SCALES, "Full resolution")
        self.braw_profile = combo(BRAW_PROFILES, "ProRes 4444")
        self.braw_helper = QLineEdit(); self.braw_helper.setPlaceholderText("braw2pipe (blank = auto)")
        for w in (QLabel("Scale:"), self.braw_scale, QLabel("Intermediate:"), self.braw_profile,
                  QLabel("Helper:"), self.braw_helper):
            bl.addWidget(w)
        v.addWidget(braw)
        self.settings = EncoderSettings(with_height=True); v.addWidget(self.settings)
        self.time_mode = combo(["Full video", "Full video, chunked"])
        self.chunk = QLineEdit("30"); self.chunk.setFixedWidth(60)
        v.addLayout(hbox(self.time_mode, QLabel("Chunk length (min):"), self.chunk))
        go, save = run_ok("Encode videos"), QPushButton("Save job file...")
        go.clicked.connect(self.encode); save.clicked.connect(self.save_job)
        v.addLayout(hbox(go, save))
        self.status = Status(); v.addWidget(self.status); v.addStretch()

    def ingest_settings(self):
        return {"helper": self.braw_helper.text().strip() or None,
                "scale": BRAW_SCALES[self.braw_scale.currentText()],
                "profile": BRAW_PROFILES[self.braw_profile.currentText()], "overwrite": False}

    def describe(self, f, info):
        lines = [f, "    " + core.describe_source(info)]
        lines += [f"    [WARN] {w}" for w in core.source_warnings(info)]
        if core.is_braw(f):
            lines.append("    BRAW clip: must be decoded once to a ProRes intermediate before cropping")
        return lines

    def browse(self):
        chosen, _ = QFileDialog.getOpenFileNames(self, "Select videos", "", ENCODE_FILTER)
        if not chosen:
            return
        self.files.clear(); self.cfgs.clear(); self.infos.clear()
        self.list.show_lines(["Reading files..."])
        helper = self.ingest_settings()["helper"]

        def probe_all():                      # nothing is touched until it is shown
            lines, ok, raws = [], [], []
            for f in chosen:
                try:
                    info = self.infos[f] = core.probe(f, helper)
                except Exception as e:
                    lines += [f, f"    [cannot read: {e}]"]
                    continue
                lines += self.describe(f, info)
                (raws if core.is_braw(f) else ok).append(f)
            ui(lambda: self.loaded(lines, ok, raws))
        background(probe_all, self.log)

    def loaded(self, lines, ok, raws):
        self.files[:] = ok
        self.list.show_lines(lines)
        if raws:
            self.ask_ingest(raws, lines)

    def ask_ingest(self, raws, lines):
        s = self.ingest_settings()
        try:
            core.find_braw_helper(s["helper"])
        except RuntimeError as e:
            QMessageBox.critical(self, "BRAW helper missing", str(e)); return
        if QMessageBox.question(self, "BRAW ingest", f"Decode {len(raws)} BRAW clip(s) to ProRes "
                                "intermediates next to them (one slow pass each; existing matching "
                                "intermediates are reused)?") != QMessageBox.Yes:
            return

        def task():
            failed, shown = [], list(lines)
            for i, f in enumerate(raws):
                self.status.set(f"BRAW ingest {i} of {len(raws)} · {os.path.basename(f)}")
                out, bad = core.ingest_braw({"source": f}, s, self.log)
                failed += bad
                if bad:
                    continue
                try:
                    self.infos[out] = core.probe(out)
                    shown += ["", "-> intermediate:"] + self.describe(out, self.infos[out])
                except Exception as e:
                    shown += [out, f"    [cannot read: {e}]"]
                    continue
                ui(lambda o=out, sh=list(shown): (self.files.append(o), self.list.show_lines(sh)))
            self.status.set(f"BRAW ingest done · {len(raws) - len(failed)} ok, {len(failed)} failed",
                            "darkorange" if failed else "darkgreen")
            if failed:
                ui(lambda: QMessageBox.warning(self, "BRAW ingest", "\n".join(failed)))
        background(task, self.log)

    def ensure_cfg(self, path, for_dialog):
        if path not in self.cfgs:
            info = self.infos.get(path) or core.probe(path)
            w, h = info["width"], info["height"]
            self.cfgs[path] = {
                "mode": core.default_mode(w, h, for_dialog), "click_x": w // 2,
                "arena_left": True, "arena_right": True,
                "layout_params": {"kind": "box", "cx": w // 2, "cy": h // 2,
                                  "w": min(1024, w), "h": min(1080, h), "rows": 1, "cols": 2,
                                  "cell_w": "", "cell_h": "", "x0": 0, "y0": 0, "pitch_x": "",
                                  "pitch_y": "", "out_w": "", "out_h": ""},
                "info": info, "audit": None, "audit_pending": for_dialog}
        return self.cfgs[path]

    def entry_for(self, path):
        c = self.cfgs[path]
        if c["mode"] == "layout":
            head = {"source": path, "layout": core.layout_from_params(c["layout_params"])}
        else:
            head = {"source": path, "mode": c["mode"], "click_x": c["click_x"],
                    "arenas": [a for a in ("left", "right") if c[f"arena_{a}"]]}
        return {**head,
                "time_mode": "chunked" if self.time_mode.currentText() == "Full video, chunked"
                else "full",
                "chunk_minutes": float(self.chunk.text())}

    def crop_dialog(self):
        if not self.files:
            QMessageBox.critical(self, "Error", "Select files first."); return
        try:
            for f in self.files:
                self.ensure_cfg(f, for_dialog=True)
        except Exception as e:
            QMessageBox.critical(self, "Probe failed", str(e)); return
        CropDialog(self, self.files, self.cfgs, self.log).exec()

    def entries(self):
        for f in self.files:
            self.ensure_cfg(f, for_dialog=False)
        return [self.entry_for(f) for f in self.files]

    def encode(self):
        if not self.files:
            QMessageBox.critical(self, "Error", "Select files first."); return
        try:
            entries = self.entries()
            if self.time_mode.currentText() == "Full video, chunked" and float(self.chunk.text()) <= 0:
                raise ValueError
            for e in entries:                       # refuse a bad layout before any ffmpeg
                if "layout" in e:
                    i = self.cfgs[e["source"]]["info"]
                    core.layout_filter_chain(e["layout"], i["width"], i["height"])
        except ValueError as e:
            QMessageBox.critical(self, "Error", str(e) or "Chunk length must be positive minutes.")
            return
        except Exception as e:
            QMessageBox.critical(self, "Probe failed", str(e)); return
        settings = self.settings.read()
        try:
            core.resolve_output_options({**core.ENCODE_SETTINGS_DEFAULTS, **settings})
        except ValueError as e:
            QMessageBox.critical(self, "Output options", str(e)); return

        def task():
            failed = []
            for i, e in enumerate(entries):
                self.status.set(f"{i} of {len(entries)} · {os.path.basename(e['source'])}")
                failed += core.encode_file(e, settings, self.log)
            self.status.set(f"Done · {len(entries)} file(s), {len(failed)} failure(s)",
                            "darkorange" if failed else "darkgreen")
            msg = "\n".join(failed) or "All jobs completed."
            ui(lambda: (QMessageBox.warning if failed else QMessageBox.information)(
                self, "Done", msg))
        background(task, self.log)

    def save_job(self):
        if not self.files:
            QMessageBox.critical(self, "Error", "Select files first."); return
        path, _ = QFileDialog.getSaveFileName(self, "Save job file", "", "Job file (*.json)")
        if path:
            core.save_json(path, {"encode": self.entries(),
                                  "encode_settings": self.settings.read()}, self.log)
            self.log(f"job file saved: {path}  (run: python stylet.py "
                     f"--headless {path})")


# ===========================================================================
# Tab 2: chunk merge / phase split
# ===========================================================================
PHASE_KEYS = ("phase1_start", "phase1_end", "phase2_start", "phase2_end")


class EvenPhasesDialog(QDialog):
    """Fill the Phases list with N equal consecutive phases."""

    def __init__(self, parent, default_end=""):
        super().__init__(parent)
        self.setWindowTitle("Split evenly")
        g = QGridLayout(self)
        self.start, self.end = QLineEdit("00:00:00"), QLineEdit(default_end)
        self.n = QSpinBox(); self.n.setRange(1, 500); self.n.setValue(4)
        for i, (t, w) in enumerate((("From (hh:mm:ss):", self.start), ("To (hh:mm:ss):", self.end),
                                    ("Number of phases:", self.n))):
            g.addWidget(QLabel(t), i, 0); g.addWidget(w, i, 1)
        ok = QPushButton("Create"); ok.clicked.connect(self.accept)
        g.addWidget(ok, 3, 0, 1, 2)

    def phases(self):
        return core.even_phases(core.parse_hms(self.start.text()), core.parse_hms(self.end.text()),
                                self.n.value())


class PhaseRow(QWidget):
    """Mode + timestamps; auto-saved to the sidecar on every change, so typed
    times survive a crash [DD V.5]. "Phase 1 only" / "Phase 1 + Phase 2" use four
    fields; "Phases" takes any number of named phases (gaps become fragment1,
    fragment2, ... automatically and are always kept [DD V.4])."""

    def __init__(self, saved, sidecar, log, duration=None):
        super().__init__()
        self.sidecar, self.log, self.ready, self.duration = sidecar, log, False, duration
        col = QVBoxLayout(self); col.setContentsMargins(24, 0, 0, 4)
        row = QHBoxLayout(); col.addLayout(row)
        self.mode = combo(core.PHASE_MODES, saved.get("mode", "No split"))
        row.addWidget(QLabel("Phase split:")); row.addWidget(self.mode)
        self.ents, self.legacy = {}, []
        defaults = {"phase1_start": "00:00:00"}
        for lab, keys in (("P1", PHASE_KEYS[:2]), ("P2", PHASE_KEYS[2:])):
            lbl = QLabel(lab); row.addWidget(lbl); self.legacy.append(lbl)
            for k in keys:
                e = QLineEdit(saved.get(k, defaults.get(k, ""))); e.setFixedWidth(84)
                e.editingFinished.connect(self.save)
                self.ents[k] = e; row.addWidget(e); self.legacy.append(e)
        row.addStretch()
        # the general mode: a growing list of named phases
        self.multi = QWidget(); ml = QVBoxLayout(self.multi); ml.setContentsMargins(0, 0, 0, 0)
        self.list_l = QVBoxLayout(); ml.addLayout(self.list_l)
        self.items = []
        btns = QHBoxLayout()
        add, even = QPushButton("+ Add phase"), QPushButton("Split evenly...")
        add.clicked.connect(lambda: (self.add_item(), self.save()))
        even.clicked.connect(self.fill_even)
        btns.addWidget(add); btns.addWidget(even)
        note = QLabel("Name is optional (file: ..._phase2-name). Gaps between phases are saved as "
                      "fragment1, fragment2, ...; leave the last end blank for 'until the end'.")
        note.setStyleSheet("color: grey"); note.setWordWrap(True)
        btns.addStretch(); ml.addLayout(btns); ml.addWidget(note); col.addWidget(self.multi)
        for ph in saved.get("phases") or []:
            self.add_item(ph.get("name", ""), ph.get("start", ""), ph.get("end", ""))
        if saved.get("mode") == "Phases" and not self.items:
            self.add_item("", "00:00:00", "")
        self.mode.currentTextChanged.connect(lambda _: (self.enable(), self.save()))
        self.enable()                               # initial state without writing a sidecar
        self.ready = True

    def add_item(self, name="", start="", end=""):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        label = QLabel(); fields = []
        for text, width in ((name, 110), (start, 84), (end, 84)):
            e = QLineEdit(text); e.setFixedWidth(width); e.editingFinished.connect(self.save)
            fields.append(e)
        fields[0].setPlaceholderText("name (optional)")
        fields[1].setPlaceholderText("start"); fields[2].setPlaceholderText("end")
        rm = QPushButton("x"); rm.setFixedWidth(26)
        item = {"w": w, "label": label, "f": fields}
        rm.clicked.connect(lambda: self.remove_item(item))
        for x in (label, *fields, rm):
            h.addWidget(x)
        h.addStretch()
        self.items.append(item); self.list_l.addWidget(w)
        self.renumber()

    def remove_item(self, item):
        self.items.remove(item)
        item["w"].deleteLater()
        self.renumber(); self.save()

    def renumber(self):
        for i, it in enumerate(self.items, 1):
            it["label"].setText(f"Phase {i}")

    def fill_even(self):
        dlg = EvenPhasesDialog(self, core.hms(self.duration) if self.duration else "")
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            phases = dlg.phases()
        except ValueError as e:
            QMessageBox.critical(self, "Split evenly", str(e)); return
        for it in list(self.items):
            self.items.remove(it); it["w"].deleteLater()
        for ph in phases:
            self.add_item(ph["name"], ph["start"], ph["end"])
        self.save()

    def enable(self):
        m = self.mode.currentText()
        for k, e in self.ents.items():
            e.setEnabled(m == "Phase 1 + Phase 2" or (m == "Phase 1 only" and k.startswith("phase1")))
        for w in self.legacy:
            w.setVisible(m != "Phases")
        self.multi.setVisible(m == "Phases")

    def read(self):
        return {"mode": self.mode.currentText(),
                **{k: e.text().strip() for k, e in self.ents.items()},
                "phases": [{"name": it["f"][0].text().strip(), "start": it["f"][1].text().strip(),
                            "end": it["f"][2].text().strip()} for it in self.items]}

    def save(self):
        if self.ready:
            core.save_json(self.sidecar, self.read(), self.log)


class PhaseTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log, self.folder, self.rows = log, None, []
        v = QVBoxLayout(self)
        browse, rescan = QPushButton("Browse folder..."), QPushButton("Re-scan")
        browse.clicked.connect(self.browse); rescan.clicked.connect(self.scan)
        self.folder_lbl = QLabel("(no folder selected)"); self.folder_lbl.setStyleSheet("color: grey")
        v.addLayout(hbox(browse, rescan, self.folder_lbl))
        self.plan = QWidget(); self.plan_l = QVBoxLayout(self.plan); self.plan_l.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.plan)
        v.addWidget(scroll, 1)
        self.summary = QLabel(); self.summary.setStyleSheet("color: grey"); v.addWidget(self.summary)
        sel_all, sel_none = QPushButton("Select all"), QPushButton("Deselect all")
        sel_all.clicked.connect(lambda: self.select_all(True))
        sel_none.clicked.connect(lambda: self.select_all(False))
        self.delete_chunks = QCheckBox("Delete chunks after successful merge")   # unchecked [V.9]
        go = run_ok("Review and process...")
        go.clicked.connect(self.process)
        v.addLayout(hbox(sel_all, sel_none, self.delete_chunks, go))
        self.status = Status(); v.addWidget(self.status)

    def clear(self):
        while self.plan_l.count() > 1:
            item = self.plan_l.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows.clear()

    def add_box(self, text, color, saved, sidecar, duration=None):
        box = QFrame(); box.setFrameShape(QFrame.StyledPanel)
        bl = QVBoxLayout(box)
        sel = QCheckBox(text); sel.setStyleSheet(f"color: {color}")      # default unchecked [V.9]
        row = PhaseRow(saved, sidecar, self.log, duration)
        bl.addWidget(sel); bl.addWidget(row)
        self.plan_l.insertWidget(self.plan_l.count() - 1, box)
        return sel, row

    def scan(self):
        if not self.folder:
            return
        self.clear()
        self.summary.setText("Scanning...")
        QApplication.processEvents()
        groups, singles, others = core.find_groups_and_others(self.folder)
        bad = []
        for key, items in sorted(groups.items()):
            ok, info = core.validate_group(items)
            if not ok:
                bad.append(f"{key}: {info['error']}")
                continue
            exists = os.path.exists(os.path.join(self.folder, core.merged_name(key)))
            sidecar = core.group_phases_json(self.folder, key)
            sel, row = self.add_box(
                f"{info['n_chunks']} chunks -> {core.merged_name(key)}"
                f"{'  (output exists, will overwrite)' if exists else ''}   "
                f"{info['codec']} {info['width']}x{info['height']} @ {info['fps']}, "
                f"{core.humansize(info['total_size_bytes'])}, {core.hms(info['merged_duration_sec'])}",
                "darkorange" if exists else "", core.load_json(sidecar), sidecar,
                info["merged_duration_sec"])
            self.rows.append(dict(kind="group", key=key, items=items, info=info, sel=sel,
                                  duration=info["merged_duration_sec"], read=row.read))
        for path in singles:
            try:
                info = core.probe(path)
            except Exception as e:
                bad.append(f"{os.path.basename(path)}: probe failed: {e}")
                continue
            sidecar = core.single_phases_json(path)
            sel, row = self.add_box(
                f"{os.path.basename(path)}   {info['codec']} {info['width']}x{info['height']} @ "
                f"{info['r_frame_rate']}, {core.humansize(info['size_bytes'])}, "
                f"{core.hms(info['duration'] or 0)}", "", core.load_json(sidecar), sidecar,
                info["duration"])
            self.rows.append(dict(kind="single", path=path, info=info, sel=sel,
                                  duration=info["duration"], read=row.read))
        for line in bad:
            lab = QLabel("SKIPPED  " + line); lab.setStyleSheet("color: red")
            self.plan_l.insertWidget(self.plan_l.count() - 1, lab)
        n_g = sum(r["kind"] == "group" for r in self.rows)
        self.summary.setText(f"{n_g} chunk group(s), {len(self.rows) - n_g} single file(s), "
                             f"{len(bad)} skipped with problems, {len(others)} other file(s) left untouched")

    def browse(self):
        f = QFileDialog.getExistingDirectory(self, "Select folder with chunked or single videos")
        if f:
            self.folder = f
            self.folder_lbl.setText(f); self.folder_lbl.setStyleSheet("")
            self.scan()

    def select_all(self, on):
        for r in self.rows:
            r["sel"].setChecked(on)

    def process(self):
        if not self.folder:
            QMessageBox.critical(self, "Error", "Select a folder first."); return
        jobs, problems = [], []
        for r in self.rows:
            if not r["sel"].isChecked():
                continue
            cfg = r["read"]()
            name = r.get("key") or os.path.basename(r["path"])
            if r["kind"] == "single" and cfg["mode"] == "No split":
                continue                                         # nothing to do
            ok, secs = core.validate_phase_config(cfg, r["duration"])
            if not ok:
                problems.append(f"  {name}: {secs}"); continue
            jobs.append((r, cfg, secs))
        if problems:
            QMessageBox.critical(self, "Phase config errors", "Fix these first:\n\n" + "\n".join(problems))
            return
        if not jobs:
            QMessageBox.information(self, "Nothing to do", "Nothing selected with work to do."); return
        lines = [f"In {self.folder}:"]
        for r, cfg, secs in jobs:
            if r["kind"] == "group":
                lines.append(f"\n  MERGE {r['key']} ({r['info']['n_chunks']} chunks)"
                             + ("  -> full merge kept" if cfg["mode"] == "No split"
                                else "  -> merge is an intermediate, deleted after"))
                name = lambda lab, k=r["key"]: core.group_phase_name(k, lab)
            else:
                lines.append(f"\n  SPLIT {os.path.basename(r['path'])} (source NOT deleted)")
                name = lambda lab, p=r["path"]: os.path.basename(core.single_phase_path(p, lab))
            for lab, a, b in core.phase_segments(cfg["mode"], secs):
                lines.append(f"    -> {name(lab)}  ({core.hms(a)} - {core.hms(b)})"
                             + ("  [always kept]" if lab == "fragment" else ""))
        delete = self.delete_chunks.isChecked()
        if delete and any(r["kind"] == "group" for r, _, _ in jobs):
            lines.append("\nDELETE the chunks of each group after a successful merge.")
        if QMessageBox.question(self, "Confirm plan", "\n".join(lines) + "\n\nProceed?") != \
                QMessageBox.Yes:
            return
        for r, cfg, _ in jobs:                  # every sidecar before any ffmpeg [V.5]
            core.save_json(core.group_phases_json(self.folder, r["key"]) if r["kind"] == "group"
                           else core.single_phases_json(r["path"]), cfg, self.log)
        folder = self.folder

        def task():
            failed = []
            for i, (r, cfg, _) in enumerate(jobs):
                self.status.set(f"{i} of {len(jobs)} · {r.get('key') or os.path.basename(r['path'])}")
                if r["kind"] == "group":
                    failed += core.process_group(folder, r["key"], r["items"], cfg, delete, self.log)
                else:
                    failed += core.process_single(r["path"], cfg, self.log)
            self.status.set(f"Done · {len(jobs)} item(s), {len(failed)} failure(s)",
                            "darkorange" if failed else "darkgreen")
            ui(self.scan)
            msg = "Failed:\n" + "\n".join(failed) if failed else f"Processed {len(jobs)} item(s)."
            ui(lambda: (QMessageBox.warning if failed else QMessageBox.information)(self, "Done", msg))
        background(task, self.log)


# ===========================================================================
# Tab 3: fps limiter
# ===========================================================================
class FpsTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log, self.files, self.infos = log, [], {}
        v = QVBoxLayout(self)
        self.list = FileList(); v.addWidget(self.list)
        browse = QPushButton("Browse videos..."); browse.clicked.connect(self.browse)
        v.addLayout(hbox(browse))
        self.target = QLineEdit("5"); self.target.setFixedWidth(70)
        self.target.textChanged.connect(self.update_plan)
        v.addLayout(hbox(QLabel("Target fps"), self.target))
        self.settings = EncoderSettings(with_height=False); v.addWidget(self.settings)
        self.plan = QLabel("(no files selected)"); self.plan.setStyleSheet("color: grey")
        v.addWidget(self.plan)
        go = run_ok("Reduce fps"); go.clicked.connect(self.run)
        v.addWidget(go, 0, Qt.AlignHCenter)
        self.status = Status(); v.addWidget(self.status); v.addStretch()

    def browse(self):
        chosen, _ = QFileDialog.getOpenFileNames(self, "Select videos", "", VIDEO_FILTER)
        if not chosen:
            return
        self.files[:] = chosen
        self.infos.clear()
        lines = []
        for f in chosen:
            try:
                i = self.infos[f] = core.probe(f)
                lines += [f, "    " + core.describe_source(i)]
                lines += [f"    [WARN] {w}" for w in core.source_warnings(i)]
            except Exception as e:
                lines.append(f"{f}\n    [probe failed: {e}]")
        self.list.show_lines(lines)
        self.update_plan()

    def update_plan(self, *_):
        if not self.files:
            return
        try:
            t = float(self.target.text())
            assert t > 0
        except (ValueError, AssertionError):
            self.plan.setText("(invalid fps target)"); self.plan.setStyleSheet("color: red"); return
        todo = [f"  {os.path.basename(f)} -> {os.path.basename(core.fps_output_name(f, t))}"
                f"  ({i['fps']:.2f} -> {t:.2f}fps)" for f, i in self.infos.items() if t < i["fps"]]
        self.plan.setStyleSheet("")
        self.plan.setText(f"Will produce {len(todo)} file(s); {len(self.infos) - len(todo)} skipped "
                          f"(already at/below target).\n" + "\n".join(todo))

    def run(self):
        if not self.files:
            QMessageBox.critical(self, "Error", "Select files first."); return
        try:
            t = float(self.target.text())
            assert t > 0
        except (ValueError, AssertionError):
            QMessageBox.critical(self, "Error", "Target fps must be a positive number."); return
        s, todo = self.settings.read(), list(self.files)
        try:
            core.resolve_output_options({**core.FPS_SETTINGS_DEFAULTS, **s})
        except ValueError as e:
            QMessageBox.critical(self, "Output options", str(e)); return

        def task():
            res = {"ok": [], "skipped": [], "failed": []}
            for i, f in enumerate(todo):
                self.status.set(f"{i} of {len(todo)} · {os.path.basename(f)}")
                status, msg = core.reduce_fps(f, t, s, self.log, info=self.infos.get(f))
                res[status].append(msg)
            self.status.set(f"Done · {len(res['ok'])} made, {len(res['skipped'])} skipped, "
                            f"{len(res['failed'])} failed", "darkorange" if res["failed"] else "darkgreen")
            parts = [f"{k.capitalize()}:\n  " + "\n  ".join(v) for k, v in res.items()
                     if k != "ok" and v]
            msg = "\n\n".join(parts) or "All files processed."
            ui(lambda: (QMessageBox.warning if res["failed"] else QMessageBox.information)(
                self, "Done", msg))
        background(task, self.log)


# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self, check_env=True):
        super().__init__()
        self.setWindowTitle("Stylet")
        self.resize(1100, 900)
        central = QWidget(); v = QVBoxLayout(central); self.setCentralWidget(central)
        self.console = QPlainTextEdit(); self.console.setReadOnly(True)
        self.console.setFont(MONO); self.console.setMaximumBlockCount(5000)
        self.console.setMinimumHeight(180)
        tabs = QTabWidget()
        for name, cls in (("Encode / Split", EncodeTab), ("Phase split / Merge", PhaseTab),
                          ("fps Limiter", FpsTab)):
            tabs.addTab(cls(self.log), name)
        v.addWidget(tabs, 1); v.addWidget(self.console)
        self.log("Stylet ready. Tabs: Encode / Split · Phase split / Merge · fps Limiter")
        self.log("Long operations run in background threads; each encode and fps step writes a "
                 "job file you can re-run with --headless.")
        if check_env:
            background(self.check_environment, self.log)

    def check_environment(self):
        """Report what this machine can do; fall back to the CPU encoder when NVENC
        is not usable, so a computer without an NVIDIA GPU works out of the box."""
        items = core.check_environment()
        self.log("Environment check:\n" + core.format_check(items))
        nv = next((i for i in items if i["name"].startswith("NVENC")), None)
        if nv and not nv["ok"]:
            def switch():
                for w in self.findChildren(EncoderSettings):
                    w.prefer_cpu()
                self.log("NVENC is not usable here: the encoder is set to CPU.")
            ui(switch)

    def log(self, msg):
        ui(lambda: self.console.appendPlainText(msg))


def run_gui():
    app = QApplication.instance() or QApplication(sys.argv)
    if core.missing_tools():
        try:
            core.require_ffmpeg()
        except RuntimeError as e:
            QMessageBox.critical(None, "Stylet needs ffmpeg", str(e))
            return 2
    win = MainWindow()
    win.show()
    return app.exec()
