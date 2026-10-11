"""Prerequisite checks: ffmpeg/ffprobe, encoders, NVENC, PySide6, BRAW helper."""

import os
import sys

import pytest

import stylet
import stylet_core as core


def test_install_hint_per_platform():
    assert "apt install ffmpeg" in core.install_hint("linux")
    assert "brew install ffmpeg" in core.install_hint("darwin")
    assert "winget install Gyan.FFmpeg" in core.install_hint("win32")
    assert all("conda install -c conda-forge ffmpeg" in core.install_hint(p)
               for p in ("linux", "darwin", "win32"))


def test_require_ffmpeg_explains_what_to_do(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda t: None)
    assert core.missing_tools() == ["ffmpeg", "ffprobe"]
    with pytest.raises(RuntimeError, match="ffmpeg and ffprobe not found") as e:
        core.require_ffmpeg()
    assert "Install ffmpeg" in str(e.value)
    monkeypatch.setattr(core.shutil, "which", lambda t: "/usr/bin/" + t if t == "ffmpeg" else None)
    assert core.missing_tools() == ["ffprobe"]


def fake_run(encoders, nvenc_ok=True):
    def run(cmd, timeout=30):
        if "-version" in cmd:
            return 0, f"{cmd[0]} version 7.0 test\nmore"
        if "-encoders" in cmd:
            return 0, "\n".join(f" V....D {e:<18} desc" for e in encoders)
        return (0, "") if nvenc_ok else (1, "OpenEncodeSessionEx failed: unsupported device (2)")
    return run


def by_name(items):
    return {i["name"]: i for i in items}


def test_check_environment_all_good(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(core, "_run_text",
                        fake_run(["libx264", "libx265", "prores_ks", "ffv1", "h264_nvenc"]))
    d = by_name(core.check_environment())
    assert d["ffmpeg"]["ok"] and d["ffmpeg"]["detail"] == "ffmpeg version 7.0 test"
    assert all(d[f"encoder {e}"]["ok"] for e in ("libx264", "libx265", "prores_ks", "ffv1"))
    assert d["NVENC (GPU)"]["ok"] and d["NVENC (GPU)"]["detail"] == "works"


def test_nvenc_listed_but_unusable_is_optional_and_explained(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(core, "_run_text", fake_run(["libx264", "h264_nvenc"], nvenc_ok=False))
    d = by_name(core.check_environment())
    nv = d["NVENC (GPU)"]
    assert not nv["ok"] and not nv["required"] and "unsupported device" in nv["detail"]
    assert "driver" in nv["hint"]
    assert not d["encoder libx265"]["ok"] and not d["encoder libx265"]["required"]
    assert d["encoder libx264"]["required"]


def test_no_nvenc_in_this_build(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(core, "_run_text", fake_run(["libx264"]))
    assert "no NVENC" in by_name(core.check_environment())["NVENC (GPU)"]["detail"]


def test_missing_ffmpeg_is_reported_with_the_install_hint(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda t: None)
    items = core.check_environment()
    d = by_name(items)
    assert not d["ffmpeg"]["ok"] and d["ffmpeg"]["required"] and "Install ffmpeg" in d["ffmpeg"]["hint"]
    assert "encoder libx264" not in d                      # nothing to ask without ffmpeg
    text = core.format_check(items)
    assert "[MISSING] ffmpeg" in text and "Install ffmpeg" in text


def test_check_flag_exit_codes(monkeypatch, capsys):
    ok = [{"name": "ffmpeg", "ok": True, "required": True, "detail": "x", "hint": ""}]
    bad = [{"name": "ffmpeg", "ok": False, "required": True, "detail": "x", "hint": "h"}]
    monkeypatch.setattr(core, "check_environment", lambda: ok)
    assert stylet.main(["--check"]) == 0
    monkeypatch.setattr(core, "check_environment", lambda: bad)
    assert stylet.main(["--check"]) == 1


def test_headless_without_ffmpeg_fails_early_and_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(core.shutil, "which", lambda t: None)
    job = tmp_path / "job.json"
    job.write_text("{}")
    assert stylet.main(["--headless", str(job)]) == 2
    assert "Install ffmpeg" in capsys.readouterr().err


def test_gui_falls_back_to_cpu_when_nvenc_is_not_usable(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import stylet_gui
    app = QApplication.instance() or QApplication([])
    items = [{"name": "NVENC (GPU)", "ok": False, "required": False,
              "detail": "listed but not usable", "hint": ""}]
    monkeypatch.setattr(core, "check_environment", lambda: items)
    win = stylet_gui.MainWindow(check_env=False)
    assert all(w.read()["encoder"] == "nvenc" for w in win.findChildren(stylet_gui.EncoderSettings))
    win.check_environment()
    app.processEvents()
    assert all(w.read()["encoder"] == "cpu" for w in win.findChildren(stylet_gui.EncoderSettings))
    win.close()
