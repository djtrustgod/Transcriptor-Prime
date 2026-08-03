"""Media inspection via PyAV.

PyAV's wheels bundle FFmpeg, so this handles MP3, MP4, M4A, WAV, FLAC, MKV,
MOV and friends without any system ffmpeg install.

Probing up front means an unreadable file or a video with no audio track fails
immediately on selection, rather than 30 seconds into a job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Extensions offered in the file dialog. Anything FFmpeg can demux will work;
# this is just the shortlist that covers the expected inputs.
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma")
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS


class MediaError(Exception):
    """Raised when a file cannot be opened or carries no audio."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    container: str
    audio_codec: str
    has_video: bool

    def summary(self) -> str:
        from transcriptor_prime.formatting import format_duration

        kind = "video" if self.has_video else "audio"
        return (
            f"{format_duration(self.duration)}  ·  "
            f"{self.container.upper()} / {self.audio_codec.upper()} ({kind})"
        )


def probe(path: str | Path) -> MediaInfo:
    """Read duration and stream layout. Raises :class:`MediaError` on failure."""
    import av

    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")

    try:
        container = av.open(str(path))
    except Exception as exc:  # av raises a family of errors; treat them alike
        raise MediaError(f"Could not open '{path.name}' as a media file: {exc}") from exc

    try:
        audio_streams = container.streams.audio
        if not audio_streams:
            raise MediaError(f"'{path.name}' contains no audio track.")

        stream = audio_streams[0]
        duration = _duration_seconds(container, stream)
        if duration <= 0:
            raise MediaError(
                f"Could not determine the length of '{path.name}'. "
                "The file may be truncated or still being written."
            )

        return MediaInfo(
            duration=duration,
            container=_container_name(container, path),
            audio_codec=_codec_name(stream),
            has_video=bool(container.streams.video),
        )
    finally:
        container.close()


def _codec_name(stream) -> str:
    """The codec's canonical name, e.g. ``mp3`` rather than the ``mp3float`` decoder."""
    context = getattr(stream, "codec_context", None)
    codec = getattr(context, "codec", None) if context else None
    if codec is None:
        return "unknown"
    return getattr(codec, "canonical_name", None) or codec.name or "unknown"


def _container_name(container, path: Path) -> str:
    """A display name for the container format.

    FFmpeg registers one demuxer for several formats — an .mp4 opens under
    ``mov,mp4,m4a,3gp,3g2,mj2``. Reporting the first entry would label every
    MP4 as "MOV", so fall back to the file's own extension in that case.
    """
    fmt = container.format.name if container.format else ""
    if "," in fmt:
        extension = path.suffix.lstrip(".").lower()
        if extension:
            return extension
        return fmt.split(",")[0]
    return fmt or "unknown"


def _duration_seconds(container, stream) -> float:
    """Container duration in seconds, falling back to the audio stream's own."""
    import av

    if container.duration is not None:
        return float(container.duration) / av.time_base
    if stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    return 0.0


def default_output_path(source: str | Path) -> Path:
    """``<source dir>/<source stem>.txt`` — the plan's default save location."""
    source = Path(source)
    return source.with_suffix(".txt")


def unique_path(path: str | Path) -> Path:
    """Return ``path``, or ``name (2).txt`` etc. if it is already taken.

    Avoids silently overwriting an existing transcript when the user transcribes
    the same source twice.
    """
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
