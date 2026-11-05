"""Fallback metadata parsed from file and folder names.

Many downloaded files carry no usable tags at all, but the name usually tells
the whole story: ``01. 郁可唯 - 时间煮雨.flac`` sitting in
``邓紫棋 - 新的心跳/CD1``. This module extracts title / artist / album /
track / disc from those names.

Rules of engagement
-------------------
* Only fields the file's own tags are missing are ever filled in (the caller
  checks that); a correct embedded tag always wins.
* Splitting is deliberately conservative: an ASCII hyphen only counts as the
  artist/title separator when it is spaced or full-width, so ``Rolling-In-Deep``
  or ``AC DC - Highway to Hell``'s ``AC DC`` stay intact and a hyphenated title
  is never torn in half.
* Quality decorations (``[FLAC 24bit/96kHz]``, ``【320Kbps】``, ``[网易无损]``)
  are stripped; meaningful ones (``(Live)``, ``(Demo)``) are kept.
"""

from __future__ import annotations

import re
from pathlib import Path

# A spaced dash in any of its usual shapes. Full-width dashes split even
# without spaces (Chinese naming convention), an ASCII hyphen only with spaces.
_DASH = re.compile(r"\s+(?:-\s+|–|—|－)|[–—－]\s*")

# Leading track number: "01.", "3-", "12 ", "[03]", "01/12", "第3首".
# A digit run only counts as a track when it is separated from real text;
# "2001太空漫游" must not lose its "200".
_TRACK = re.compile(
    r"^\s*(?:\[|【|（)?(?:第)?(\d{1,3})(?:\s*[/\-]\s*\d{1,3})?(?:\]|\】|\）)?"
    r"([.、．)）\-_－–—]|首)?\s*(?=\D)")
# ... but not when what follows is another spec word: "24 bit", "96 kHz".
_TRACK_LOOKAHEAD = re.compile(r"^(?:bit|kbps|khz|hz|flac|ape|wav)\b", re.IGNORECASE)

# Decorations that describe the *container*, never the song.
_QUALITY = re.compile(
    r"flac|ape|wav|aiff?|dsf|dff|mqa|m4a|mp3|aac|opus|ogg(?:ra)?|wma|"
    r"\d{2,4}\s*[- ]?\s*kbps|16\s*bit|24\s*bit|32\s*bit|\d{2,3}[.,]\d?\s*kh?z|"
    r"hi[- ]?res(?:ution)?|无损|超清 Sq|高清|立体声| surround|"
    r"网易云音乐|网易云|qq音乐|酷狗|酷我|kgm|mgg|mgk|"
    r"official\s*(?:mv|audio|lyric|video)?|动态歌词|歌词版?|lyrics?\s*version|"
    r"explicit|音频|音乐(?:版)?|伴奏",
    re.IGNORECASE,
)
_BRACKET = re.compile(r"[\[【(（{]([^\]】)）}]*)[\]】)）}]")

# Folder names that carry no album information.
_GENERIC_FOLDERS = {
    "music", "audio", "songs", "tracks", "album", "albums", "downloads",
    "download", "temp",
    "tmp", "新建文件夹", "未命名", "歌曲", "音乐", "下载", "音频", "flac", "mp3",
    # Short hand-made folders ("D:\x\in") say nothing about the album either.
    "in", "input", "out", "output", "src", "source", "data", "media", "files",
}

# Digits in a folder name are property numbers, not albums: "in12", "CD-1",
# "2026", "upload_3". A leading digit is too common in real titles ("7 Days",
# "99 Problems"), so it only counts the other way round: letters *followed* by
# digits, or a digit-led name with no real word after it.
_LOOKS_LIKE_CODE = re.compile(
    r"^(?:[^\W\d_]{2,3}[\s._-]?\d+[^\W\d_]{0,3}"
    r"|\d+[\s._-]?[^\W\d_]{0,3})$")
_YEAR = re.compile(r"[\[【(（]?\s*((?:19|20)\d{2})\s*(?:年)?[\]】)）]?")
_DISC_DIR = re.compile(r"^(?:cd|disc|磁盘)\s*(\d{1,2})$", re.IGNORECASE)


def _strip_decorations(stem: str) -> str:
    """Remove bracketed quality/source watermarks, keep meaningful brackets."""

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        return "" if _QUALITY.search(inner) else m.group(0)

    cleaned = _BRACKET.sub(repl, stem)
    # Unbracketed trailing quality strings: "稻香 320kbps", "歌名 FLAC".
    cleaned = re.sub(
        r"(?:\s+|_)(?:320|256|192|128|96)\s*kbps$|\s+flac$|\s+ape$|\s+m4a$",
        "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ._-－–—·•")


