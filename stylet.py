"""
stylet.py -- Stylet.

    python stylet.py                         # GUI
    python stylet.py --headless job.json     # no GUI: run a job file
    python stylet.py --headless job.json --dry-run   # print the commands

GUI tabs: Encode / Split (arena crop, with a thumbnail to click the crop
centre on), Phase split / Merge, fps Limiter. All logic is in stylet_core.py;
this file only collects choices and shows progress.

Every encode writes <name>.encode.json next to its outputs before ffmpeg
starts. That file is a valid job file: to redo a crop or a split, edit it and
run it with --headless. Phase timestamps live in <name>.phases.json as before.

The GUI is Qt (PySide6) and lives in stylet_gui.py; widgets are touched only from
the main thread [DD V.7].
"""

from __future__ import annotations

import argparse
import json
import sys

import stylet_core as core


def run_gui():
    from stylet_gui import run_gui as qt_gui          # Qt is imported only for the GUI
    return qt_gui()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stylet: crop, phase split and fps limit for analysis-ready video.")
    ap.add_argument("--headless", metavar="JOB.json",
                    help="run a job file without the GUI (an <name>.encode.json record is one)")
    ap.add_argument("--dry-run", action="store_true", help="with --headless: print commands only")
    ap.add_argument("--check", action="store_true",
                    help="check ffmpeg, encoders, NVENC, PySide6 and the BRAW helper, then exit")
    a = ap.parse_args(argv)
    if a.check:
        items = core.check_environment()
        print(core.format_check(items))
        return 0 if all(i["ok"] for i in items if i["required"]) else 1
    if not a.headless:
        return run_gui()
    try:
        core.require_ffmpeg()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with open(a.headless) as f:
        job = json.load(f)
    failed = core.run_job(job, dry_run=a.dry_run)
    for line in failed:
        print("FAILED:", line)
    print("done" if not failed else f"{len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
