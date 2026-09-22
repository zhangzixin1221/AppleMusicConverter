"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import AMCError, Options, collect_inputs, convert_file, fmt_size


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amc",
        description="把音频转换成可直接导入 Apple Music 的格式（内嵌封面 + 歌词）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python amc.py --gui\n"
            "  python amc.py \"D:\\Downloads\\时间煮雨.flac\"\n"
            "  python amc.py D:\\Music --out D:\\Out --target-mb 50 --recursive\n"
        ),
    )
    p.add_argument("inputs", nargs="*", help="音频文件或文件夹（可多个）")
    p.add_argument("--out", "-o", help="输出文件夹，默认与源文件同目录")
    p.add_argument("--target-mb", type=float, default=50.0,
                   help="单曲大小上限 MB，默认 50；填 0 表示不限制")
    p.add_argument("--keep-name", action="store_true",
                   help="保留原文件名（默认用「歌手 - 标题」重命名）")
    p.add_argument("--force-alac", action="store_true",
                   help="强制输出 ALAC 无损，即使源已是 MP3/AAC")
    p.add_argument("--recursive", action="store_true", help="递归扫描子文件夹")
    p.add_argument("--lyrics-encoding", help="强制外挂歌词编码，如 gbk / big5 / utf-8")
    p.add_argument("--gui", action="store_true", help="打开图形界面")
    p.add_argument("--check", action="store_true", help="只检查环境（ffmpeg 是否可用）")
    return p


def run_cli(args) -> int:
    targets = collect_inputs(args.inputs)
    if not targets:
        print("没有找到可处理的音频文件。", file=sys.stderr)
        return 2

    opts = Options(
        outdir=Path(args.out).expanduser() if args.out else None,
        target_mb=args.target_mb if args.target_mb > 0 else 1e6,
        keep_name=args.keep_name,
        force_alac=args.force_alac,
        lyrics_encoding=args.lyrics_encoding,
    )

    def log(msg: str) -> None:
        print(msg, flush=True)

    ok = warn = fail = 0
    for i, src in enumerate(targets, 1):
        log(f"[{i}/{len(targets)}] {src.name}")
        res = convert_file(src, opts, log)
        if not res.ok:
            fail += 1
            log(f"  ✗ 失败: {res.error}")
            continue
        flags = []
        if res.warnings:
            flags.extend(res.warnings)
        if flags:
            warn += 1
        log(f"  ✓ {res.dest.name}  {fmt_size(res.size)}  [{res.plan.label()}]")
        log(f"    歌词: {res.lyrics_from}   封面: {res.cover_from}"
            + (f"   ⚠ {'; '.join(flags)}" if flags else ""))
        ok += 1

    log("")
    log(f"完成: 成功 {ok}，有警告 {warn}，失败 {fail}")
    return 0 if fail == 0 else 1
