"""Tests for the argv re-gluing that repairs shell-mangled paths."""

from __future__ import annotations

import pytest

from amc.__main__ import _reglue_paths


def test_a_path_split_by_a_forgetful_shell_is_rejoined(tmp_path):
    spaced = tmp_path / "Deepseek Harness Workspace"
    spaced.mkdir()
    whole = str(spaced)
    cut = whole.index(" ")  # first space: the shell broke the path apart here
    assert _reglue_paths([whole[:cut], *whole[cut:].split()]) == [whole]


def test_unresolvable_tokens_pass_through_in_order():
    argv = ["D:\\x\\Deepseek", "Harness", "Workspace"]
    assert _reglue_paths(argv) == argv


def test_real_arguments_are_untouched(tmp_path):
    a = tmp_path / "a.flac"
    a.write_bytes(b"")
    assert _reglue_paths([str(a), "--recursive"]) == [str(a), "--recursive"]


def test_flags_never_swallow_the_next_token(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    assert _reglue_paths(["--out", str(out), "missing"]) == [
        "--out", str(out), "missing"]