def _split_artist_title(text: str) -> tuple[str, str]:
    """``Artist - Title`` when the dash is a real separator, else title only."""
    m = _DASH.search(text)
    if not m:
        return "", text
    left, right = text[: m.start()].strip(" \t_-－–—·"), text[m.end():].strip(" \t_-－–—·")
    # A one-character side is a hyphen inside a name, not a separator.
    if len(left) < 2 or len(right) < 1:
        return "", text
    # "1 - Song" is a track number, not an artist; the leading-number rule
    # above runs first, so seeing a pure number here means it is not a track.
    if left.isdigit():
        return "", text
    return left, right


def _clean(value: str) -> str:
    return re.sub(r"\s{2,}", " ", value).strip()


def _folder_album(folder: Path, artist: str, has_track: bool) -> tuple[str, str]:
    """``(album, artist_taken_from_folder)``; the second is "" unless the
    folder clearly names an artist rather than an album."""
    name = folder.name
    if not name or name.lower() in _GENERIC_FOLDERS or name in _GENERIC_FOLDERS:
        return "", ""
    if _DISC_DIR.match(name):                      # .../周杰伦 - 叶惠美/CD1/
        return _folder_album(folder.parent, artist, has_track)
    if _is_root(folder):
        return "", ""
    cleaned = _strip_decorations(name)
    # "Artist - Album" folders: trust the split when the track has no artist,
    # or when its artist matches; a mismatching artist means the folder may
    # just be some unrelated path segment, so take nothing.
    left, right = _split_artist_title(cleaned)
    if left and right:
        if not artist or _norm(left) == _norm(artist):
            right = _YEAR.sub("", right).strip(" -－–—·")
            return _clean(right), _clean(left)
        return "", ""
    year = _YEAR.search(cleaned)
    if year:
        cleaned = _clean(_YEAR.sub("", cleaned).strip(" -－–—·"))
    if len(cleaned) < 2:
        return "", ""
    if _LOOKS_LIKE_CODE.match(cleaned):
        # "in12", "CD-1", "2026": a code, not a release title.
        return "", ""
    if artist and _norm(cleaned) == _norm(artist):
        return "", ""
    if not artist and not has_track:
        # An undecorated folder with un-numbered tracks reads as an artist
        # folder (``郁可唯/时间煮雨.flac``); numbered tracks read as an album
        # (``叶惠美/01 以父之名.flac``). Guessing the artist is the safer
        # default when the evidence is thin.
        return "", cleaned
    return cleaned, ""


def _is_root(folder: Path) -> bool:
    try:
        return folder.parent == folder          # drive root or filesystem root
    except OSError:
        return True


def _norm(s: str) -> str:
    return re.sub(r"[\s_－–—-]", "", s).casefold()


def parse_path(path: Path) -> dict:
    """Metadata guessed from the file and its folder names.

    Returns a dict containing only confidently identified fields among
    ``title`` / ``artist`` / ``album`` / ``album_artist`` / ``track`` / ``disc``.
    """
    out: dict = {}
    stem = _clean(_strip_decorations(path.stem))
    track_no = None

    m = _TRACK.match(stem)
    if m:
        rest = stem[m.end():].strip(" \t._-－–—·")
        # "24 bit depth"-style leads are title material, not track numbers;
        # neither is a bare big number ("99 Problems" — no punctuation and
        # above any plausible track count).
        if not _TRACK_LOOKAHEAD.match(rest) and not (not m.group(2) and int(m.group(1)) > 30):
            track_no = m.group(1)
            stem = rest

    artist, title = _split_artist_title(stem)
    if title:
        out["title"] = _clean(title)
    if artist:
        out["artist"] = _clean(artist)
        out["album_artist"] = _clean(artist)

    folder = path.parent
    disc = _DISC_DIR.match(folder.name)
    if disc:
        out["disc"] = disc.group(1)
        folder = folder.parent
    album, folder_artist = _folder_album(folder, artist, bool(track_no))
    if album:
        out["album"] = album
    if folder_artist and "artist" not in out:
        out["artist"] = folder_artist
        out["album_artist"] = folder_artist

    if track_no:
        out["track"] = str(int(track_no))
    return out
