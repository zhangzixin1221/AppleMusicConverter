"""AMC core engine.

Converts arbitrary audio files into files that import cleanly into Apple Music
with an embedded cover and embedded plain-text lyrics, keeping the result under
a per-track size budget without throwing away quality unnecessarily.

Design notes
------------
* No shell is ever involved. Every external command is launched with an argv
  list via ``subprocess`` and its output is decoded explicitly as UTF-8. This is
  what keeps CJK filenames and tags from turning into mojibake on Windows.
* ffmpeg only ever touches *audio*. All text metadata, the cover and the lyrics
  are written afterwards with mutagen, which stores UTF-8 natively.
* Lyrics are embedded as plain text in the ``(c)lyr`` / ``USLT`` field. Apple
  Music renders that payload verbatim; a TTML payload is shown as raw XML.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

LOSSLESS_CODECS = {
    "flac", "alac", "ape", "wavpack", "tta", "tak",
    "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le",
    "pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_u8", "pcm_s8",
    "adpcm_ima_wav", "adpcm_ms",
    "dsd_lsbf", "dsd_msbf", "dsd_lsbf_planar", "dsd_msbf_planar",
}
APPLE_NATIVE = {"mp3", "aac", "alac"}          # Apple Music plays these as-is
AUDIO_EXTS = {
    ".flac", ".wav", ".wave", ".aiff", ".aif", ".aifc", ".ape", ".wv", ".tta",
    ".tak", ".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".asf", ".dsf", ".dff", ".mpc", ".caf", ".mka", ".ac3", ".mp2",
    ".w64", ".rf64", ".amr", ".au", ".snd",
}
COVER_STEMS = ("cover", "folder", "front", "album", "albumart", "thumb", "artwork")
LYRIC_STEMS = ("", ".lrc")
MAX_AAC_KBPS = 512
ALAC_RATIO = 0.62          # empirical ALAC payload / raw PCM ratio


class AMCError(RuntimeError):
    """Raised when a file cannot be converted."""


# --------------------------------------------------------------------------
# tool discovery / process helpers
# --------------------------------------------------------------------------

def _search_dirs() -> list[Path]:
    """Places ffmpeg might live, most specific first."""
    dirs: list[Path] = []
    # A frozen build does not ship ffmpeg, but the user may drop ffmpeg.exe next
    # to the .exe -- check there before falling back to the PATH lookup.
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass))
    dirs += [
        Path(r"C:\Windows"),
        Path(r"C:\ffmpeg\bin"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
    ]
    return dirs


def _find_tool(name: str) -> str:
    exe = shutil.which(name)
    if exe:
        return exe
    for base in _search_dirs():
        cand = base / f"{name}.exe"
        try:
            if cand.exists():
                return str(cand)
        except OSError:
            continue
    raise AMCError(
        f"找不到 {name}.exe。请安装 ffmpeg 并把它加入 PATH，"
        f"或把 ffmpeg.exe 放在程序所在目录下。"
    )


_FFMPEG: str | None = None
_FFPROBE: str | None = None


def ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _find_tool("ffmpeg")
    return _FFMPEG


def ffprobe() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = _find_tool("ffprobe")
    return _FFPROBE


def run(cmd: list[str]) -> tuple[str, str]:
    """Run an argv list, returning (stdout, stderr) decoded as UTF-8."""
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = (err or out).strip().splitlines()
        detail = " / ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise AMCError(f"{Path(cmd[0]).name} 执行失败: {detail}")
    return out, err


# --------------------------------------------------------------------------
# text repair / decoding
# --------------------------------------------------------------------------

_LATIN1_SUPP = re.compile(r"[\u0080-\u00ff]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _mojibake_score(s: str) -> int:
    return len(_CJK.findall(s)) * 2 - len(_LATIN1_SUPP.findall(s))


def repair_text(s: str) -> str:
    """Undo the classic 'UTF-8/GBK bytes decoded as Latin-1' mojibake.

    Common in Chinese ID3v2.3 tags written by old taggers. Only fires when the
    string actually contains Latin-1 supplement characters, so clean text is
    returned untouched.
    """
    if not s or not _LATIN1_SUPP.search(s):
        return s
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s
    best, best_score = s, _mojibake_score(s)
    for enc in ("gb18030", "big5", "shift_jis", "cp1251", "utf-8"):
        try:
            cand = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = _mojibake_score(cand)
        if score > best_score:
            best, best_score = cand, score
    return best


def decode_text(raw: bytes, forced: str | None = None) -> str:
    """Decode a lyrics/text file, working around unknown legacy encodings."""
    if forced:
        return raw.decode(forced, "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32")
    for enc in ("utf-8", "gb18030", "big5", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", "replace")


# --------------------------------------------------------------------------
# lyrics
# --------------------------------------------------------------------------

_LRC_META = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|au|kana):.*?\]\s*", re.I)
_LRC_TIME = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
_LRC_WORD = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_LRC_BG = re.compile(r"\[bg:.*?\]", re.I)


def lrc_to_plain(text: str) -> str:
    """Reduce LRC/SRT-ish lyrics to clean plain text lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in text.split("\n"):
        line = _LRC_META.sub("", raw)
        line = _LRC_WORD.sub("", line)
        line = _LRC_TIME.sub("", line)
        line = _LRC_BG.sub("", line)
        line = line.strip()
        if line and not re.fullmatch(r"[\s\-–—]*", line):
            lines.append(line)
    # collapse accidental duplicates that adjacent identical timestamps produce
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return "\n".join(out)


