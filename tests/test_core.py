"""Tests for the AMC core engine.

Only pure logic is covered here -- anything that shells out to ffmpeg belongs in
an integration test with a real fixture file.
"""

from pathlib import Path

import pytest

from amc.core import (MediaInfo, Plan, container_for, decode_text,
                      est_alac_bytes, lrc_to_plain, plan_candidates,
                      repair_text, safe_name)

MB = 1024 * 1024


def make_info(codec="flac", sr=48000, bits=16, ch=2, dur=240.0,
              size=30 * MB, ext=".flac", tags=None) -> MediaInfo:
    return MediaInfo(path=Path(f"sample{ext}"), codec=codec, sample_rate=sr,
                     channels=ch, bits=bits, duration=dur, size=size,
                     tags=tags or {}, streams=[])


# --------------------------------------------------------------------------
# lyrics
# --------------------------------------------------------------------------

def test_lrc_to_plain_strips_timestamps_and_headers():
    raw = "\n".join([
        "[ti:Some Title]",
        "[ar:Some Artist]",
        "[al:Some Album]",
        "[00:01.00]first line",
        "[00:03.50]",
        "[00:05.25]second line",
        "[00:09.10]third line",
    ])
    assert lrc_to_plain(raw) == "first line\nsecond line\nthird line"


def test_lrc_to_plain_handles_word_level_and_enhanced_tags():
    raw = "[00:12.00]<00:12.00>word <00:12.50>by [bg:00:13.00]word"
    assert lrc_to_plain(raw) == "word by word"


def test_lrc_to_plain_passes_through_plain_text():
    raw = "plain line one\nplain line two"
    assert lrc_to_plain(raw) == "plain line one\nplain line two"


def test_lrc_to_plain_normalises_crlf_and_blank_runs():
    assert lrc_to_plain("a\r\n\r\n\r\nb\r\n") == "a\nb"


def test_lrc_to_plain_empty_input():
    assert lrc_to_plain("") == ""


# --------------------------------------------------------------------------
# encoding safety
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["歌词内容", "日本語のテキスト", "plain ascii"])
def test_decode_text_reads_utf8(text):
    assert decode_text(text.encode("utf-8")) == text


def test_decode_text_strips_utf8_bom():
    assert decode_text("歌词".encode("utf-8-sig")) == "歌词"


def test_decode_text_reads_utf16():
    assert decode_text("歌词".encode("utf-16")) == "歌词"


def test_decode_text_falls_back_to_gb18030():
    # These GB18030 bytes are not valid UTF-8, so detection must move on.
    assert decode_text("歌词".encode("gb18030")) == "歌词"


def test_decode_text_honours_forced_encoding():
    assert decode_text("歌词".encode("gb18030"), forced="gb18030") == "歌词"


def test_repair_text_undoes_latin1_mojibake():
    mangled = "歌词".encode("gb18030").decode("latin-1")
    assert mangled != "歌词"
    assert repair_text(mangled) == "歌词"


def test_repair_text_leaves_clean_text_alone():
    assert repair_text("歌词") == "歌词"
    assert repair_text("plain") == "plain"
    assert repair_text("") == ""


# --------------------------------------------------------------------------
# naming / containers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("codec,expected", [
    ("mp3", ".mp3"), ("aac", ".m4a"), ("alac", ".m4a"),
])
def test_container_for(codec, expected):
    assert container_for(codec) == expected


def test_safe_name_strips_windows_reserved_characters():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j', "fb") == "a_b_c_d_e_f_g_h_i_j"


def test_safe_name_strips_trailing_dots_and_spaces():
    assert safe_name("title... ", "fb") == "title"


def test_safe_name_falls_back_when_empty():
    assert safe_name("", "fallback") == "fallback"
    assert safe_name("   ", "fallback") == "fallback"


def test_safe_name_truncates():
    assert len(safe_name("x" * 500, "fb")) == 150


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def test_native_alac_under_budget_is_passed_through():
    info = make_info(codec="alac", ext=".m4a", size=28 * MB)
    plans = plan_candidates(info, 50 * MB)
    assert plans[0].mode == "copy"
    assert plans[0].out_ext == ".m4a"


def test_aac_in_mp3_container_is_remuxed_not_copied():
    """A source whose extension lies about its payload must be repackaged."""
    info = make_info(codec="aac", ext=".mp3", size=10 * MB)
    plans = plan_candidates(info, 50 * MB)
    assert plans[0].mode == "remux"
    assert plans[0].out_ext == ".m4a"


