"""Planning for the forced output format (--format / GUI 输出格式)."""

from pathlib import Path

import pytest

from amc.core import (MAX_MP3_KBPS, META_FIELDS, UNLIMITED_BYTES, MediaInfo,
                      Options, _FIELD_CN, _TAG_ALIASES, normalize_meta_key,
                      plan_candidates)

MB = 1024 * 1024


def make_info(codec="flac", sr=48000, bits=16, ch=2, dur=240.0,
              size=30 * MB, ext=".flac", tags=None, bitrate=0) -> MediaInfo:
    return MediaInfo(path=Path(f"sample{ext}"), codec=codec, sample_rate=sr,
                     channels=ch, bits=bits, duration=dur, size=size,
                     tags=tags or {}, streams=[], bitrate=bitrate)


# --------------------------------------------------------------------------
# MP3
# --------------------------------------------------------------------------

def test_mp3_output_from_lossless_only_encodes_mp3():
    plans = plan_candidates(make_info(), 50 * MB, out_format="mp3")
    assert plans and all(p.mode == "mp3" for p in plans)
    assert all(p.out_ext == ".mp3" for p in plans)
    assert plans[0].bitrate_kbps <= MAX_MP3_KBPS


def test_mp3_source_is_passed_through_untouched():
    info = make_info(codec="mp3", ext=".mp3", size=8 * MB, bitrate=320_000)
    plans = plan_candidates(info, 50 * MB, out_format="mp3")
    assert plans[0].mode == "copy"
    assert all(p.mode != "mp3" for p in plans)


def test_mp3_bitrate_steps_down_to_keep_the_budget():
    """libmp3lame caps at 320kbps; a tight cap must still be honoured."""
    plans = plan_candidates(make_info(dur=300.0), 8 * MB, out_format="mp3")
    rates = [p.bitrate_kbps for p in plans]
    assert rates == sorted(rates, reverse=True)
    assert rates[0] <= MAX_MP3_KBPS
    assert plans[-1].bitrate_kbps == 64
    assert plans[-1].est_bytes <= 8 * MB


def test_mp3_output_respects_the_lossy_source_cap():
    """Re-encoding a 96kbps opus to 320kbps MP3 only adds bytes."""
    info = make_info(codec="opus", ext=".opus", size=2 * MB, bitrate=96_000)
    plans = plan_candidates(info, UNLIMITED_BYTES, out_format="mp3")
    assert plans[0].bitrate_kbps <= 144


# --------------------------------------------------------------------------
# AAC
# --------------------------------------------------------------------------

def test_aac_output_skips_the_alac_ladder():
    plans = plan_candidates(make_info(), 50 * MB, out_format="aac")
    assert [p.mode for p in plans] == ["aac"]
    assert plans[0].out_ext == ".m4a"


def test_aac_source_is_passed_through_untouched():
    info = make_info(codec="aac", ext=".m4a", size=10 * MB)
    assert plan_candidates(info, 50 * MB, out_format="aac")[0].mode == "copy"


# --------------------------------------------------------------------------
# ALAC
# --------------------------------------------------------------------------

def test_alac_output_forces_a_lossless_ladder_on_lossy_sources():
    info = make_info(codec="mp3", ext=".mp3", size=8 * MB, bitrate=320_000)
    plans = plan_candidates(info, 50 * MB, out_format="alac")
    assert plans[0].mode == "alac"
    assert plans[-1].mode == "aac"      # still falls back when it cannot fit


def test_auto_keeps_the_quality_first_ladder():
    plans = plan_candidates(make_info(), 50 * MB, out_format="auto")
    assert plans[0].mode == "alac" and plans[-1].mode == "aac"


def test_force_alac_and_format_alac_agree():
    info = make_info(codec="mp3", ext=".mp3", size=8 * MB, bitrate=320_000)
    assert ([p.mode for p in plan_candidates(info, 50 * MB, force_alac=True)]
            == [p.mode for p in plan_candidates(info, 50 * MB, out_format="alac")])


# --------------------------------------------------------------------------
# metadata plumbing
# --------------------------------------------------------------------------

def test_every_metadata_field_is_known_to_the_tag_writer():
    for field in META_FIELDS:
        assert field in _TAG_ALIASES


def test_manual_metadata_is_empty_by_default():
    opts = Options()
    assert opts.meta == {} and opts.out_format == "auto"


@pytest.mark.parametrize("fmt", ["auto", "alac", "aac", "mp3"])
def test_every_format_produces_at_least_one_plan(fmt):
    for info in (make_info(), make_info(codec="mp3", ext=".mp3", size=3 * MB),
                 make_info(codec="alac", ext=".m4a", size=400 * MB)):
        assert plan_candidates(info, 20 * MB, out_format=fmt)


@pytest.mark.parametrize("given,expected", [
    ("artist", "artist"),
    ("Artist", "artist"),
    ("专辑", "album"),
    ("流派", "genre"),
    (" 年份 ", "date"),
    ("歌手", "artist"),
])
def test_meta_keys_accept_chinese_and_english(given, expected):
    assert normalize_meta_key(given) == expected


def test_unknown_meta_key_is_rejected():
    assert normalize_meta_key("颜色") is None
    assert normalize_meta_key("albumart") is None


def test_every_chinese_field_name_maps_back():
    for key, cn in _FIELD_CN.items():
        assert normalize_meta_key(cn) == key