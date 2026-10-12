"""Shared test fixtures.

Unit tests exercise pure logic. Tests marked ``integration`` drive a real ffmpeg
and are skipped automatically when one is not installed, so the suite stays
green on a machine without it while still proving the end-to-end behaviour
where it can.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make the src layout importable without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amc.core import ffmpeg, ffprobe       # noqa: E402


def _available(getter) -> bool:
    try:
        getter()
        return True
    except Exception:
        return False


HAVE_FFMPEG = _available(ffmpeg) and _available(ffprobe)

# CI sets this so a missing ffmpeg fails the build instead of quietly skipping
# the entire integration suite.
if os.environ.get("AMC_REQUIRE_FFMPEG") and not HAVE_FFMPEG:
    raise RuntimeError(
        "AMC_REQUIRE_FFMPEG is set, but ffmpeg/ffprobe could not be found on PATH"
    )

requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="需要 ffmpeg / ffprobe")

_ENCODER_CACHE: dict[str, bool] = {}


def run_ffmpeg(*args: object) -> None:
    """Invoke ffmpeg with an argv list -- never a shell string."""
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *[str(a) for a in args]]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 执行失败: " + proc.stderr.decode("utf-8", "replace"))


def can_encode(codec: str) -> bool:
    """True when this ffmpeg build can actually encode ``codec``.

    Runners ship trimmed builds, so a test that needs libmp3lame has to ask
    rather than assume.
    """
    if not HAVE_FFMPEG:
        return False
    if codec not in _ENCODER_CACHE:
        cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "sine=frequency=440:duration=0.1", "-c:a", codec, "-f", "null", "-"]
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        _ENCODER_CACHE[codec] = proc.returncode == 0
    return _ENCODER_CACHE[codec]


class MediaFactory:
    """Builds real audio, artwork and lyric files for the integration tests."""

    def __init__(self, folder: Path) -> None:
        self.dir = folder

    def tone(self, name: str = "tone.flac", codec: str = "flac", seconds: float = 2.0,
             rate: int = 48000, channels: int = 1, bitrate: str | None = None,
             tags: dict | None = None, extra: list | None = None,
             source: str | None = None) -> Path:
        path = self.dir / name
        signal = source or f"sine=frequency=440:duration={seconds}"
        args: list = ["-f", "lavfi", "-i", signal,
                      "-c:a", codec, "-ar", str(rate), "-ac", str(channels)]
        if bitrate:
            args += ["-b:a", bitrate]
        if extra:
            args += list(extra)
        for key, value in (tags or {}).items():
            args += ["-metadata", f"{key}={value}"]
        run_ffmpeg(*args, path)
        return path

    def art(self, name: str = "cover.jpg", size: int = 600, quality: int = 2,
            source: str | None = None) -> Path:
        path = self.dir / name
        src = source or f"color=c=red:s={size}x{size}:d=1"
        run_ffmpeg("-f", "lavfi", "-i", src, "-frames:v", "1", "-q:v", str(quality), path)
        return path

    def lyrics(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_text(text, encoding=encoding)
        return path

    def noise_jpeg(self, name: str = "cover.jpg", size: int = 1600) -> Path:
        """A detailed image: flat colours compress to a few KB, this does not."""
        return self.art(name, size=size, quality=1, source=f"testsrc2=s={size}x{size}")


@pytest.fixture
def media(tmp_path):
    return MediaFactory(tmp_path)
