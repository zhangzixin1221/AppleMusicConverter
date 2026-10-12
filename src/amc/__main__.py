"""Entry point: ``python -m amc`` or the installed ``amc`` command."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from .cli import build_parser, run_cli
from .core import AMCError, ffmpeg, ffprobe
from .gui import run_gui


# --------------------------------------------------------------------------

def _check_environment() -> int:
    """Report everything the converter needs, so a broken install is obvious."""
    print(f"AMC       : {__version__}")
    print(f"Python    : {sys.version.split()[0]} ({sys.executable})")

    problems = 0
    try:
        import mutagen
        print(f"mutagen   : {mutagen.version_string}")
    except Exception as exc:                      # pragma: no cover - install issue
        problems += 1
        print(f"mutagen   : 不可用 ({exc}) -- 请运行 pip install mutagen")

    for label, getter in (("ffmpeg    ", ffmpeg), ("ffprobe   ", ffprobe)):
        try:
            print(f"{label}: {getter()}")
        except AMCError as exc:
            problems += 1
            print(f"{label}: 不可用 -- {exc}")

    try:
        with tempfile.TemporaryDirectory(prefix="amc_check_") as tmp:
            probe = Path(tmp) / "w.txt"
            probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        problems += 1
        print(f"临时目录  : 不可写 ({exc})")

    print("环境正常。" if not problems else f"发现 {problems} 个问题。")
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    # A --windowed build has no console attached, which leaves stdout/stderr as
    # None. Give them somewhere to go so logging and error paths still work.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.check:
        return _check_environment()

    if args.gui or not args.inputs:
        return run_gui(args.inputs)
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
