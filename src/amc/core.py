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
* The budget covers the *finished* file. Cover art and lyrics are reserved out
  of the budget before the audio encoder is chosen, and the size of the tagged
  result is measured before it is allowed to replace anything.
* Output is written to a temporary file beside the destination and moved into
  place with ``os.replace``, so an interrupted run cannot leave a truncated
  file behind -- and an existing file is either replaced whole or not at all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:                       # pragma: no cover - typing only
    from threading import Event

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
LOSSY_CODECS = {
    "mp3", "mp2", "aac", "ac3", "eac3", "vorbis", "opus", "wmav1", "wmav2",
    "wmapro", "atrac3", "atrac3p", "mp1", "amrnb", "amrwb", "cook", "adpcm",
}
APPLE_NATIVE = {"mp3", "aac", "alac"}          # Apple Music plays these as-is
AUDIO_EXTS = {
    ".flac", ".wav", ".wave", ".aiff", ".aif", ".aifc", ".ape", ".wv", ".tta",
    ".tak", ".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".asf", ".dsf", ".dff", ".mpc", ".caf", ".mka", ".ac3", ".mp2",
    ".w64", ".rf64", ".amr", ".au", ".snd",
}
COVER_STEMS = ("cover", "folder", "front", "album", "albumart", "thumb", "artwork")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
LYRIC_EXTS = (".lrc", ".srt")
MAX_AAC_KBPS = 512
ALAC_RATIO = 0.62          # typical ALAC payload / raw PCM, for reporting only
# ALAC cannot realistically beat this fraction of raw PCM, so a rung whose raw
# size exceeds budget/MIN_RATIO is the only one that may be skipped outright.
# Everything else is decided by encoding the real file and measuring it.
ALAC_MIN_RATIO = 0.45
MAX_COVER_BYTES = 6 * 1024 * 1024
MAX_COVER_PX = 2000
UNLIMITED_BYTES = 1 << 62  # "--target-mb 0"
TMP_PREFIX = ".amc-tmp-"   # staging files; never treated as input
POLL_SECONDS = 0.2         # cancellation granularity while a child runs


class AMCError(RuntimeError):
    """Raised when a file cannot be converted."""


class AMCCancelled(Exception):
    """Raised when the caller asked to stop.

    Deliberately not an :class:`AMCError`: the plan loop treats an AMCError as
    "this encoder did not work, try the next one", whereas a cancellation has to
    unwind the whole batch.
    """


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


def reset_tool_cache() -> None:
    """Forget the cached ffmpeg/ffprobe paths (used by tests)."""
    global _FFMPEG, _FFPROBE
    _FFMPEG = None
    _FFPROBE = None


