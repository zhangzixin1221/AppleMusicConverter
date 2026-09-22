"""Entry point: ``python -m amc`` or the installed ``amc`` command."""

from __future__ import annotations

import os
import sys

from .cli import build_parser, run_cli
from .core import AMCError, ffmpeg, ffprobe
from .gui import run_gui


# --------------------------------------------------------------------------

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
        try:
            print("ffmpeg :", ffmpeg())
            print("ffprobe:", ffprobe())
            print("环境正常。")
            return 0
        except AMCError as exc:
            print("环境异常:", exc)
            return 1

    if args.gui or not args.inputs:
        return run_gui(args.inputs)
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
