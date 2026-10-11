"""Qt smoke test: the window builds and the settings read back (offscreen)."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import stylet_core as core  # noqa: E402


def test_window_builds_and_settings_roundtrip():
    from PySide6.QtWidgets import QApplication
    import stylet_gui
    app = QApplication.instance() or QApplication([])
    win = stylet_gui.MainWindow(check_env=False)
    enc = win.findChildren(stylet_gui.EncodeTab)[0]
    s = enc.settings.read()
    assert s["encoder"] == "nvenc" and s["codec"] == "h264" and s["pix_fmt"] is None
    assert core.resolve_output_options({**core.ENCODE_SETTINGS_DEFAULTS, **s})["container"] == "mp4"
    enc.settings.codec.setCurrentText("ProRes (CPU)")
    assert enc.settings.read()["codec"] == "prores"
    assert len(win.findChildren(stylet_gui.PhaseTab)) == 1 and len(win.findChildren(stylet_gui.FpsTab)) == 1
    win.close()


def test_loading_a_video_shows_what_it_is_before_anything_runs(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QFileDialog
    import stylet_gui
    app = QApplication.instance() or QApplication([])
    info = {"width": 4096, "height": 2160, "codec": "prores", "profile": "HQ",
            "pix_fmt": "yuv422p10le", "bit_depth": 10, "chroma": "422", "fps": 23.976,
            "vfr": False, "duration": 12.5, "nb_frames_meta": 300, "size_bytes": 5_000_000,
            "timecode": "01:00:00:00", "r_frame_rate": "24000/1001"}
    f = str(tmp_path / "A006.mov")
    monkeypatch.setattr(core, "probe", lambda p, helper=None: info)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([f], ""))
    win = stylet_gui.MainWindow(check_env=False)
    enc = win.findChildren(stylet_gui.EncodeTab)[0]
    enc.browse()
    import time
    for _ in range(100):                       # the probe runs in a worker thread
        app.processEvents(); time.sleep(0.02)
        if enc.files:
            break
    text = enc.list.toPlainText()
    assert enc.files == [f] and "4096x2160" in text and "prores HQ" in text
    assert "yuv422p10le (10-bit 4:2:2)" in text and "23.976 fps" in text and "TC 01:00:00:00" in text
    assert enc.cfgs == {}                       # nothing configured or encoded yet
    win.close()