def run(cmd: list[str], cancel: "Event | None" = None) -> tuple[str, str]:
    """Run an argv list, returning (stdout, stderr) decoded as UTF-8.

    ``cancel`` is polled while the child runs: once it is set the child is
    killed and :class:`AMCCancelled` is raised. Without that, one stuck ffmpeg
    would hold a whole batch hostage with no way out from the interface.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    while True:
        try:
            # Retrying communicate() after a timeout does not lose output.
            stdout, stderr = proc.communicate(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise AMCCancelled() from None

    out = (stdout or b"").decode("utf-8", "replace")
    err = (stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = (err or out).strip().splitlines()
        detail = " / ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise AMCError(f"{Path(cmd[0]).name} 执行失败: {detail}")
    return out, err


# --------------------------------------------------------------------------
# text repair / decoding
# --------------------------------------------------------------------------

_LATIN1_SUPP = re.compile(r"[\u0080-\u00ff]")
# Scripts that mojibake is *made of*. Latin-1 accents are excluded on purpose:
# they are what a correctly tagged European name looks like.
_NON_LATIN = re.compile(
    r"[\u0370-\u03ff\u0400-\u04ff\u0530-\u058f\u0590-\u05ff\u0600-\u06ff"
    r"\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff]"
)
MOJIBAKE_MIN_ACCENTS = 2      # a lone "é" is not evidence of anything
MOJIBAKE_MIN_RATIO = 0.34     # mojibake is pervasive, real accents are not
MOJIBAKE_MIN_GAIN = 2         # a decoding must yield at least two real glyphs

# East Asian scripts are weighted far above the single-byte candidates: cp1251
# maps every byte to a Cyrillic letter, so it always halves the string length
# and would otherwise outscore the correct GBK/Big5 reading of the same bytes.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_CJK_WEIGHT = 4
_OTHER_WEIGHT = 1


def _mojibake_score(s: str) -> int:
    cjk = len(_CJK.findall(s))
    other = len(_NON_LATIN.findall(s)) - cjk
    return (_CJK_WEIGHT * cjk + _OTHER_WEIGHT * other
            - len(_LATIN1_SUPP.findall(s)))


def repair_text(s: str) -> str:
    """Undo the classic 'UTF-8/GBK bytes decoded as Latin-1' mojibake.

    Common in Chinese ID3v2.3 tags written by old taggers. Only fires when the
    string really looks like mojibake: every CJK character turns into one to
    three Latin-1 supplement characters, so mangled text is *made of* them,
    while a correctly tagged name merely contains one or two ("Motörhead",
    "Café Tacvba", "Renée"). Re-decoding those invents CJK that was never there
    -- and because the repaired text also feeds the output filename, guessing
    wrong corrupts the file name as well.
    """
    if not s or not _LATIN1_SUPP.search(s):
        return s
    accents = len(_LATIN1_SUPP.findall(s))
    if accents < MOJIBAKE_MIN_ACCENTS or accents < MOJIBAKE_MIN_RATIO * len(s):
        return s
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s

    # UTF-8 is self-validating, so a clean decode is the strongest evidence
    # available: GBK bytes almost never happen to be valid UTF-8, whereas UTF-8
    # bytes for CJK usually *are* plausible GBK pairs. Trying the scored
    # candidates first therefore read UTF-8 mojibake back as garbage CJK.
    try:
        cand = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        if len(_NON_LATIN.findall(cand)) >= MOJIBAKE_MIN_GAIN:
            return cand

    best, best_score = s, _mojibake_score(s)
    for enc in ("gb18030", "big5", "shift_jis", "cp1251"):
        try:
            cand = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if len(_NON_LATIN.findall(cand)) < MOJIBAKE_MIN_GAIN:
            continue
        score = _mojibake_score(cand)
        if score > best_score:
            best, best_score = cand, score
    return best


def _wide_text_encoding(sample: bytes) -> str | None:
    """Detect BOM-less UTF-16, whose NUL bytes sit on one strand of byte pairs.

    Notepad-style "Unicode" files without a BOM are common enough to matter, and
    the single-byte fallbacks would otherwise return a NUL-riddled string. The
    strand test also keeps BOM-less UTF-32 from being mistaken for UTF-16.
    """
    if len(sample) < 4 or sample.count(0) / len(sample) <= 0.25:
        return None
    pairs = sample[: len(sample) - len(sample) % 2]
    for enc, offset in (("utf-16-le", 1), ("utf-16-be", 0)):
        strand = sum(1 for i in range(offset, len(pairs), 2) if pairs[i] == 0)
        other = sum(1 for i in range(1 - offset, len(pairs), 2) if pairs[i] == 0)
        if strand and strand / (strand + other) > 0.9:
            return enc
    return None


def decode_text(raw: bytes, forced: str | None = None) -> str:
    """Decode a lyrics/text file, working around unknown legacy encodings."""
    if forced:
        return raw.decode(forced, "replace")
    # Longest BOM first: the UTF-32-LE BOM *starts with* the UTF-16-LE BOM, so
    # testing UTF-16 first hands UTF-32 lyrics to the wrong codec.
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")

    wide = _wide_text_encoding(raw[:400])
    if wide is not None:
        try:
            return raw.decode(wide)
        except UnicodeDecodeError:
            pass

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
_SRT_INDEX = re.compile(r"^\s*\d{1,4}\s*$")
_SRT_TIME = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")


def lrc_to_plain(text: str) -> str:
    """Reduce LRC/SRT lyrics to clean plain text lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = text.split("\n")
    lines: list[str] = []
    last_stamps: tuple[str, ...] = ()
    for i, raw in enumerate(raw_lines):
        # SRT housekeeping: a bare index line introduces a timing line. Only a
        # number that actually precedes a cue is dropped, so "1 2 3 4" sung as a
        # lyric line survives.
        if _SRT_TIME.match(raw):
            continue
        if _SRT_INDEX.match(raw) and i + 1 < len(raw_lines) and _SRT_TIME.match(raw_lines[i + 1]):
            continue

        stamps = tuple(_LRC_TIME.findall(raw)) + tuple(_LRC_WORD.findall(raw))
        line = _LRC_META.sub("", raw)
        line = _LRC_WORD.sub("", line)
        line = _LRC_TIME.sub("", line)
        line = _LRC_BG.sub("", line)
        line = line.strip()
        if not line or re.fullmatch(r"[\s\-–—]*", line):
            continue
        # A malformed LRC repeats a line under the *same* timestamp; a chorus
        # repeats it under different ones and has to survive.
        if lines and lines[-1] == line and stamps and stamps == last_stamps:
            continue
        lines.append(line)
        last_stamps = stamps
    return "\n".join(lines)


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


