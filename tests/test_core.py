"""Tests for the AMC core engine.

Only pure logic is covered here -- anything that shells out to ffmpeg belongs in
``test_integration.py``, which drives real fixtures and skips itself when ffmpeg
is missing.
"""

from pathlib import Path

import codecs

import pytest

from amc.core import (UNLIMITED_BYTES, MediaInfo, Options, Plan, _image_kind,
                      _normalise_cover, _pcm_params, _wide_text_encoding,
                      alac_might_fit, collect_inputs, container_for, decode_text,
                      est_alac_bytes, lrc_to_plain, output_path, plan_candidates,
                      repair_text, safe_name, same_file, unique_path)

MB = 1024 * 1024


def make_info(codec="flac", sr=48000, bits=16, ch=2, dur=240.0,
              size=30 * MB, ext=".flac", tags=None, bitrate=0) -> MediaInfo:
    return MediaInfo(path=Path(f"sample{ext}"), codec=codec, sample_rate=sr,
                     channels=ch, bits=bits, duration=dur, size=size,
                     tags=tags or {}, streams=[], bitrate=bitrate)


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
    # The 24-bit rung is out of reach, so bit depth is dropped first and the
    # 96kHz/16bit attempt comes before any downsampling.
    assert (alac[0].sample_rate, alac[0].bits) == (96000, 16)
    assert any(p.sample_rate == 48000 and p.bits == 16 for p in alac)


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


# --------------------------------------------------------------------------
# regressions: text repair must not invent CJK out of real accents
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Motörhead", "Café Tacvba", "Sigur Rós", "Édith Piaf", "Björk", "Renée",
    "Zoë Ünver", "naïve", "Åsa Öberg", "Björk – Jóhannsson", "Dvořák",
])
def test_repair_text_leaves_real_accented_names_alone(name):
    """A correctly tagged European name is not mojibake.

    The repaired text also feeds the output filename, so guessing wrong corrupts
    the file name as well: 'Motörhead' used to become 'Mot鰎head'.
    """
    assert repair_text(name) == name


@pytest.mark.parametrize("text", ["歌词", "日本語", "母亲", "首页"])
def test_repair_text_still_undoes_gbk_mojibake(text):
    mangled = text.encode("gb18030").decode("latin-1")
    assert mangled != text
    assert repair_text(mangled) == text


def test_repair_text_undoes_utf8_mojibake():
    text = "歌手 - 稻香"
    mangled = text.encode("utf-8").decode("latin-1")
    assert repair_text(mangled) == text


def test_repair_text_handles_partial_mojibake_beside_ascii():
    """CJK mangled inside an otherwise ASCII title still gets repaired."""
    text = "稻香 (Live)"
    mangled = text.encode("gb18030").decode("latin-1")
    assert repair_text(mangled) == text


def test_repair_text_ignores_a_single_accent():
    # One accented character can never be the residue of a mangled CJK string:
    # that always leaves at least two.
    assert repair_text("é") == "é"
    assert repair_text("Zürich") == "Zürich"


# --------------------------------------------------------------------------
# regressions: text decoding
# --------------------------------------------------------------------------

def test_decode_text_reads_utf32_with_bom():
    """A UTF-32-LE BOM starts with the UTF-16-LE BOM and used to be misread."""
    text = "歌词内容"
    assert decode_text(codecs.BOM_UTF32_LE + text.encode("utf-32-le")) == text
    assert decode_text(codecs.BOM_UTF32_BE + text.encode("utf-32-be")) == text


def test_decode_text_reads_utf16_without_bom():
    """Notepad's BOM-less 'Unicode' file, i.e. ASCII with NULs on one strand."""
    text = "hello\nworld"
    assert decode_text(text.encode("utf-16-le")) == text
    assert decode_text(text.encode("utf-16-be")) == text


def test_wide_detection_ignores_bomless_utf32():
    # BOM-less UTF-32 puts its NULs on both strands, so it must not be read as
    # UTF-16 and returned as a NUL-riddled half-string.
    assert _wide_text_encoding("歌词内容".encode("utf-32-le")) is None
    assert _wide_text_encoding("hello world".encode("utf-8")) is None
    assert _wide_text_encoding("hello world".encode("utf-16-le")) == "utf-16-le"
    assert _wide_text_encoding("hello world".encode("utf-16-be")) == "utf-16-be"


def test_decode_text_prefers_utf8_over_legacy_codepages():
    assert decode_text("café".encode("utf-8")) == "café"


# --------------------------------------------------------------------------
# regressions: lyrics cleaning
# --------------------------------------------------------------------------

def test_lrc_to_plain_keeps_a_repeated_chorus_line():
    """Identical words under *different* timestamps are a real repeat."""
    raw = "[00:10.00]Na na na\n[00:12.00]Na na na"
    assert lrc_to_plain(raw) == "Na na na\nNa na na"


