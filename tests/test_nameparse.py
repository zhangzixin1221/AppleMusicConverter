"""Tests for the filename -> metadata fallback parser."""

from __future__ import annotations

from pathlib import Path

from amc.nameparse import parse_path


def P(*parts: str) -> Path:
    return Path(*parts)


# --------------------------------------------------------------------------
# artist / title from the stem
# --------------------------------------------------------------------------

def test_chinese_artist_dash_title():
    got = parse_path(P("Music", "01. 郁可唯 - 时间煮雨.flac"))
    assert got["artist"] == "郁可唯"
    assert got["title"] == "时间煮雨"
    assert got["track"] == "1"


def test_full_width_dash_without_spaces():
    got = parse_path(P("周杰伦－稻香.mp3"))
    assert got["artist"] == "周杰伦"
    assert got["title"] == "稻香"


def test_hyphen_inside_a_title_is_not_a_separator():
    # An ASCII hyphen only splits when spaced.
    got = parse_path(P("Rolling-In-Deep.mp3"))
    assert got.get("artist", "") == ""
    assert got["title"] == "Rolling-In-Deep"


def test_artist_containing_a_hyphen():
    got = parse_path(P("AC-DC - Highway to Hell.m4a"))
    assert got["artist"] == "AC-DC"
    assert got["title"] == "Highway to Hell"


def test_no_separator_means_title_only():
    got = parse_path(P("平凡之路.wav"))
    assert got["title"] == "平凡之路"
    assert "artist" not in got


def test_parenthesised_live_is_kept_but_quality_is_stripped():
    got = parse_path(P("五月天 - 突然好想你 (Live).mp3"))
    assert got["title"] == "突然好想你 (Live)"
    got = parse_path(P("陈奕迅 - 富士山下 [FLAC 24bit 96kHz].flac"))
    assert got["artist"] == "陈奕迅"
    assert got["title"] == "富士山下"


def test_platform_watermarks_are_stripped():
    got = parse_path(P("林俊杰 - 江南【320Kbps 网易云音乐】.mp3"))
    assert got["artist"] == "林俊杰"
    assert got["title"] == "江南"


def test_trailing_bare_quality_suffix():
    got = parse_path(P("Sofia - 98 320kbps.mp3"))
    assert got["artist"] == "Sofia"
    assert got["title"] == "98"


def test_leading_track_variants():
    for name, track, title in (
        ("03.心水.wav", "3", "心水"),
        ("12 - Song Title.flac", "12", "Song Title"),
        ("[07] Overture.m4a", "7", "Overture"),
        ("02-14 夜空中最亮的星.flac", "2", "夜空中最亮的星"),
    ):
        got = parse_path(P("Album", name))
        assert got.get("track") == track, name
        assert got["title"] == title, name


# --------------------------------------------------------------------------
# album / disc from folders
# --------------------------------------------------------------------------

def test_album_from_artist_album_folder():
    got = parse_path(P(r"D:\Music", "邓紫棋 - 新的心跳", "03 泡沫.flac"))
    assert got["artist"] == "邓紫棋"
    assert got["title"] == "泡沫"
    assert got["album"] == "新的心跳"


def test_album_folder_year_stripped():
    got = parse_path(P("Radiohead - OK Computer (1997)", "01 Airbag.flac"))
    # folder == artist + album with year; the file had no artist, so the
    # folder's artist half is adopted too.
    assert got["album"] == "OK Computer"
    assert got["artist"] == "Radiohead"


def test_plain_folder_name_is_the_album():
    got = parse_path(P("叶惠美", "01 以父之名.flac"))
    assert got["album"] == "叶惠美"


def test_generic_folders_do_not_become_albums():
    got = parse_path(P(r"C:\Music", "Some Song.flac"))
    assert "album" not in got
    got = parse_path(P(r"D:\新建文件夹", "Some Song.flac"))
    assert "album" not in got


def test_disc_folder():
    got = parse_path(P("周杰伦 - 依然范特西", "CD2", "01 听妈妈的话.flac"))
    assert got["disc"] == "2"
    assert got["album"] == "依然范特西"


def test_folder_matching_artist_is_not_an_album():
    got = parse_path(P("郁可唯", "时间煮雨.flac"))
    assert "album" not in got
    assert got["title"] == "时间煮雨"


# --------------------------------------------------------------------------
# restraint: never guess what cannot be known
# --------------------------------------------------------------------------

def test_bogus_name_returns_nothing_useful():
    got = parse_path(P("Track01.flac"))
    assert got.get("artist", "") == ""


def test_artist_is_not_a_bare_number():
    # "12 - Song": the number is only a track if nothing else looks like an
    # artist; a pure-number left side is treated as title material.
    got = parse_path(P("12 - Song.flac"))
    assert "artist" not in got