def _sidecar_dir(path: Path) -> dict[str, Path]:
    """Case-insensitive map of the source folder's files (extension included)."""
    try:
        return {p.name.lower(): p for p in path.parent.iterdir() if p.is_file()}
    except OSError:
        return {}


def _lyric_candidates(path: Path) -> list[Path]:
    folder = _sidecar_dir(path)
    found: list[Path] = []
    for ext in LYRIC_EXTS:
        hit = folder.get(f"{path.stem}{ext}".lower())
        if hit:
            found.append(hit)
    return found


def find_lyrics(path: Path, tags: dict, forced_enc: str | None = None,
                repair: bool = True) -> tuple[str | None, str]:
    """Return (plain_text_lyrics, where_it_came_from)."""
    embedded = _lyrics_from_probe_tags(tags) or _lyrics_from_mutagen(path)
    if embedded and embedded.strip():
        text = repair_text(embedded) if repair else embedded
        return lrc_to_plain(text), "内嵌标签"

    for cand in _lyric_candidates(path):
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

# Apple Music only renders JPEG/PNG cover art, and a file's extension is no
# guarantee of its payload, so art is identified by magic number and converted
# when it is anything else.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)


def _image_kind(data: bytes) -> str | None:
    """Identify image bytes by magic number; the extension is not trusted."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for signature, kind in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return kind
    return None


def _image_bytes(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _normalise_cover(data: bytes, workdir: Path, log: Callable[[str], None] | None = None,
                     cancel: "Event | None" = None) -> bytes | None:
    """Return cover bytes guaranteed to be JPEG or PNG, or None if unusable."""
    kind = _image_kind(data)
    if kind is None:
        return None
    if kind in ("jpeg", "png") and len(data) <= MAX_COVER_BYTES:
        return data

    src = workdir / f"cover_src.{kind}"
    dst = workdir / "cover_norm.jpg"
    try:
        src.write_bytes(data)
        run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
             "-i", str(src), "-frames:v", "1", "-an",
             "-vf", f"scale='min({MAX_COVER_PX},iw)':-2", "-q:v", "3", str(dst)], cancel)
        converted = dst.read_bytes()
    except (AMCError, OSError):
        # Unconvertible art is no art -- but usable JPEG/PNG must not be thrown
        # away just because re-encoding failed.
        return data if kind in ("jpeg", "png") else None
    if not converted:
        return None
    if log:
        log(f"    封面 {kind} {len(data)//1024}KB -> JPEG {len(converted)//1024}KB")
    return converted


def _cover_candidates(path: Path) -> list[Path]:
    """Sidecar artwork in priority order: generic names before track names."""
    folder = _sidecar_dir(path)
    if not folder:
        return []
    ordered: list[Path] = []
    for stem in COVER_STEMS:
        for ext in COVER_EXTS:
            hit = folder.get(f"{stem}{ext}")
            if hit:
                ordered.append(hit)
    for ext in COVER_EXTS:
        for name in (f"{path.stem}{ext}", f"{path.stem}.cover{ext}",
                     f"{path.stem}.folder{ext}", f"{path.stem} - cover{ext}"):
            hit = folder.get(name.lower())
            if hit:
                ordered.append(hit)
    # Windows Media Player writes AlbumArt_{GUID}_Large.jpg and friends.
    ordered += [folder[name] for name in sorted(folder) if name.startswith("albumart")]
    return ordered


def find_cover(path: Path, streams: list[dict], workdir: Path,
               log: Callable[[str], None] | None = None,
               cancel: "Event | None" = None) -> tuple[bytes | None, str]:
    """Return (image_bytes, description). Embedded art first, then sidecars."""
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        if stream.get("disposition", {}).get("attached_pic") != 1:
            continue
        codec = stream.get("codec_name", "")
        ext = {"mjpeg": ".jpg", "jpeg": ".jpg", "png": ".png", "bmp": ".bmp",
               "gif": ".gif", "webp": ".webp"}.get(codec)
        if ext is None:
            continue
        tmp = workdir / f"embedded{ext}"
        for codec_args in (["-c:v", "copy"], ["-c:v", "png"]):
            try:
                run([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
                     "-i", str(path), "-map", f"0:{stream['index']}",
                     "-frames:v", "1", *codec_args, str(tmp)], cancel)
            except AMCError:
                continue
            data = _image_bytes(tmp)
            if data:
                art = _normalise_cover(data, workdir, log, cancel)
                if art:
                    return art, "内嵌封面"

    for cand in _cover_candidates(path):
        data = _image_bytes(cand)
        if not data:
            continue
        art = _normalise_cover(data, workdir, log, cancel)
        if art:
            return art, f"外挂图片 {cand.name}"
    return None, "无"


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
    bitrate: int = 0            # audio-stream bit rate in bits/s (0 = unknown)
    repair: bool = True         # honour repair_text() when reading tags

    @property
    def is_lossless(self) -> bool:
        return self.codec in LOSSLESS_CODECS

    @property
    def is_lossy(self) -> bool:
        return self.codec in LOSSY_CODECS

    @property
    def is_native(self) -> bool:
        return self.codec in APPLE_NATIVE

    def tag(self, *names: str) -> str:
        for name in names:
            for key, value in self.tags.items():
                if key.lower() == name.lower() and value:
                    text = str(value).strip()
                    return repair_text(text) if self.repair else text
        return ""


_EMPTY_BITS = {"mp3", "aac", "vorbis", "opus", "wmav1", "wmav2", "wmapro", "mp2"}


def probe(path: Path, repair_tags: bool = True) -> MediaInfo:
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

    size = int(fmt.get("size") or path.stat().st_size)
    try:
        bitrate = int(audio.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    if bitrate <= 0:
        # The format-level bit rate would include embedded art, so derive the
        # audio-only figure from the duration instead.
        bitrate = int(size * 8 / duration)

    return MediaInfo(
        path=path,
        codec=codec,
        sample_rate=int(audio.get("sample_rate") or 44100),
        channels=int(audio.get("channels") or 2),
        bits=bits,
        duration=duration,
        size=size,
        tags=dict(fmt.get("tags") or {}),
        streams=streams,
        bitrate=bitrate,
        repair=repair_tags,
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
    bits: int | None = None

    @property
    def rewrites_audio(self) -> bool:
        """True when the plan cannot be undone without the source file."""
        return self.mode in ("alac", "aac")

    def label(self) -> str:
        if self.mode == "copy":
            return f"直通复用（零重编码）{self.out_ext}"
        if self.mode == "remux":
            return f"重新封装 {self.out_ext}（音频直通，零重编码）"
        if self.mode == "alac":
            return f"ALAC 无损 {self.sample_rate}Hz/{self._bits()}bit"
        return f"AAC {self.bitrate_kbps}kbps {self.sample_rate}Hz"

    def _bits(self) -> int:
        if self.bits:
            return self.bits
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


_DSD_PCM_RATES = {64: 88200, 128: 176400, 256: 352800, 512: 705600}


def _pcm_params(codec: str, sample_rate: int, bits: int) -> tuple[int, int]:
    """The PCM sample rate/bit depth an ALAC encoder can actually be fed.

    DSD is 1-bit at 64/128/256x 44.1kHz. Handing 2822400Hz/16bit to the ALAC
    ladder makes *every* rung look impossible, so a DSD source silently
    collapsed to a downsampled CD-rate plan or to AAC. DSD is conventionally
    decoded to 88.2k/176.4k PCM instead, which is what the first rung now
    offers. 64-bit float PCM is likewise not an ALAC depth.
    """
    if codec.startswith("dsd"):
        multiple = round(sample_rate / 44100) if sample_rate > 0 else 64
        return _DSD_PCM_RATES.get(multiple, 88200), 24
    if bits >= 32:
        return sample_rate, 32
    if bits > 16:
        return sample_rate, 24
    return sample_rate, 16


def est_alac_bytes(sr: int, bits: int, channels: int, duration: float) -> int:
    """Rough ALAC size for reporting; never the last word on whether it fits."""
    return int(sr * bits * channels * ALAC_RATIO * duration / 8)


def alac_might_fit(sr: int, bits: int, channels: int, duration: float,
                   target_bytes: int) -> bool:
    """Whether an ALAC rung is worth *trying*.

    The point is to skip only the rungs that cannot possibly fit, and to let the
    encoder decide the rest: the estimate above is a guess in both directions, and
    gating on it threw away rungs whose real output was smaller than the budget
    (a 0.4MB cap picked AAC while ALAC 48kHz/16bit measured in at a fifth of it).
    """
    raw_bytes = sr * bits * channels * duration / 8
    return raw_bytes * ALAC_MIN_RATIO <= target_bytes


def plan_candidates(info: MediaInfo, target_bytes: int, force_alac: bool = False) -> list[Plan]:
    """Ordered least-destructive-first list; the last entry always fits the budget."""
    plans: list[Plan] = []
    ch, dur = info.channels, info.duration
    sr0, bits0 = _pcm_params(info.codec, info.sample_rate, info.bits)
    budget_bps = target_bytes * 8 / max(dur, 1.0)
    allowed_kbps = max(64, min(MAX_AAC_KBPS, int(budget_bps / 1000 * 0.97)))

    # 1. Already Apple-native and small enough -> do not touch the audio at all.
    #    A source that already busts the cap is skipped: copying 400MB only to
    #    reject it on size wastes the slowest part of the run.
    if info.is_native and not force_alac and info.size <= target_bytes:
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
        for sr, bits in dict.fromkeys(ladder):       # dedupe, keep order
            if not alac_might_fit(sr, bits, ch, dur, target_bytes):
                continue
            plans.append(Plan("alac", "ALAC 无损", sample_rate=sr,
                              sample_fmt="s16p" if bits <= 16 else "s32p",
                              est_bytes=est_alac_bytes(sr, bits, ch, dur), bits=bits))

    # 3. Guaranteed-to-fit fallback. Resampling stays inside the same family
    #    (88.2k -> 44.1k) so the rate converter remains a clean integer ratio.
    aac_kbps = allowed_kbps
    if info.bitrate > 0 and info.is_lossy:
        # Re-encoding lossy audio above its own bit rate buys nothing but bytes.
        aac_kbps = min(aac_kbps, max(96, int(info.bitrate / 1000 * 1.5)))
    plans.append(Plan("aac", "AAC 转码（预算内最高码率）",
                      sample_rate=_downsample_target(sr0), bitrate_kbps=aac_kbps,
                      est_bytes=int(aac_kbps * 1000 * dur / 8)))

    return plans


# --------------------------------------------------------------------------
# encoding / tagging
# --------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = ({"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
                 | {f"{base}{i}" for base in ("com", "lpt") for i in range(1, 10)})
_MAX_STEM = 150


def safe_name(text: str, fallback: str) -> str:
    name = _INVALID.sub("_", text).strip().rstrip(". ")
    # "CON.m4a" is still the console device: Windows reserves the stem, not the
    # whole name, so the check happens before any extension is appended.
    if name.split(".")[0].lower() in _WIN_RESERVED:
        name = f"_{name}"
    name = name[:_MAX_STEM].rstrip(". ")
    return name or fallback


def output_path(info: MediaInfo, outdir: Path, ext: str, keep_name: bool) -> Path:
    """The natural destination for this track; collisions are the caller's job."""
    if keep_name or not (info.tag("artist") and info.tag("title")):
        stem = info.path.stem
    else:
        stem = f"{info.tag('artist')} - {info.tag('title')}"
    return outdir / f"{safe_name(stem, info.path.stem)}{ext}"


