"""Command line interface."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .core import (AMCCancelled, META_FIELDS, Options, _FIELD_CN, collect_inputs,
                   convert_file, fmt_size, normalize_meta_key)


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
            "  python -m amc --gui\n"
            "  python -m amc \"D:\\Downloads\\时间煮雨.flac\"\n"
            "  python -m amc D:\\Music --recursive --out D:\\Out --target-mb 50\n"
        ),
    )
    p.add_argument("inputs", nargs="*", help="音频文件或文件夹（可多个）")
    p.add_argument("--out", "-o", help="输出文件夹，默认与源文件同目录")
    p.add_argument("--target-mb", type=float, default=50.0,
                   help="单曲大小上限 MB，默认 50；填 0 表示不限制")
    p.add_argument("--keep-name", action="store_true",
                   help="保留原文件名（默认用「歌手 - 标题」重命名）")
    p.add_argument("--force-alac", action="store_true",
                   help="强制输出 ALAC 无损，即使源已是 MP3/AAC（等价 --format alac）")
    p.add_argument("--format", dest="out_format", default="auto",
                   choices=("auto", "alac", "aac", "mp3"),
                   help="输出格式：auto=优先无损（默认）；alac=强制 ALAC；"
                        "aac=强制 AAC(m4a)；mp3=强制 MP3(上限 320kbps)")
    p.add_argument("--meta", action="append", metavar="KEY=值", default=[],
                   help="手动补全缺失信息（只填文件里没有的字段，内嵌标签不动），"
                        "可重复。KEY: title/artist/album/album_artist/date/genre/"
                        "track/disc。例: --meta artist=周杰伦 --meta album=叶惠美")
    p.add_argument("--recursive", "-r", action="store_true",
                   help="递归扫描子文件夹（默认只处理文件夹第一层）")
    p.add_argument("--lyrics-encoding", help="强制外挂歌词编码，如 gbk / big5 / utf-8")
    p.add_argument("--no-overwrite", dest="overwrite", action="store_false",
                   help="输出文件已存在时跳过，而不是原地替换")
    p.add_argument("--no-tag-repair", dest="repair_tags", action="store_false",
                   help="关闭标签乱码自动修复（修复猜错时使用）")
    p.add_argument("--no-name-parse", dest="parse_names", action="store_false",
                   help="关闭「文件名补全」：标签缺失时不再从文件名猜歌名/歌手/专辑")
    p.add_argument("--quiet", "-q", action="store_true", help="只输出每个文件的结果行")
    p.add_argument("--gui", action="store_true", help="打开图形界面")
    p.add_argument("--check", action="store_true", help="只检查环境（ffmpeg 是否可用）")
    p.add_argument("--version", "-V", action="version",
                   version=f"AMC (Apple Music 格式转换器) {__version__}")
    return p


def _install_sigint(cancel: threading.Event) -> None:
    """Turn Ctrl+C into a cancellable stop so ffmpeg does not keep running."""
    def handler(signum, frame):        # noqa: ARG001
        if cancel.is_set():
            raise KeyboardInterrupt
        print("\n正在停止…（再按一次 Ctrl+C 可强制退出）", file=sys.stderr, flush=True)
        cancel.set()

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass          # not the main thread, or a platform without SIGINT


def run_cli(args) -> int:
    targets = collect_inputs([Path(x) for x in args.inputs], recursive=args.recursive)
    if not targets:
        print("没有找到可处理的音频文件。", file=sys.stderr)
        if not args.recursive and any(Path(x).is_dir() for x in args.inputs):
            print("提示: 音频可能在子文件夹里，加 --recursive 可以递归扫描。",
                  file=sys.stderr)
        return 2

    meta: dict[str, str] = {}
    for item in args.meta:
        key, sep, value = item.partition("=")
        canonical = normalize_meta_key(key.lower()) if sep else None
        if canonical is None:
            p_msg = ("--meta 需要 字段=值 形式，字段可以是 "
                     + "/".join(META_FIELDS) + " 或中文 "
                     + "/".join(_FIELD_CN.values()))
            print(f"参数错误: {p_msg}（收到: {item!r}）", file=sys.stderr)
            return 2
        meta[canonical] = value.strip()

    opts = Options(
        outdir=Path(args.out).expanduser() if args.out else None,
        target_mb=args.target_mb,
        keep_name=args.keep_name,
        force_alac=args.force_alac,
        out_format=args.out_format,
        lyrics_encoding=args.lyrics_encoding,
        overwrite=args.overwrite,
        repair_tags=args.repair_tags,
        parse_names=args.parse_names,
        meta=meta,
    )

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    cancel = threading.Event()
    _install_sigint(cancel)

    limit = "不限制" if opts.target_mb <= 0 else f"{opts.target_mb:g}MB"
    fmt_note = {"auto": "", "alac": "，强制 ALAC", "aac": "，强制 AAC",
                "mp3": "，强制 MP3"}.get(opts.out_format, "")
    print(f"共 {len(targets)} 个文件，单曲上限 {limit}"
          f"{'，覆盖已有输出' if opts.overwrite else '，跳过已存在的输出'}{fmt_note}",
          flush=True)

    ok = warn = fail = 0
    failures: list[tuple[Path, str]] = []
    cancelled = False

    for i, src in enumerate(targets, 1):
        if cancel.is_set():
            cancelled = True
            break
        log(f"[{i}/{len(targets)}] {src.name}")
        try:
            res = convert_file(src, opts, log, cancel=cancel)
        except AMCCancelled:
            cancelled = True
            print(f"  已取消（剩余 {len(targets) - i + 1} 个文件未处理）", file=sys.stderr)
            break
        except KeyboardInterrupt:
            cancelled = True
            print("\n已强制退出。", file=sys.stderr)
            break

        if not res.ok:
            fail += 1
            failures.append((src, res.error or "未知错误"))
            log(f"  ✗ 失败: {res.error}")
            continue

        flags = list(res.warnings)
        if flags:
            warn += 1
        log(f"  ✓ {res.dest.name}  {fmt_size(res.size)}  [{res.plan.label()}]")
        log(f"    歌词: {res.lyrics_from}   封面: {res.cover_from}"
            f"   信息: {res.meta_from}"
            + (f"   ⚠ {'; '.join(flags)}" if flags else ""))
        ok += 1

    print("")
    print(f"完成: 成功 {ok}，有警告 {warn}，失败 {fail}"
          + ("（已取消）" if cancelled else ""))
    if failures:
        print("失败明细:")
        for src, reason in failures:
            print(f"  - {src}: {reason}")

    if cancelled:
        return 130
    return 1 if fail else 0