def _lyrics_from_probe_tags(tags: dict) -> str | None:
    for key, value in tags.items():
        if "lyric" in key.lower() and value and str(value).strip():
            return str(value)
    return None


def _lyrics_from_mutagen(path: Path) -> str | None:
    try:
        from mutagen import File as MFile
    except ImportError:
        return None
    try:
        audio = MFile(str(path))
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None
    try:
        if hasattr(audio.tags, "getall"):
            frames = audio.tags.getall("USLT")
            if frames:
                return str(frames[0].text)
    except Exception:
        pass
    for key in ("\xa9lyr", "LYRICS", "UNSYNCEDLYRICS", "unsyncedlyrics",
                "lyrics", "LYRICS-XXX", "WM/Lyrics"):
        try:
            value = audio.tags.get(key)
        except Exception:
            continue
        if not value:
            continue
        if isinstance(value, list):
            value = value[0]
        if str(value).strip():
            return str(value)
    return None


def find_lyrics(path: Path, tags: dict, forced_enc: str | None = None) -> tuple[str | None, str]:
    """Return (plain_text_lyrics, where_it_came_from)."""
    embedded = _lyrics_from_probe_tags(tags) or _lyrics_from_mutagen(path)
    if embedded and embedded.strip():
        return lrc_to_plain(repair_text(embedded)), "内嵌标签"

    for cand in (path.with_suffix(".lrc"), path.with_suffix(".LRC"),
                 path.parent / f"{path.stem}.lrc"):
        if cand.exists():
            try:
                raw = cand.read_bytes()
            except OSError:
                continue
            text = lrc_to_plain(decode_text(raw, forced_enc))
            if text:
                return text, f"外挂歌词 {cand.name}"
    return None, "无"


# --------------------------------------------------------------------------
# covers
# --------------------------------------------------------------------------

_IMG_MAGIC = ((b"\xff\xd8\xff", 13, ".jpg"), (b"\x89PNG\r\n\x1a\n", 14, ".png"))


def _image_bytes(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def find_cover(path: Path, streams: list[dict], workdir: Path,
               log: Callable[[str], None] | None = None) -> tuple[bytes | None, str]:
    """Return (image_bytes, description). Embedded art first, then sidecars."""
    for stream in streams:
        is_pic = stream.get("disposition", {}).get("attached_pic") == 1
        if stream.get("codec_type") == "video" and is_pic:
            ext = ".png" if "png" in stream.get("codec_name", "") else ".jpg"
            tmp = workdir / f"cover{ext}"
            try:
                run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
                     "-i", str(path), "-map", f"0:{stream['index']}",
                     "-frames:v", "1", "-c:v", "copy", str(tmp)])
            except AMCError:
                try:
                    run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
                         "-i", str(path), "-map", f"0:{stream['index']}",
                         "-frames:v", "1", "-c:v", "png", str(tmp)])
                except AMCError:
                    continue
            data = _image_bytes(tmp)
            if data:
                return _shrink_if_huge(data, workdir, log), "内嵌封面"

    folder = path.parent
    candidates: list[Path] = []
    for stem in COVER_STEMS:
        for ext in (".jpg", ".jpeg", ".png"):
            candidates.append(folder / f"{stem}{ext}")
    for ext in (".jpg", ".jpeg", ".png"):
        candidates.append(folder / f"{path.stem}{ext}")
        candidates.append(folder / f"{path.stem}.cover{ext}")
    for extra in sorted(folder.glob("AlbumArt*")):
        candidates.append(extra)

    for cand in candidates:
        if cand.exists() and cand.is_file():
            data = _image_bytes(cand)
            if data:
                return _shrink_if_huge(data, workdir, log), f"外挂图片 {cand.name}"
    return None, "无"


