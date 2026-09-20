"""Media inspection via PyAV.

PyAV's wheels bundle FFmpeg, so this handles MP3, MP4, M4A, WAV, FLAC, MKV,
MOV and friends without any system ffmpeg install.

Probing up front means an unreadable file or a video with no audio track fails
immediately on selection, rather than 30 seconds into a job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Container, Iterable

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


def decode_clip(
    path: str | Path, start: float, duration: float, rate: int = 22050
) -> bytes:
    """A few seconds of a recording as 16-bit mono PCM, for a voice sample.

    Seeks rather than decoding from the top, so a clip from the third hour of a
    recording costs the same as one from the first minute. Raises
    :class:`MediaError` on failure.
    """
    import av
    from av.audio.resampler import AudioResampler

    path = Path(path)
    try:
        container = av.open(str(path))
    except Exception as exc:
        raise MediaError(f"Could not open '{path.name}' as a media file: {exc}") from exc

    try:
        if not container.streams.audio:
            raise MediaError(f"'{path.name}' contains no audio track.")
        stream = container.streams.audio[0]
        start = max(0.0, float(start))
        if start > 0 and stream.time_base:
            container.seek(
                int(start / stream.time_base), stream=stream, backward=True
            )

        resampler = AudioResampler(format="s16", layout="mono", rate=rate)
        wanted = int(duration * rate) * 2  # bytes: 16-bit mono
        pcm = bytearray()
        for frame in container.decode(stream):
            if frame.pts is None or frame.time is None:
                continue
            frame_end = frame.time + frame.samples / frame.sample_rate
            if frame_end <= start:
                continue  # a seek lands on the keyframe *before* the target
            for converted in resampler.resample(frame):
                data = bytes(converted.planes[0])[: converted.samples * 2]
                if not pcm and frame.time < start:
                    # Trim the part of the first frame that precedes the target.
                    skip = int((start - frame.time) * rate) * 2
                    data = data[skip:]
                pcm += data
            if len(pcm) >= wanted:
                break
        return bytes(pcm[:wanted])
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"Could not read audio from '{path.name}': {exc}") from exc
    finally:
        container.close()


def write_wav(path: str | Path, pcm: bytes, rate: int = 22050) -> None:
    """Write 16-bit mono PCM as a WAV file — the one format ``winsound`` plays."""
    import wave

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


def default_output_path(source: str | Path) -> Path:
    """``<source dir>/<source stem>.txt`` — the plan's default save location."""
    source = Path(source)
    return source.with_suffix(".txt")


def unique_path(path: str | Path, taken: Container[Path] = frozenset()) -> Path:
    """Return ``path``, or ``name (2).txt`` etc. if it is already taken.

    Avoids silently overwriting an existing transcript when the user transcribes
    the same source twice.

    ``taken`` reserves names that are not on disk *yet*. A batch plans every
    output path before a single transcript exists, so two queued sources that
    share a stem in one folder — ``talk.mp3`` and ``talk.mp4`` — would both
    resolve to ``talk.txt`` and the second run would clobber the first.
    """
    path = Path(path)
    if not path.exists() and path not in taken:
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists() and candidate not in taken:
            return candidate
        counter += 1


def media_files_in(directory: str | Path, recursive: bool = False) -> list[Path]:
    """Every file under ``directory`` with a supported extension, sorted.

    Used by the queue's "Add folder…" button. Sorting is case-insensitive so
    the queue order matches what the file manager shows.
    """
    directory = Path(directory)
    walker: Iterable[Path] = (
        directory.rglob("*") if recursive else directory.iterdir()
    )
    try:
        found = [
            entry
            for entry in walker
            if entry.suffix.lower() in SUPPORTED_EXTENSIONS and entry.is_file()
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: str(p).lower())


def same_file_key(path: str | Path) -> str:
    """A comparison key for "is this already in the queue?".

    ``Path.resolve()`` alone is not enough on Windows: it does not case-fold,
    so ``C:\\Rec\\A.MP3`` and ``c:\\rec\\a.mp3`` would queue twice.
    """
    path = Path(path)
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - a path the OS refuses to resolve
        resolved = path
    return os.path.normcase(str(resolved))