def unique_path(dest: Path) -> Path:
    """First free ``name (2).ext`` variant of ``dest``."""
    n = 2
    while True:
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not cand.exists():
            return cand
        n += 1


def same_file(a: Path, b: Path) -> bool:
    """True when two paths denote the same file (case- and alias-insensitive)."""
    try:
        if a.resolve() == b.resolve():
            return True
    except OSError:
        pass
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def encode_audio(info: MediaInfo, plan: Plan, dest: Path,
                 cancel: "Event | None" = None) -> None:
    if plan.mode == "copy":
        shutil.copy2(info.path, dest)
        return

    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-i", str(info.path), "-map", "0:a:0"]
    if plan.mode == "remux":
        # Repackage the existing stream only; no re-encode, no quality loss.
        cmd += ["-c:a", "copy"]
    elif plan.mode == "alac":
        cmd += ["-c:a", "alac", "-sample_fmt", plan.sample_fmt or "s16p"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.bitrate_kbps}k"]
    if plan.sample_rate:
        cmd += ["-ar", str(plan.sample_rate)]
    cmd += ["-map_metadata", "-1"]
    # -movflags only means anything to the MP4 family; ffmpeg warns about it on
    # every other muxer, including the .mp3 a remux can legitimately produce.
    if dest.suffix.lower() in (".m4a", ".mp4", ".m4b"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(dest))
    run(cmd, cancel)