def _shrink_if_huge(data: bytes, workdir: Path, log: Callable[[str], None] | None) -> bytes:
    """Recompress absurdly large art; Apple Music is happy with a few MB."""
    if len(data) <= 6 * 1024 * 1024:
        return data
    src = workdir / "big_cover"
    src.write_bytes(data)
    dst = workdir / "small_cover.jpg"
    try:
        run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
             "-vf", "scale='min(2000,iw)':-2", "-q:v", "3", str(dst)])
        smaller = dst.read_bytes()
        if log:
            log(f"    封面 {len(data)//1024}KB 过大，已压到 {len(smaller)//1024}KB")
        return smaller
    except (AMCError, OSError):
        return data


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

@dataclass
class MediaInfo:
    path: Path
    codec: str
    sample_rate: int
    channels: int
    bits: int
    duration: float
    size: int
    tags: dict
    streams: list[dict]

    @property
    def is_lossless(self) -> bool:
        return self.codec in LOSSLESS_CODECS

    @property
    def is_native(self) -> bool:
        return self.codec in APPLE_NATIVE

    def tag(self, *names: str) -> str:
        for name in names:
            for key, value in self.tags.items():
                if key.lower() == name.lower() and value:
                    return repair_text(str(value).strip())
        return ""


_EMPTY_BITS = {"mp3", "aac", "vorbis", "opus", "wmav1", "wmav2", "wmapro", "mp2"}


def probe(path: Path) -> MediaInfo:
    out, _ = run([ffprobe(), "-v", "error", "-show_format", "-show_streams",
                  "-of", "json", str(path)])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AMCError(f"无法解析 ffprobe 输出: {exc}") from exc

    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise AMCError("文件里没有音频流")

    fmt = data.get("format", {})
    bits = int(audio.get("bits_per_raw_sample") or 0) or int(audio.get("bits_per_sample") or 0)
    codec = audio.get("codec_name", "")
    if bits <= 0:
        bits = 16 if codec in _EMPTY_BITS else 24 if codec in ("pcm_s24le",) else 16

    duration = float(fmt.get("duration") or audio.get("duration") or 0.0)
    if duration <= 0:
        raise AMCError("无法读取时长")

    return MediaInfo(
        path=path,
        codec=codec,
        sample_rate=int(audio.get("sample_rate") or 44100),
        channels=int(audio.get("channels") or 2),
        bits=bits,
        duration=duration,
        size=int(fmt.get("size") or path.stat().st_size),
        tags=dict(fmt.get("tags") or {}),
        streams=streams,
    )


# --------------------------------------------------------------------------
# conversion planning
# --------------------------------------------------------------------------

@dataclass
class Plan:
    mode: str                       # "copy" | "remux" | "alac" | "aac"
    desc: str
    out_ext: str = ".m4a"
    sample_rate: int | None = None
    sample_fmt: str | None = None
    bitrate_kbps: int | None = None
    est_bytes: int = 0

    def label(self) -> str:
        if self.mode == "copy":
            return f"直通复用（零重编码）{self.out_ext}"
        if self.mode == "remux":
            return f"重新封装 {self.out_ext}（音频直通，零重编码）"
        if self.mode == "alac":
            return f"ALAC 无损 {self.sample_rate}Hz/{self._bits()}bit"
        return f"AAC {self.bitrate_kbps}kbps {self.sample_rate}Hz"

    def _bits(self) -> int:
        return 16 if self.sample_fmt in (None, "s16p") else 24


def container_for(codec: str) -> str:
    """The Apple-compatible container a given codec belongs in.

    Guards against sources whose extension lies about their payload, e.g. an
    AAC stream sitting in a file named ``.mp3``: copying that through verbatim
    produces something Apple Music may refuse, so it gets repackaged instead.
    """
    return ".mp3" if codec == "mp3" else ".m4a"


def _downsample_target(sr: int) -> int:
    if sr <= 48000:
        return sr
    if sr % 44100 == 0:
        return 44100
    return 48000


def est_alac_bytes(sr: int, bits: int, channels: int, duration: float) -> int:
    return int(sr * bits * channels * ALAC_RATIO * duration / 8)