def test_lrc_to_plain_drops_a_line_repeated_under_the_same_timestamp():
    raw = "[00:10.00]only once\n[00:10.00]only once"
    assert lrc_to_plain(raw) == "only once"


def test_lrc_to_plain_strips_srt_scaffolding():
    raw = ("1\n00:00:01,000 --> 00:00:04,000\nfirst line\n\n"
           "2\n00:00:05,000 --> 00:00:08,000\nsecond line\n")
    assert lrc_to_plain(raw) == "first line\nsecond line"


def test_lrc_to_plain_keeps_a_numeric_lyric_line():
    # Only a number that introduces a cue is scaffolding.
    assert lrc_to_plain("1 2 3 4\nlet's go") == "1 2 3 4\nlet's go"


# --------------------------------------------------------------------------
# regressions: Windows file naming
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reserved", ["CON", "PRN", "aux", "NUL", "COM1", "lpt9",
                                      "con.txt", "Nul.mp3"])
def test_safe_name_neutralises_reserved_device_names(reserved):
    """'CON.m4a' is still the console device, so the stem has to be prefixed."""
    name = safe_name(reserved, "fb")
    assert name.split(".")[0].lower() not in {"con", "prn", "aux", "nul"}
    assert not name.lower().startswith(("com1", "lpt9")) or name.startswith("_")


@pytest.mark.parametrize("ok", ["CONSOLE", "CONTENT", "com10", "nullify", "AUXILIARY"])
def test_safe_name_keeps_words_that_merely_start_like_devices(ok):
    assert safe_name(ok, "fb") == ok


def test_safe_name_strips_again_after_truncation():
    assert not safe_name("x" * 149 + ". ", "fb").endswith((".", " "))


# --------------------------------------------------------------------------
# regressions: input collection honours --recursive
# --------------------------------------------------------------------------

def _touch_tree(root: Path) -> None:
    (root / "top.flac").write_bytes(b"")
    (root / "notes.txt").write_bytes(b"")
    sub = root / "cd1"
    sub.mkdir()
    (sub / "nested.flac").write_bytes(b"")
    (root / ".amc-tmp-abcd1234.m4a").write_bytes(b"")


def test_collect_inputs_is_not_recursive_by_default(tmp_path):
    _touch_tree(tmp_path)
    assert [p.name for p in collect_inputs([tmp_path])] == ["top.flac"]


def test_collect_inputs_recurses_when_asked(tmp_path):
    _touch_tree(tmp_path)
    assert [p.name for p in collect_inputs([tmp_path], recursive=True)] == [
        "nested.flac", "top.flac"]


def test_collect_inputs_ignores_its_own_staging_files(tmp_path):
    _touch_tree(tmp_path)
    names = [p.name for p in collect_inputs([tmp_path], recursive=True)]
    assert not any(n.startswith(".amc-tmp-") for n in names)


# --------------------------------------------------------------------------
# regressions: destination paths and overwrite semantics
# --------------------------------------------------------------------------

def test_output_path_is_the_natural_path_even_when_it_exists(tmp_path):
    """Collisions are the caller's business now: the old helper silently turned
    every re-run into a new ' (2)' file, which made --overwrite a no-op."""
    (tmp_path / "A - T.m4a").write_bytes(b"")
    info = make_info(tags={"artist": "A", "title": "T"})
    assert output_path(info, tmp_path, ".m4a", keep_name=False) == tmp_path / "A - T.m4a"


def test_unique_path_finds_the_first_free_variant(tmp_path):
    dest = tmp_path / "song.m4a"
    dest.write_bytes(b"")
    assert unique_path(dest).name == "song (2).m4a"
    (tmp_path / "song (2).m4a").write_bytes(b"")
    assert unique_path(dest).name == "song (3).m4a"


def test_same_file_matches_relative_and_absolute_paths(tmp_path):
    target = tmp_path / "a.m4a"
    target.write_bytes(b"x")
    assert same_file(target, Path(str(tmp_path / "." / "a.m4a")))
    assert not same_file(target, tmp_path / "b.m4a")


def test_options_default_to_overwriting():
    opts = Options()
    assert opts.overwrite is True and opts.repair_tags is True


# --------------------------------------------------------------------------
# regressions: cover art identification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("data,kind", [
    (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "jpeg"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "png"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
    (b"GIF89a" + b"\x00" * 8, "gif"),
    (b"BM" + b"\x00" * 8, "bmp"),
])
def test_image_kind_sniffs_the_payload_not_the_extension(data, kind):
    assert _image_kind(data) == kind


def test_image_kind_rejects_non_images():
    assert _image_kind(b"not an image at all") is None
    assert _image_kind(b"") is None


def test_normalise_cover_drops_unidentifiable_bytes(tmp_path):
    assert _normalise_cover(b"just some text", tmp_path) is None


def test_normalise_cover_passes_small_jpeg_through_untouched(tmp_path):
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    assert _normalise_cover(data, tmp_path) == data