def _image_format(data: bytes) -> int:
    from mutagen.mp4 import MP4Cover
    return MP4Cover.FORMAT_PNG if _image_kind(data) == "png" else MP4Cover.FORMAT_JPEG


def _mime_for_image(data: bytes) -> str:
    return "image/png" if _image_kind(data) == "png" else "image/jpeg"


def _split_track(value: str) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", value or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def tag_output(dest: Path, info: MediaInfo, lyrics: str | None,
               cover: bytes | None) -> None:
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
        tags.add(APIC(encoding=3, mime=_mime_for_image(cover), type=3, desc="Cover",
                      data=cover))
    if lyrics:
        tags.add(USLT(encoding=3, lang="XXX", desc="", text=lyrics))
    tags.save(str(dest), v2_version=3)


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

@dataclass
class Options:
    outdir: Path | None = None
    target_mb: float = 50.0         # 0 = no limit
    keep_name: bool = False
    force_alac: bool = False
    lyrics_encoding: str | None = None
    overwrite: bool = True
    repair_tags: bool = True


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


def convert_file(src: Path, opts: Options, log: Callable[[str], None],
                 cancel: "Event | None" = None) -> Result:
    res = Result(src=src)
    try:
        info = probe(src, repair_tags=opts.repair_tags)
    except AMCError as exc:
        res.error = str(exc)
        return res

    lyrics, lyrics_from = find_lyrics(src, info.tags, opts.lyrics_encoding,
                                      repair=opts.repair_tags)
    res.lyrics_from = lyrics_from
    if not lyrics:
        res.warnings.append("未找到歌词")

    unlimited = not opts.target_mb or opts.target_mb <= 0
    target_bytes = UNLIMITED_BYTES if unlimited else int(opts.target_mb * 1024 * 1024)
    outdir = opts.outdir or src.parent
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.error = f"无法创建输出文件夹 {outdir}: {exc}"
        return res

    with tempfile.TemporaryDirectory(prefix="amc_") as tmp:
        tmpdir = Path(tmp)
        cover, cover_from = find_cover(src, info.streams, tmpdir, log, cancel)
        res.cover_from = cover_from
        if not cover:
            res.warnings.append("未找到封面")

        # Cover art and lyrics end up inside the finished file, so they come out
        # of the budget before the audio encoder is chosen. Without this a 6MB
        # cover walks straight through the promised cap.
        overhead = len(cover or b"") + len((lyrics or "").encode("utf-8")) + 16 * 1024
        audio_budget = UNLIMITED_BYTES if unlimited else max(0, target_bytes - overhead)
        plans = plan_candidates(info, audio_budget, opts.force_alac)

        log(f"  源: {info.codec} {info.sample_rate}Hz/{info.bits}bit/{info.channels}ch "
            f"{info.duration:.0f}s {info.size/1048576:.1f}MB")
        if not unlimited and overhead > 16 * 1024:
            log(f"  预留封面+歌词 {overhead/1024:.0f}KB，音频预算 "
                f"{audio_budget/1048576:.1f}MB")
        if not unlimited and overhead > target_bytes:
            # No encoder can rescue this: say so instead of quietly emitting a
            # file over the cap and leaving the user to guess why.
            res.warnings.append(
                f"封面+歌词 {overhead/1024:.0f}KB 已超过 {opts.target_mb:g}MB 上限")

        last_error: str | None = None
        for i, plan in enumerate(plans):
            if cancel is not None and cancel.is_set():
                raise AMCCancelled()

            dest = output_path(info, outdir, plan.out_ext, opts.keep_name)
            if plan.rewrites_audio and same_file(dest, src):
                # Re-encoding over the file being read would destroy the only
                # copy of the original, so divert instead of clobbering it.
                dest = unique_path(dest)
                note = f"与源文件同名，已改存为 {dest.name}"
                if note not in res.warnings:
                    res.warnings.append(note)
            elif not opts.overwrite and dest.exists():
                where = "（与源文件同名）" if same_file(dest, src) else ""
                res.error = f"已存在{where}: {dest.name}"
                return res

            log(f"  尝试 {i+1}/{len(plans)}: {plan.label()}")
            # Encode beside the destination, never over it: a crash or a kill
            # leaves the previous file untouched instead of truncated.
            staging = outdir / f"{TMP_PREFIX}{uuid.uuid4().hex[:8]}{plan.out_ext}"
            try:
                encode_audio(info, plan, staging, cancel)
                tag_output(staging, info, lyrics, cover)
            except AMCCancelled:
                staging.unlink(missing_ok=True)
                raise
            except (AMCError, OSError) as exc:
                last_error = str(exc)
                staging.unlink(missing_ok=True)
                continue

            try:
                size = staging.stat().st_size
                if size <= target_bytes or i == len(plans) - 1:
                    os.replace(staging, dest)
                else:
                    log(f"    {size/1048576:.1f}MB 超出预算，降级重试")
                    staging.unlink(missing_ok=True)
                    continue
            except OSError as exc:
                staging.unlink(missing_ok=True)
                last_error = f"写入失败: {exc}"
                continue

            res.dest = dest
            res.plan = plan
            res.size = dest.stat().st_size
            if res.size > target_bytes:
                res.warnings.append(
                    f"仍超出目标 {res.size/1048576:.1f}MB > {opts.target_mb:g}MB")
            return res

        res.error = last_error or "所有编码方案均失败"
        return res


def _is_audio(path: Path) -> bool:
    if path.name.startswith(TMP_PREFIX):
        return False        # our own staging file, never an input
    try:
        return path.is_file() and path.suffix.lower() in AUDIO_EXTS
    except OSError:
        return False


def collect_inputs(paths: Iterable[Path], recursive: bool = False) -> list[Path]:
    """Expand folders into a sorted, de-duplicated list of audio files."""
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        try:
            is_dir = path.is_dir()
        except OSError:
            continue
        if is_dir:
            walker = path.rglob("*") if recursive else path.glob("*")
            try:
                children = sorted(walker)
            except OSError:
                children = []
            found += [child for child in children if _is_audio(child)]
        elif _is_audio(path):
            found.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for item in found:
        try:
            key = str(item.resolve()).lower()
        except OSError:
            key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fmt_size(n: int) -> str:
    return f"{n/1048576:.1f}MB" if n >= 1048576 else f"{n/1024:.0f}KB"