def plan_candidates(info: MediaInfo, target_bytes: int, force_alac: bool = False) -> list[Plan]:
    """Ordered least-destructive-first list; the last entry always fits the budget."""
    plans: list[Plan] = []
    sr0, bits0, ch, dur = info.sample_rate, info.bits, info.channels, info.duration
    budget_bps = target_bytes * 8 / max(dur, 1.0)
    allowed_kbps = max(64, min(MAX_AAC_KBPS, int(budget_bps / 1000 * 0.97)))

    # 1. Already Apple-native and small enough -> do not touch the audio at all.
    if info.is_native and not force_alac:
        ext = container_for(info.codec)
        same_container = ext == info.path.suffix.lower()
        plans.append(Plan(
            "copy" if same_container else "remux",
            "Apple 原生格式，音频直通",
            out_ext=ext, est_bytes=info.size))

    # 2. Lossless ALAC ladder, mild reductions first.
    if info.is_lossless or force_alac:
        ladder: list[tuple[int, int]] = [(sr0, bits0)]
        if bits0 > 16:
            ladder.append((sr0, 16))
        if sr0 > 48000:
            ladder.append((_downsample_target(sr0), min(bits0, 16)))
        if sr0 != 44100:
            ladder.append((44100, 16))
        seen: set[tuple[int, int]] = set()
        for sr, bits in ladder:
            key = (sr, bits)
            if key in seen:
                continue
            seen.add(key)
            est = est_alac_bytes(sr, bits, ch, dur)
            # keep a little slack: the estimate is only a guess
            if est <= target_bytes * 1.05:
                plans.append(Plan("alac", "ALAC 无损", sample_rate=sr,
                                  sample_fmt="s16p" if bits <= 16 else "s32p",
                                  est_bytes=est))

    # 3. Guaranteed-to-fit fallback.
    plans.append(Plan("aac", "AAC 转码（预算内最高码率）", sample_rate=min(sr0, 48000),
                      bitrate_kbps=allowed_kbps,
                      est_bytes=int(allowed_kbps * 1000 * dur / 8)))

    return plans


# --------------------------------------------------------------------------
# encoding / tagging
# --------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str) -> str:
    name = _INVALID.sub("_", text).strip().rstrip(". ")
    return name[:150] or fallback


def output_path(info: MediaInfo, outdir: Path, ext: str, keep_name: bool) -> Path:
    if keep_name or not (info.tag("artist") and info.tag("title")):
        stem = info.path.stem
    else:
        stem = f"{info.tag('artist')} - {info.tag('title')}"
    stem = safe_name(stem, info.path.stem)
    dest = outdir / f"{stem}{ext}"
    n = 2
    while dest.exists():
        dest = outdir / f"{stem} ({n}){ext}"
        n += 1
    return dest


def encode_audio(info: MediaInfo, plan: Plan, dest: Path) -> None:
    if plan.mode == "copy":
        shutil.copy2(info.path, dest)
        return

    if plan.mode == "remux":
        # Repackage the existing stream only; no re-encode, no quality loss.
        run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
             "-i", str(info.path), "-map", "0:a:0", "-c:a", "copy",
             "-map_metadata", "-1", "-movflags", "+faststart", str(dest)])
        return

    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-i", str(info.path), "-map", "0:a:0"]
    if plan.mode == "alac":
        cmd += ["-c:a", "alac", "-sample_fmt", plan.sample_fmt or "s16p"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.bitrate_kbps}k"]
    if plan.sample_rate:
        cmd += ["-ar", str(plan.sample_rate)]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", str(dest)]
    run(cmd)


def _image_format(data: bytes) -> int:
    from mutagen.mp4 import MP4Cover
    return MP4Cover.FORMAT_PNG if data.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG


def _split_track(value: str) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", value or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def tag_output(dest: Path, info: MediaInfo, lyrics: str | None,
               cover: bytes | None, log: Callable[[str], None]) -> None:
    """Write all text metadata, the cover and the lyrics (UTF-8 native)."""
    title = info.tag("title")
    artist = info.tag("artist")
    album = info.tag("album")
    album_artist = info.tag("album_artist", "albumartist", "ALBUMARTIST")
    date = info.tag("date", "year", "originaldate")[:10]
    genre = info.tag("genre")
    track = _split_track(info.tag("track", "TRCK"))
    disc = _split_track(info.tag("disc", "TPOS"))

    if dest.suffix.lower() == ".mp3":
        _tag_mp3(dest, title, artist, album, album_artist, date, genre, track, disc,
                 lyrics, cover)
    else:
        _tag_mp4(dest, title, artist, album, album_artist, date, genre, track, disc,
                 lyrics, cover)