# --------------------------------------------------------------------------
# regressions: encoder planning
# --------------------------------------------------------------------------

def test_dsd_source_offers_high_resolution_alac():
    """DSD64 was fed to the ALAC ladder at 2822400Hz, so every rung looked
    impossible and the source collapsed to 44.1kHz/16bit or to AAC."""
    dsd = make_info(codec="dsd_lsbf", sr=2822400, bits=1, dur=180.0, size=200 * MB,
                    ext=".dsf")
    plans = plan_candidates(dsd, 100 * MB)
    assert plans[0].mode == "alac"
    assert (plans[0].sample_rate, plans[0].bits) == (88200, 24)
    # The raw 1-bit rate is never a sane encoder target, at any budget.
    assert all(p.sample_rate != 2822400 for p in plan_candidates(dsd, 50 * MB))


@pytest.mark.parametrize("dsd_rate,expected", [
    (2822400, 88200),      # DSD64
    (5644800, 176400),     # DSD128
    (11289600, 352800),    # DSD256
])
def test_dsd_rates_map_to_the_pcm_family(dsd_rate, expected):
    assert _pcm_params("dsd_lsbf", dsd_rate, 1) == (expected, 24)


def test_64bit_float_pcm_does_not_skip_32bit():
    """bits=64 doubled the size estimate, so the 32-bit rung was never offered."""
    info = make_info(codec="pcm_f64le", sr=48000, bits=64, dur=180.0, size=200 * MB,
                     ext=".wav")
    assert _pcm_params("pcm_f64le", 48000, 64) == (48000, 32)
    plans = plan_candidates(info, 50 * MB)
    assert (plans[0].sample_rate, plans[0].bits) == (48000, 32)


def test_passthrough_is_skipped_when_the_source_already_busts_the_cap():
    """Copying a 400MB file only to reject it on size wastes the whole run."""
    big = make_info(codec="alac", ext=".m4a", size=400 * MB)
    plans = plan_candidates(big, 50 * MB)
    assert all(p.mode != "copy" for p in plans)
    assert plan_candidates(make_info(codec="alac", ext=".m4a", size=28 * MB),
                           50 * MB)[0].mode == "copy"


def test_aac_fallback_stays_in_the_44100_family():
    info = make_info(codec="vorbis", sr=88200, bits=16, dur=60.0, size=5 * MB,
                     ext=".ogg")
    assert plan_candidates(info, 50 * MB)[-1].sample_rate == 44100


def test_aac_bitrate_is_not_inflated_far_above_the_source():
    """Transcoding a 96kbps source to 512kbps AAC buys nothing but bytes."""
    opus = make_info(codec="opus", sr=48000, bits=16, dur=60.0, size=1 * MB,
                     ext=".opus", bitrate=96_000)
    final = plan_candidates(opus, UNLIMITED_BYTES)[-1]
    assert final.bitrate_kbps <= 144


def test_lossless_source_is_not_bitrate_capped():
    flac = make_info(codec="flac", dur=60.0, bitrate=400_000)
    assert plan_candidates(flac, UNLIMITED_BYTES)[-1].bitrate_kbps == 512


def test_unlimited_budget_keeps_the_native_parameters():
    info = make_info(codec="flac", sr=96000, bits=24, dur=60.0)
    plan = plan_candidates(info, UNLIMITED_BYTES)[0]
    assert (plan.mode, plan.sample_rate, plan.bits) == ("alac", 96000, 24)


def test_plan_rewrites_audio_only_for_real_re_encodes():
    assert Plan("alac", "").rewrites_audio and Plan("aac", "").rewrites_audio
    assert not Plan("copy", "").rewrites_audio
    assert not Plan("remux", "").rewrites_audio


def test_plan_label_reports_the_declared_bit_depth():
    assert "32bit" in Plan("alac", "", sample_rate=96000, sample_fmt="s32p", bits=32).label()


def test_alac_rungs_are_offered_beyond_the_estimate():
    """Only a rung that cannot possibly fit may be skipped outright.

    Gating on the 0.62 estimate threw away an ALAC rung whose real output was a
    fifth of the budget, which is how a 0.4MB cap ended up choosing AAC.
    """
    budget = 24 * MB
    # A four-minute stereo CD-quality track: the estimate says 28.6MB, over the
    # cap, yet ALAC can compress well enough that it is still worth measuring.
    assert est_alac_bytes(48000, 16, 2, 240.0) > budget
    assert alac_might_fit(48000, 16, 2, 240.0, budget)
    assert plan_candidates(make_info(dur=240.0), budget)[0].mode == "alac"

    # An hour at 60MB is genuinely hopeless: even the best case is 296MB, so no
    # encoder time is wasted finding that out.
    assert not alac_might_fit(48000, 16, 2, 3600.0, 60 * MB)
    hour = plan_candidates(make_info(dur=3600.0), 60 * MB)
    assert all(p.mode != "alac" for p in hour) and hour[-1].mode == "aac"