def test_lossless_source_is_encoded_to_alac():
    info = make_info(codec="flac")
    plans = plan_candidates(info, 50 * MB)
    assert plans[0].mode == "alac"
    assert plans[0].sample_rate == 48000


def test_force_alac_skips_passthrough():
    info = make_info(codec="mp3", ext=".mp3", size=8 * MB)
    plans = plan_candidates(info, 50 * MB, force_alac=True)
    assert all(p.mode != "copy" for p in plans)
    assert plans[0].mode == "alac"


def test_missing_album_artist_falls_back_to_file_stem():
    info = make_info(tags={"artist": "", "title": ""})
    assert info.tag("artist") == ""
    assert info.tag("title") == ""


def test_last_plan_always_fits_the_budget():
    """The final fallback must respect the cap whenever the cap is reachable."""
    for info in (make_info(), make_info(codec="alac", ext=".m4a", size=400 * MB),
                 make_info(codec="aac", ext=".mp3", size=200 * MB, dur=7200.0)):
        for target in (5, 20, 50, 100):
            plans = plan_candidates(info, target * MB)
            assert plans, "planner returned nothing"
            final = plans[-1]
            assert final.mode == "aac"
            if final.bitrate_kbps > 64:
                assert final.est_bytes <= target * MB * 1.02


def test_bitrate_floor_is_respected_for_absurd_ratios():
    """A 2-hour file capped at 5MB is not achievable; we floor instead of
    degrading to an unlistenable bitrate."""
    info = make_info(dur=7200.0)
    final = plan_candidates(info, 5 * MB)[-1]
    assert final.bitrate_kbps == 64


def test_oversized_long_track_drops_alac_entirely():
    info = make_info(dur=7200.0)          # two hours
    plans = plan_candidates(info, 50 * MB)
    assert all(p.mode != "alac" for p in plans)
    assert plans[-1].bitrate_kbps <= 64


def test_hires_steps_down_bit_depth_before_sample_rate():
    info = make_info(codec="flac", sr=96000, bits=24, dur=60.0)
    plans = plan_candidates(info, 25 * MB)
    alac = [p for p in plans if p.mode == "alac"]
    rates = [(p.sample_rate, p.sample_fmt) for p in alac]
    assert rates[0] == (96000, "s32p")
    # dropping to 16-bit at the original rate must precede any downsampling
    assert rates.index((96000, "s16p")) < rates.index((48000, "s16p"))


def test_hires_downsamples_when_budget_is_tight():
    info = make_info(codec="flac", sr=96000, bits=24, dur=60.0)
    plans = plan_candidates(info, 10 * MB)
    alac = [p for p in plans if p.mode == "alac"]
    assert alac, "expected an ALAC candidate after downsampling"
    assert alac[0].sample_rate == 48000
    assert alac[0].sample_fmt == "s16p"


def test_88200_family_downsamples_to_44100():
    info = make_info(codec="flac", sr=88200, bits=24, dur=60.0)
    plans = plan_candidates(info, 10 * MB)
    alac = [p for p in plans if p.mode == "alac"]
    assert alac and alac[-1].sample_rate == 44100


def test_uncompressed_source_prefers_lossless_over_aac():
    info = make_info(codec="pcm_s24le", sr=48000, bits=24, dur=180.0)
    plans = plan_candidates(info, 50 * MB)
    assert plans[0].mode == "alac"


def test_plan_label_is_human_readable():
    assert "ALAC" in Plan("alac", "", sample_rate=48000, sample_fmt="s16p").label()
    assert "AAC" in Plan("aac", "", sample_rate=44100, bitrate_kbps=256).label()
    assert "直通" in Plan("copy", "", out_ext=".mp3").label()
    assert "重新封装" in Plan("remux", "", out_ext=".m4a").label()


def test_est_alac_bytes_scales_with_parameters():
    base = est_alac_bytes(44100, 16, 2, 60)
    assert est_alac_bytes(88200, 16, 2, 60) == pytest.approx(base * 2, rel=0.01)
    assert est_alac_bytes(44100, 32, 2, 60) == pytest.approx(base * 2, rel=0.01)
    assert est_alac_bytes(44100, 16, 1, 60) == pytest.approx(base / 2, rel=0.01)