def _tag_mp4(dest, title, artist, album, album_artist, date, genre, track, disc,
             lyrics, cover) -> None:
    from mutagen.mp4 import MP4, MP4Cover  # noqa: F401
    audio = MP4(str(dest))
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for key, value in (("\xa9nam", title), ("\xa9ART", artist), ("\xa9alb", album),
                       ("aART", album_artist), ("\xa9day", date), ("\xa9gen", genre)):
        if value:
            t[key] = [value]
        elif key in t:
            del t[key]
    if track:
        t["trkn"] = [track]
    if disc:
        t["disk"] = [disc]
    if lyrics:
        t["\xa9lyr"] = [lyrics]
    if cover:
        t["covr"] = [MP4Cover(cover, imageformat=_image_format(cover))]
    audio.save()


def _tag_mp3(dest, title, artist, album, album_artist, date, genre, track, disc,
             lyrics, cover) -> None:
    from mutagen.id3 import (ID3, TIT2, TPE1, TALB, TPE2, TDRC, TCON, TRCK, TPOS,
                             APIC, USLT, ID3NoHeaderError)
    try:
        tags = ID3(str(dest))
    except ID3NoHeaderError:
        tags = ID3()
    for frame in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC", "TCON", "TRCK", "TPOS",
                  "APIC", "USLT"):
        tags.delall(frame)
    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if album_artist:
        tags.add(TPE2(encoding=3, text=album_artist))
    if date:
        tags.add(TDRC(encoding=3, text=date))
    if genre:
        tags.add(TCON(encoding=3, text=genre))
    if track:
        tags.add(TRCK(encoding=3, text=f"{track[0]}/{track[1]}" if track[1] else str(track[0])))
    if disc:
        tags.add(TPOS(encoding=3, text=f"{disc[0]}/{disc[1]}" if disc[1] else str(disc[0])))
    if cover:
        mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
    if lyrics:
        tags.add(USLT(encoding=3, lang="XXX", desc="", text=lyrics))
    tags.save(str(dest), v2_version=3)


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

@dataclass
class Options:
    outdir: Path | None = None
    target_mb: float = 50.0
    keep_name: bool = False
    force_alac: bool = False
    lyrics_encoding: str | None = None
    overwrite: bool = True


@dataclass
class Result:
    src: Path
    dest: Path | None = None
    plan: Plan | None = None
    size: int = 0
    lyrics_from: str = "无"
    cover_from: str = "无"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def convert_file(src: Path, opts: Options, log: Callable[[str], None]) -> Result:
    res = Result(src=src)
    try:
        info = probe(src)
    except AMCError as exc:
        res.error = str(exc)
        return res

    lyrics, lyrics_from = find_lyrics(src, info.tags, opts.lyrics_encoding)
    res.lyrics_from = lyrics_from
    if not lyrics:
        res.warnings.append("未找到歌词")

    target_bytes = int(opts.target_mb * 1024 * 1024)
    outdir = opts.outdir or src.parent
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="amc_") as tmp:
        tmpdir = Path(tmp)
        cover, cover_from = find_cover(src, info.streams, tmpdir, log)
        res.cover_from = cover_from
        if not cover:
            res.warnings.append("未找到封面")

        plans = plan_candidates(info, target_bytes, opts.force_alac)
        log(f"  源: {info.codec} {info.sample_rate}Hz/{info.bits}bit/{info.channels}ch "
            f"{info.duration:.0f}s {info.size/1048576:.1f}MB")

        last_error: str | None = None
        for i, plan in enumerate(plans):
            dest = output_path(info, outdir, plan.out_ext, opts.keep_name)
            if dest.exists() and not opts.overwrite:
                res.error = f"已存在: {dest.name}"
                return res
            log(f"  尝试 {i+1}/{len(plans)}: {plan.label()}")
            try:
                encode_audio(info, plan, dest)
            except AMCError as exc:
                last_error = str(exc)
                dest.unlink(missing_ok=True)
                continue

            size = dest.stat().st_size
            if size <= target_bytes or i == len(plans) - 1:
                tag_output(dest, info, lyrics, cover, log)
                res.dest = dest
                res.plan = plan
                res.size = dest.stat().st_size
                if size > target_bytes:
                    res.warnings.append(
                        f"仍超出目标 {size/1048576:.1f}MB > {opts.target_mb}MB")
                return res

            log(f"    {size/1048576:.1f}MB 超出预算，降级重试")
            dest.unlink(missing_ok=True)

        res.error = last_error or "所有编码方案均失败"
        return res


def collect_inputs(paths: Iterable[Path]) -> list[Path]:
    """Expand folders into a sorted list of audio files."""
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in AUDIO_EXTS:
                    found.append(child)
        elif path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            found.append(path)
    seen: set[str] = set()
    unique: list[Path] = []
    for item in found:
        key = str(item.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fmt_size(n: int) -> str:
    return f"{n/1048576:.1f}MB" if n >= 1048576 else f"{n/1024:.0f}KB"
