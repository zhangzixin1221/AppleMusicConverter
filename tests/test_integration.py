"""End-to-end tests against a real ffmpeg.

These cover the behaviour the README actually promises -- embedded art and
lyrics, an honoured size budget, sane overwrite semantics -- plus the
regressions that only show up once ffmpeg is in the picture. The whole module
skips itself when ffmpeg/ffprobe are not installed.

Where a size budget is involved the fixtures use white noise: a sine compresses
to almost nothing, so it cannot demonstrate anything about encoder planning.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from conftest import can_encode, requires_ffmpeg, run_ffmpeg

from amc import core
from amc.core import (TMP_PREFIX, AMCCancelled, AMCError, Options, convert_file)

pytestmark = [pytest.mark.integration, requires_ffmpeg]

MB = 1024 * 1024
NOISE = "anoisesrc=color=white:a=0.9:d={seconds}"


def convert(src: Path, **kwargs):
    """Convert one file, returning (result, log_lines)."""
    logs: list[str] = []
    return convert_file(src, Options(**kwargs), logs.append), logs


def staging_leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.startswith(TMP_PREFIX)]


def mp4_tags(path: Path) -> dict:
    from mutagen.mp4 import MP4
    return MP4(str(path)).tags


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_flac_becomes_alac_with_cover_and_lyrics(media):
    src = media.tone("song.flac", tags={"title": "时间煮雨", "artist": "郁可唯",
                                        "album": "测试专辑", "track": "3/12"})
    media.art("cover.jpg")
    media.lyrics("song.lrc", "[ti:时间煮雨]\n[00:01.00]第一行\n[00:03.00]第二行\n")

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.plan.mode == "alac"
    assert res.lyrics_from.startswith("外挂歌词")
    assert res.cover_from.startswith("外挂图片")

    tags = mp4_tags(res.dest)
    assert tags["\xa9nam"] == ["时间煮雨"]
    assert tags["\xa9ART"] == ["郁可唯"]
    assert tags["\xa9lyr"] == ["第一行\n第二行"]      # plain text, no timestamps
    assert bytes(tags["covr"][0]).startswith(b"\xff\xd8\xff")
    assert res.warnings == []
    assert staging_leftovers(media.dir) == []


def test_hires_lossless_keeps_its_depth_when_the_budget_allows(media):
    src = media.tone("hires.wav", codec="pcm_s32le", rate=96000, channels=2)
    res, _ = convert(src, outdir=media.dir, target_mb=100)
    assert res.ok, res.error
    assert (res.plan.mode, res.plan.sample_rate, res.plan.bits) == ("alac", 96000, 32)


def test_target_mb_zero_means_no_limit(media):
    src = media.tone("hires.wav", codec="pcm_s32le", rate=96000, channels=2)
    res, _ = convert(src, outdir=media.dir, target_mb=0)
    assert res.ok, res.error
    assert not any("超出目标" in w for w in res.warnings)
    assert res.plan.mode == "alac" and res.plan.sample_rate == 96000


# --------------------------------------------------------------------------
# regression: the budget has to cover the finished file, art included
# --------------------------------------------------------------------------

def test_size_budget_is_honoured_including_cover_art(media):
    """A ~150KB cover used to ride on top of the cap: a 0.5MB target produced a
    0.53MB file, with no warning, because only the untagged audio was measured."""
    src = media.tone("noise.flac", channels=2, seconds=3.0, source=NOISE.format(seconds=3))
    media.noise_jpeg("cover.jpg", size=1600)
    media.lyrics("noise.lrc", "测试歌词\n第二行\n")

    target = 0.5
    res, _ = convert(src, outdir=media.dir, target_mb=target)

    assert res.ok, res.error
    assert res.size <= target * MB
    assert not any("仍超出目标" in w for w in res.warnings)
    assert mp4_tags(res.dest).get("covr"), "封面必须真的内嵌进最终文件"


def test_an_optimistic_estimate_is_caught_by_measuring_the_real_file(media, monkeypatch):
    """The README promises a measured guarantee, not an estimate.

    Real encoders rarely exceed the feasibility bound used for planning, so the
    gate is deliberately forced open here: every ALAC rung is offered, the
    finished files are too big, and the run has to fall through to the next rung
    on the strength of the bytes actually written to disk.
    """
    src = media.tone("noise.wav", codec="pcm_s16le", channels=2, seconds=3.0,
                     source=NOISE.format(seconds=3))
    monkeypatch.setattr(core, "alac_might_fit", lambda *a, **k: True)
    target = 0.2

    res, logs = convert(src, outdir=media.dir, target_mb=target)

    assert res.ok, res.error
    assert sum("超出预算，降级重试" in line for line in logs) >= 2
    assert res.plan.mode == "aac", "ALAC 全部实测超标后必须退回 AAC"
    assert res.size <= target * MB


# --------------------------------------------------------------------------
# regression: overwrite semantics
# --------------------------------------------------------------------------

def test_second_run_replaces_instead_of_piling_up_copies(media):
    """--overwrite used to be dead code: every re-run made a new ' (2)' file."""
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    out = media.dir / "out"
    out.mkdir()

    first, _ = convert(src, outdir=out, target_mb=50)
    second, _ = convert(src, outdir=out, target_mb=50)

    assert first.ok and second.ok
    assert second.dest == first.dest
    assert [p.name for p in out.iterdir()] == [first.dest.name]


def test_no_overwrite_reports_the_conflict_and_keeps_the_first_copy(media):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    out = media.dir / "out"
    out.mkdir()

    first, _ = convert(src, outdir=out, target_mb=50)
    original = first.dest.read_bytes()

    second, _ = convert(src, outdir=out, target_mb=50, overwrite=False)

    assert not second.ok
    assert "已存在" in second.error
    assert second.dest is None
    assert first.dest.read_bytes() == original
    assert [p.name for p in out.iterdir()] == [first.dest.name]


def test_in_place_reencode_diverts_instead_of_destroying_the_source(media):
    """Re-encoding over the file being read would destroy the only copy."""
    src = media.tone("song.m4a", codec="aac", bitrate="192k", seconds=3.0,
                     tags={"artist": "A", "title": "T"})
    original = src.read_bytes()

    res, _ = convert(src, target_mb=50, keep_name=True, force_alac=True)

    assert res.ok, res.error
    assert res.dest != src and src.read_bytes() == original
    assert res.dest.name == "song (2).m4a"
    assert any("与源文件同名" in w for w in res.warnings)


def test_retagging_a_native_file_in_place_is_allowed(media):
    """A passthrough only rewrites tags, so replacing the source is safe."""
    src = media.tone("song.m4a", codec="aac", bitrate="192k", seconds=3.0,
                     tags={"artist": "A", "title": "T"})
    res, _ = convert(src, target_mb=50, keep_name=True)
    assert res.ok, res.error
    assert res.plan.mode == "copy" and res.dest == src


# --------------------------------------------------------------------------
# regression: output is written atomically
# --------------------------------------------------------------------------

def test_a_failed_encode_never_damages_the_existing_output(media, monkeypatch):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    out = media.dir / "out"
    out.mkdir()
    existing = out / "A - T.m4a"
    existing.write_bytes(b"PREVIOUS GOOD OUTPUT")

    def boom(info, plan, dest, cancel=None):
        Path(dest).write_bytes(b"half written garbage")   # a truncated encode
        raise AMCError("simulated encoder failure")

    monkeypatch.setattr(core, "encode_audio", boom)
    res, _ = convert(src, outdir=out, target_mb=50)

    assert not res.ok and "simulated encoder failure" in res.error
    assert existing.read_bytes() == b"PREVIOUS GOOD OUTPUT"
    assert staging_leftovers(out) == []


def test_cancellation_aborts_without_leaving_anything(media):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(AMCCancelled):
        convert_file(src, Options(outdir=media.dir), lambda m: None, cancel=cancel)

    assert staging_leftovers(media.dir) == []
    assert not [p for p in media.dir.iterdir() if p.suffix == ".m4a"]


# --------------------------------------------------------------------------
# artwork
# --------------------------------------------------------------------------

def test_webp_sidecar_is_found_and_embedded_as_jpeg(media):
    """WebP covers were not even looked for, and any payload was declared JPEG."""
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    media.art("cover.webp", size=600)

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.cover_from == "外挂图片 cover.webp"
    from mutagen.mp4 import MP4Cover
    cover = mp4_tags(res.dest)["covr"][0]
    assert bytes(cover).startswith(b"\xff\xd8\xff")
    assert cover.imageformat == MP4Cover.FORMAT_JPEG


def test_embedded_cover_is_reused(media):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    media.art("cover.jpg")
    tagged = media.dir / "tagged.flac"
    run_ffmpeg("-i", src, "-i", media.dir / "cover.jpg", "-map", "0:a", "-map", "1:v",
               "-c:a", "copy", "-c:v", "copy", "-disposition:v", "attached_pic", tagged)

    res, _ = convert(tagged, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.cover_from == "内嵌封面"
    assert bytes(mp4_tags(res.dest)["covr"][0]).startswith(b"\xff\xd8\xff")


def test_unreadable_sidecar_art_is_reported_not_embedded(media):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    (media.dir / "cover.jpg").write_bytes(b"this is not an image")

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.cover_from == "无" and "未找到封面" in res.warnings
    assert not mp4_tags(res.dest).get("covr")


def test_artwork_bigger_than_the_whole_budget_is_explained(media):
    """No encoder can make a 150KB cover fit inside a 40KB cap; the run has to
    say so rather than just emit an oversized file."""
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    media.noise_jpeg("cover.jpg", size=1600)
    assert (media.dir / "cover.jpg").stat().st_size > 40 * 1024

    res, _ = convert(src, outdir=media.dir, target_mb=0.04)

    assert res.ok, res.error
    assert any("已超过" in w and "上限" in w for w in res.warnings)
    assert any("仍超出目标" in w for w in res.warnings)


# --------------------------------------------------------------------------
# lyrics
# --------------------------------------------------------------------------

@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "gb18030"])
def test_lyric_sidecar_encodings_round_trip(media, encoding):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T"})
    media.lyrics("song.lrc", "[00:01.00]第一行\n[00:02.00]第二行\n", encoding=encoding)

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert mp4_tags(res.dest)["\xa9lyr"] == ["第一行\n第二行"]


def test_embedded_lyrics_win_over_a_sidecar(media):
    src = media.tone("song.flac", tags={"artist": "A", "title": "T",
                                        "LYRICS": "[00:01.00]内嵌歌词"})
    media.lyrics("song.lrc", "外挂歌词")

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.lyrics_from == "内嵌标签"
    assert mp4_tags(res.dest)["\xa9lyr"] == ["内嵌歌词"]


# --------------------------------------------------------------------------
# Apple-native sources
# --------------------------------------------------------------------------

@pytest.mark.skipif(not can_encode("libmp3lame"), reason="ffmpeg 缺少 MP3 编码器")
def test_mp3_passthrough_writes_id3v23(media):
    src = media.tone("song.mp3", codec="libmp3lame", bitrate="192k", seconds=3.0,
                     tags={"title": "标题", "artist": "歌手", "album": "专辑"})
    media.art("cover.jpg")
    media.lyrics("song.lrc", "[00:01.00]第一行\n[00:02.00]第二行\n")

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.plan.mode == "copy" and res.dest.suffix == ".mp3"

    from mutagen.id3 import ID3
    tags = ID3(str(res.dest))
    assert tags.version == (2, 3, 0), "iTunes 对 ID3v2.3 的兼容性最好"
    assert str(tags["TIT2"]) == "标题"
    assert tags.getall("APIC") and tags.getall("USLT")
    assert tags.getall("USLT")[0].text == "第一行\n第二行"


@pytest.mark.skipif(not can_encode("aac"), reason="ffmpeg 缺少 AAC 编码器")
def test_aac_in_an_mp3_named_file_is_repackaged(media):
    """The extension lies about the payload, so a straight copy would be wrong."""
    src = media.dir / "song.mp3"
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2",
               "-c:a", "aac", "-b:a", "128k", "-f", "adts", src)

    res, _ = convert(src, outdir=media.dir, target_mb=50)

    assert res.ok, res.error
    assert res.plan.mode == "remux" and res.dest.suffix == ".m4a"


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_corrupt_input_fails_cleanly(media):
    bad = media.dir / "broken.flac"
    bad.write_bytes(b"definitely not audio, not even close")

    res, _ = convert(bad, outdir=media.dir, target_mb=50)

    assert not res.ok and res.error
    assert staging_leftovers(media.dir) == []


def test_tag_repair_can_be_turned_off(media):
    """The escape hatch for a heuristic that guessed wrong on someone's tags."""
    mangled = "郁可唯".encode("gb18030").decode("latin-1")
    assert mangled != "郁可唯"
    src = media.tone("song.flac", tags={"artist": mangled, "title": "T"})
    on, off = media.dir / "on", media.dir / "off"
    on.mkdir()
    off.mkdir()

    repaired, _ = convert(src, outdir=on, target_mb=50)
    kept, _ = convert(src, outdir=off, target_mb=50, repair_tags=False)

    assert mp4_tags(repaired.dest)["\xa9ART"] == ["郁可唯"]
    assert mp4_tags(kept.dest)["\xa9ART"] == [mangled]
