#!/usr/bin/env python3

"""Unattended MKV/video to MP4 conversion for SABnzbd and manual runs.

Usage:
    MP4_Steward.py <input folder> <output folder> <smart=true|encode=true|remux=true>
                   subfolders=<true|false> rename=<true|false>

Common examples:
    # Encode to H.265, clean the names, and place results beside their sources.
    python3 MP4_Steward.py "/downloads/movie" "/downloads/movie" encode=true subfolders=false rename=true

    # Remux compact compatible files and encode larger/incompatible files.
    python3 MP4_Steward.py "/downloads/movie" "/downloads/movie" smart=true subfolders=false rename=true

    # Encode into a separate library, creating one cleaned subfolder per video.
    python3 MP4_Steward.py "/downloads/finished" "/media/movies" encode=true subfolders=true rename=true

    # Remux compatible tracks without re-encoding, beside the source files.
    python3 MP4_Steward.py "/downloads/movie" "/downloads/movie" remux=true subfolders=false rename=true

    # Remux to another folder while preserving the original filenames.
    python3 MP4_Steward.py "/downloads/finished" "/media/converted" remux=true subfolders=false rename=false

The original is removed only after its staged output passes validation and is
safely committed. See readme.md beside this script for complete setup, naming,
track-selection, safety, logging, and SABnzbd details.
"""

from __future__ import annotations

import builtins
from collections import deque
from dataclasses import dataclass
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGFILE = os.environ.get(
    "MP4_CONVERTER_LOG",
    os.path.join(SCRIPT_DIR, "MP4_Converter.log"),
)
LOG_MAX_BYTES = max(int(os.environ.get("MP4_LOG_MAX_BYTES", 5 * 1024 * 1024)), 1024)
LOG_BACKUP_COUNT = max(int(os.environ.get("MP4_LOG_BACKUP_COUNT", 3)), 0)
OS_NAME = platform.system()
PRINT_LOCK = threading.Lock()
ACTIVE_PROCESS_LOCK = threading.RLock()
CANCELLATION_REQUESTED = threading.Event()
ACTIVE_PROCESS: subprocess.Popen[str] | None = None
LOG_PREPARED = False
INTERACTIVE_PROGRESS = sys.stdout.isatty()
PROGRESS_INTERVAL_SECONDS = max(
    float(
        os.environ.get(
            "MP4_PROGRESS_INTERVAL_SECONDS",
            5 if INTERACTIVE_PROGRESS else 60,
        )
    ),
    1,
)
try:
    DEFAULT_SMART_TARGET_MB_PER_MINUTE = max(
        float(os.environ.get("MP4_SMART_TARGET_MB_PER_MINUTE", "25")),
        0.1,
    )
except ValueError:
    DEFAULT_SMART_TARGET_MB_PER_MINUTE = 25.0


def prepare_log() -> None:
    """Create the log directory and rotate a full log once per invocation."""
    global LOG_PREPARED
    if LOG_PREPARED or not LOGFILE:
        return
    LOG_PREPARED = True

    log_directory = os.path.dirname(os.path.abspath(LOGFILE))
    os.makedirs(log_directory, exist_ok=True)
    if not os.path.isfile(LOGFILE) or os.path.getsize(LOGFILE) < LOG_MAX_BYTES:
        return

    if LOG_BACKUP_COUNT == 0:
        with open(LOGFILE, "w", encoding="utf-8"):
            pass
        return

    for backup_index in range(LOG_BACKUP_COUNT, 1, -1):
        previous_backup = f"{LOGFILE}.{backup_index - 1}"
        next_backup = f"{LOGFILE}.{backup_index}"
        if os.path.exists(previous_backup):
            os.replace(previous_backup, next_backup)
    os.replace(LOGFILE, f"{LOGFILE}.1")


def print(*args, **kwargs):
    """Write normal messages to both the console and the persistent log."""
    with PRINT_LOCK:
        builtins.print(*args, **kwargs)

        # Carriage-return progress updates are console-only. Logging those twice a
        # second made the unattended log grow without adding useful information.
        if kwargs.get("end", "\n") != "\n" or any("\r" in str(arg) for arg in args):
            return

        try:
            prepare_log()
            separator = kwargs.get("sep", " ")
            message = separator.join(str(arg) for arg in args)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOGFILE, "a", encoding="utf-8") as log_file:
                for line in message.splitlines() or [""]:
                    builtins.print(f"{timestamp} {line}", file=log_file)
        except OSError:
            # A read-only script directory should not prevent SAB processing.
            pass


def print_progress(message: str) -> None:
    with PRINT_LOCK:
        if INTERACTIVE_PROGRESS:
            builtins.print(f"\r{message:<160}", end="", flush=True)
        else:
            # SAB captures stdout through a pipe, so force each rate-limited
            # progress update through immediately without duplicating it in the
            # converter's persistent log.
            builtins.print(f"{SYMBOL_INFO} Progress: {message}", flush=True)


def clear_progress_line() -> None:
    if not INTERACTIVE_PROGRESS:
        return
    with PRINT_LOCK:
        builtins.print("\r" + (" " * 160), end="\r", flush=True)


if OS_NAME == "Darwin":
    os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"
    SYMBOL_SUCCESS = "✅ "
    SYMBOL_ERROR = "❌ "
    SYMBOL_INFO = "ℹ️ "
    SYMBOL_WARNING = "⚠️ "
    SYMBOL_FINAL = "🚀 "
else:
    SYMBOL_SUCCESS = "[+] "
    SYMBOL_ERROR = "[X] "
    SYMBOL_INFO = "[i] "
    SYMBOL_WARNING = "[!] "
    SYMBOL_FINAL = "[!!!] "


totalRunTime = 0.0

MOVIE_YEAR_PATTERN = re.compile(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)")
TV_EPISODE_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9])(?:S\s*\d{1,2}[._\- ]*E\s*\d{1,3}|\d{1,2}x\d{1,3})(?!\d)"
)
LOWERCASE_TITLE_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
    "nor", "of", "on", "or", "the", "to", "via", "vs",
}
UPPERCASE_TITLE_WORDS = {"tv", "uk", "usa"}


def title_case_movie_token(
    token: str,
    position: int,
    count: int,
    previous_token: str | None = None,
) -> str:
    """Apply readable title casing while retaining acronyms and Roman numerals."""
    lowered = token.lower()
    starts_after_numeric_title = position == 1 and str(previous_token or "").isdigit()
    if (
        0 < position < count - 1
        and lowered in LOWERCASE_TITLE_WORDS
        and not starts_after_numeric_title
    ):
        return lowered
    if lowered in UPPERCASE_TITLE_WORDS:
        return lowered.upper()
    if token.isupper() and re.fullmatch(r"[IVXLCDM]+", token):
        return token.upper()
    if "-" in token:
        return "-".join(
            title_case_movie_token(part, 0, 1) for part in token.split("-")
        )
    return lowered[:1].upper() + lowered[1:]


def automatic_output_stem(file_name: str) -> str:
    """Return `Movie Title (Year)` when a reliable movie year is present.

    TV episode names deliberately pass through unchanged because the converter's
    existing TV workflow is already relied upon by unattended processing.
    """
    raw_stem = os.path.splitext(os.path.basename(file_name))[0].strip()
    if not raw_stem or TV_EPISODE_PATTERN.search(raw_stem):
        return raw_stem

    # Release names normally place the actual movie year immediately before the
    # technical metadata. Work backward so a year in the title (for example,
    # `Blade Runner 2049.2017`) is not mistaken for the release year.
    for year_match in reversed(list(MOVIE_YEAR_PATTERN.finditer(raw_stem))):
        title_portion = raw_stem[:year_match.start()]
        title_portion = re.sub(r"[._]+", " ", title_portion)
        raw_tokens = re.findall(r"[A-Za-z0-9]+(?:['’&-][A-Za-z0-9]+)*", title_portion)
        if not raw_tokens:
            continue

        title = " ".join(
            title_case_movie_token(
                token,
                index,
                len(raw_tokens),
                raw_tokens[index - 1] if index else None,
            )
            for index, token in enumerate(raw_tokens)
        )
        return f"{title} ({year_match.group(1)})"

    # Without a reliable year, avoid guessing and preserve the source stem.
    return raw_stem


@dataclass
class FFmpegRunResult:
    success: bool
    cancelled: bool
    elapsed: float
    error: str = ""


@dataclass
class ConversionResult:
    status: str
    reason: str = ""
    elapsed: float = 0.0

    @property
    def success(self) -> bool:
        return self.status == "success"


def show_time(seconds: float) -> None:
    global totalRunTime
    totalRunTime += seconds
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds_part = int(seconds % 60)
    if hours:
        print(f"{SYMBOL_INFO} Completed in {hours}h {minutes}m {seconds_part}s")
    elif minutes:
        print(f"{SYMBOL_INFO} Completed in {minutes}m {seconds_part}s")
    else:
        print(f"{SYMBOL_INFO} Completed in {seconds_part}s")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds_part}s"
    if minutes:
        return f"{minutes}m {seconds_part}s"
    return f"{seconds_part}s"


def formatted_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _set_active_process(process: subprocess.Popen[str] | None) -> None:
    global ACTIVE_PROCESS
    with ACTIVE_PROCESS_LOCK:
        ACTIVE_PROCESS = process


def _terminate_process(process: subprocess.Popen[str], timeout: float = 2.0) -> None:
    if process.poll() is not None:
        return

    try:
        if OS_NAME == "Windows":
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=timeout)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    except OSError:
        try:
            process.terminate()
            process.wait(timeout=timeout)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    if process.poll() is None:
        try:
            if OS_NAME == "Windows":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _handle_termination_signal(_signum, _frame) -> None:
    CANCELLATION_REQUESTED.set()
    with ACTIVE_PROCESS_LOCK:
        process = ACTIVE_PROCESS
    if process is not None:
        _terminate_process(process)


for signal_name in ("SIGINT", "SIGTERM"):
    signal_value = getattr(signal, signal_name, None)
    if signal_value is not None:
        signal.signal(signal_value, _handle_termination_signal)


def executable_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required tool not found: {name}")
    return path


def tool_version(name: str) -> str:
    path = executable_path(name)
    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout.splitlines()[0].strip()
    except (OSError, subprocess.CalledProcessError, IndexError):
        return f"{name} version unavailable"


def run_capture(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return result.stdout


def probe_media(input_file: str, select_streams: str | None = None) -> dict[str, Any]:
    command = [executable_path("ffprobe"), "-v", "error"]
    if select_streams:
        command.extend(["-select_streams", select_streams])
    command.extend([
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        input_file,
    ])
    return json.loads(run_capture(command))


def probe_streams(input_file: str, select_streams: str | None = None) -> dict[str, Any]:
    """Compatibility wrapper retained for callers of the older module API."""
    return probe_media(input_file, select_streams)


def tag_value(tags: dict[str, Any] | None, requested_key: str) -> Any:
    """Read FFprobe metadata without depending on its source capitalization."""
    requested = requested_key.casefold()
    for key, value in (tags or {}).items():
        if str(key).casefold() == requested:
            return value
    return None


def normalized_language(stream: dict[str, Any]) -> str:
    language = str(tag_value(stream.get("tags"), "language") or "und").strip().lower()
    if language in {"", "undefined"}:
        return "und"
    if language in {"en", "english"}:
        return "eng"
    return language


def is_english_or_undefined(stream: dict[str, Any]) -> bool:
    return normalized_language(stream) in {"eng", "und"}


def optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def stream_duration_seconds(stream: dict[str, Any]) -> float | None:
    try:
        duration = float(stream.get("duration"))
        if duration > 0:
            return duration
    except (TypeError, ValueError):
        pass

    tagged_duration = tag_value(stream.get("tags"), "duration")
    if not tagged_duration:
        return None
    try:
        hours, minutes, seconds = str(tagged_duration).split(":", 2)
        return (float(hours) * 3600) + (float(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None


def normalized_track_label(mapping: dict[str, Any]) -> str:
    return " ".join(
        str(mapping.get(field) or "").strip()
        for field in ("title", "handler_name")
    ).strip().lower()


def audio_role(mapping: dict[str, Any]) -> str:
    label = normalized_track_label(mapping)
    if mapping.get("is_commentary") or any(marker in label for marker in (
        "commentary", "director's comment", "directors comment",
        "producer's comment", "producers comment", "cast comment",
    )):
        return "commentary"
    if mapping.get("is_visual_impaired") or any(marker in label for marker in (
        "audio description", "audio descriptive", "descriptive audio",
        "described video", "vision impaired",
    )):
        return "audio_description"
    if mapping.get("is_dub") or "dubbed" in label or "dub track" in label:
        return "dub"
    return "main"


def audio_score(mapping: dict[str, Any], longest_duration: float | None) -> int:
    channels = optional_int(mapping.get("channels"))
    channel_score = {6: 400, 2: 300, 8: 250, 1: 200}.get(
        channels,
        min(max(channels or 0, 0), 10) * 20,
    )
    codec_score = {
        "aac": 140,
        "eac3": 120,
        "ac3": 110,
        "alac": 100,
        "mp3": 80,
    }.get(str(mapping.get("codec") or "").lower(), 0)
    score = channel_score + codec_score
    if mapping.get("is_default"):
        score += 35
    if "repaired" in normalized_track_label(mapping):
        score += 20
    bit_rate = optional_int(mapping.get("bit_rate"))
    if bit_rate:
        score += min(bit_rate // 64_000, 12)
    duration = mapping.get("duration")
    if longest_duration and duration and duration < longest_duration * 0.95:
        score -= 1_000
    return score


def preferred_audio_mapping(
    mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not mappings:
        return None
    ordinary = [mapping for mapping in mappings if audio_role(mapping) == "main"]
    pool = ordinary or mappings
    longest_duration = max(
        (mapping["duration"] for mapping in pool if mapping.get("duration")),
        default=None,
    )
    return max(
        pool,
        key=lambda mapping: (
            audio_score(mapping, longest_duration),
            -int(mapping["audio_index"]),
        ),
    )


def resolved_channel_layout(stream: dict[str, Any]) -> str | None:
    # AAC-in-MP4 is most portable when common channel counts use the canonical
    # MPEG layout. In particular, 5.1(side) makes FFmpeg use a PCE; FFmpeg 8.x
    # can then produce an MP4 whose AAC layout is unreadable by Apple players.
    canonical_layout = {
        1: "mono",
        2: "stereo",
        6: "5.1",
        8: "7.1",
    }.get(stream.get("channels"))
    if canonical_layout:
        return canonical_layout

    layout = str(stream.get("channel_layout") or "").strip()
    return layout or None


def aac_bitrate(channels: int | None) -> str:
    """Use enough AAC bitrate for the number of encoded channels."""
    if channels == 1:
        return "128k"
    if channels == 2:
        return "256k"
    if channels in {3, 4}:
        return "384k"
    if channels in {5, 6}:
        return "512k"
    if channels in {7, 8}:
        return "768k"
    return "256k"


def is_apple_compatible_audio_codec(codec: str | None) -> bool:
    """Return whether an audio stream can be copied into an Apple-friendly MP4."""
    return str(codec or "").strip().lower() in {"aac", "alac", "ac3", "eac3"}


def is_canonical_apple_aac_layout(layout: str | None, channels: int | None) -> bool:
    normalized = str(layout or "").strip().lower().replace(" ", "")
    if not normalized or normalized == "unknown":
        return False
    if channels == 6:
        return normalized == "5.1"
    if channels == 8:
        return normalized == "7.1"
    return True


def audio_requires_layout_normalization(mapping: dict[str, Any]) -> bool:
    channels = optional_int(mapping.get("channels"))
    if str(mapping.get("codec") or "").lower() != "aac" or not channels or channels <= 2:
        return False
    return not is_canonical_apple_aac_layout(
        mapping.get("declared_channel_layout"),
        channels,
    )


def should_copy_audio(mapping: dict[str, Any], _mode: str) -> bool:
    # Copy safety is codec/layout based, regardless of the requested mode.
    return (
        is_apple_compatible_audio_codec(mapping.get("codec"))
        and not audio_requires_layout_normalization(mapping)
    )


def channel_layout_family(layout: str | None, channels: int | None) -> str:
    """Normalize equivalent FFmpeg/AAC layout labels for validation."""
    normalized = str(layout or "").strip().lower().replace(" ", "")
    if channels == 6 and normalized.startswith("5.1"):
        return "5.1"
    if channels == 8 and normalized.startswith("7.1"):
        return "7.1"
    return normalized


def audio_decode_issue(output_file: str) -> str | None:
    """Decode a short sample from every audio track to catch broken output."""
    result = subprocess.run(
        [
            executable_path("ffmpeg"),
            "-v", "error",
            "-xerror",
            "-i", output_file,
            "-map", "0:a",
            "-t", "5",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 0:
        return None
    diagnostic = (result.stderr or result.stdout).strip()
    return diagnostic or f"FFmpeg audio decode exited with code {result.returncode}"


def get_english_audio_mappings(audio_probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Choose one preferred main English track, falling back to undefined."""
    mappings: list[dict[str, Any]] = []
    for audio_index, stream in enumerate(audio_probe.get("streams", [])):
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        title = str(tag_value(tags, "title") or "").strip()
        handler_name = str(tag_value(tags, "handler_name") or "").strip()
        mappings.append({
            "index": int(stream["index"]),
            "audio_index": audio_index,
            "language": normalized_language(stream),
            "title": title,
            "handler_name": handler_name,
            "channels": stream.get("channels"),
            "channel_layout": resolved_channel_layout(stream),
            "declared_channel_layout": str(stream.get("channel_layout") or "").strip(),
            "codec": str(stream.get("codec_name") or "").lower(),
            "sample_format": str(stream.get("sample_fmt") or "").lower(),
            "codec_tag": str(stream.get("codec_tag_string") or "").lower(),
            "bit_rate": optional_int(stream.get("bit_rate")),
            "duration": stream_duration_seconds(stream),
            "is_default": disposition.get("default") == 1,
            "is_commentary": disposition.get("comment") == 1,
            "is_visual_impaired": disposition.get("visual_impaired") == 1,
            "is_dub": disposition.get("dub") == 1,
        })

    english = [mapping for mapping in mappings if mapping["language"] == "eng"]
    undefined = [mapping for mapping in mappings if mapping["language"] == "und"]
    preferred = preferred_audio_mapping(english or undefined)
    return [preferred] if preferred else []


def get_english_audio_indices(audio_streams: dict[str, Any]) -> list[str]:
    """Compatibility wrapper for the previous public helper."""
    return [str(mapping["index"]) for mapping in get_english_audio_mappings(audio_streams)]


def get_video_stream(video_probe: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (stream for stream in video_probe.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )


def get_video_codec(video_streams: dict[str, Any]) -> str | None:
    stream = get_video_stream(video_streams)
    return str(stream.get("codec_name") or "").lower() if stream else None


def parse_frame_rate(value: Any) -> float | None:
    if value in (None, "", "0/0"):
        return None
    text = str(value)
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else None
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def get_frame_rate(video_probe: dict[str, Any]) -> float | None:
    stream = get_video_stream(video_probe)
    if not stream:
        return None
    return parse_frame_rate(stream.get("avg_frame_rate")) or parse_frame_rate(stream.get("r_frame_rate"))


def media_duration(media_probe: dict[str, Any]) -> float | None:
    try:
        return float(media_probe.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


def subtitle_is_english(mapping: dict[str, Any]) -> bool:
    return mapping.get("language") == "eng" or "english" in normalized_track_label(mapping)


def subtitle_role(
    mapping: dict[str, Any],
    maximum_cue_count: int | None = None,
) -> str:
    label = normalized_track_label(mapping)
    if mapping.get("is_forced") or "forced" in label:
        return "forced"
    cue_count = mapping.get("cue_count")
    if (
        maximum_cue_count is not None
        and maximum_cue_count >= 20
        and cue_count is not None
        and cue_count < max(10, int(maximum_cue_count * 0.35))
    ):
        return "forced"
    if mapping.get("is_hearing_impaired") or mapping.get("is_captions") or any(
        marker in label
        for marker in ("sdh", "hearing impaired", "closed caption", "closed-caption", "cc")
    ):
        return "sdh"
    accessibility_count = int(mapping.get("accessibility_marker_count") or 0)
    if cue_count and accessibility_count >= max(5, int(cue_count * 0.03)):
        return "sdh"
    return "ordinary"


def subtitle_content_metrics(text: str) -> tuple[int, int]:
    lines = text.splitlines()
    cue_count = sum("-->" in line for line in lines)
    accessibility_count = 0
    for raw_line in lines:
        line = re.sub(r"<[^>]+>", "", raw_line).strip()
        if not line or "-->" in line:
            continue
        if "♪" in line or "♫" in line:
            accessibility_count += 1
        elif line.startswith("[") and "]" in line:
            accessibility_count += 1
        elif line.startswith("(") and line.endswith(")"):
            accessibility_count += 1
        elif re.search(r"^[A-Z][A-Z0-9 .'-]{1,24}:\s+", line):
            accessibility_count += 1
    return cue_count, accessibility_count


def inspect_subtitle_content(input_file: str, stream_index: int) -> tuple[int, int] | None:
    result = subprocess.run(
        [
            executable_path("ffmpeg"),
            "-nostdin", "-v", "error", "-i", input_file,
            "-map", f"0:{stream_index}", "-f", "srt", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        return None
    return subtitle_content_metrics(result.stdout)


def subtitle_needs_content_inspection(mappings: list[dict[str, Any]]) -> bool:
    if len(mappings) <= 1:
        return False
    maximum_cue_count = max(
        (mapping["cue_count"] for mapping in mappings if mapping.get("cue_count") is not None),
        default=None,
    )
    priorities = {"ordinary": 3, "sdh": 2, "forced": 1}
    resolved_priorities = [
        priorities[subtitle_role(mapping, maximum_cue_count)] for mapping in mappings
    ]
    best_priority = max(resolved_priorities)
    return resolved_priorities.count(best_priority) > 1


def preferred_subtitle_mapping(
    mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not mappings:
        return None
    maximum_cue_count = max(
        (mapping["cue_count"] for mapping in mappings if mapping.get("cue_count") is not None),
        default=None,
    )
    base_scores = {"ordinary": 300_000, "sdh": 200_000, "forced": 100_000}

    def score(mapping: dict[str, Any]) -> tuple[int, int]:
        role = subtitle_role(mapping, maximum_cue_count)
        value = base_scores[role]
        cue_count = mapping.get("cue_count")
        if maximum_cue_count and cue_count is not None:
            value += int((cue_count / maximum_cue_count) * 10_000)
        value -= min(int(mapping.get("accessibility_marker_count") or 0), 10_000) * 20
        if mapping.get("is_default") and role != "forced":
            value += 50
        return value, -int(mapping["subtitle_index"])

    return max(mappings, key=score)


def get_subtitle_streams(
    subtitle_probe: dict[str, Any],
    input_file: str | None = None,
) -> list[dict[str, Any]]:
    """Choose one preferred full English subtitle, falling back to undefined."""
    valid_codecs = {"subrip", "ass", "ssa", "mov_text", "webvtt"}
    mappings: list[dict[str, Any]] = []
    for subtitle_index, stream in enumerate(subtitle_probe.get("streams", [])):
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in valid_codecs:
            continue
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        mappings.append({
            "index": int(stream["index"]),
            "subtitle_index": subtitle_index,
            "codec": codec,
            "language": normalized_language(stream),
            "title": str(tag_value(tags, "title") or "").strip(),
            "handler_name": str(tag_value(tags, "handler_name") or "").strip(),
            "cue_count": optional_int(stream.get("nb_frames"))
                or optional_int(stream.get("nb_read_packets")),
            "accessibility_marker_count": 0,
            "is_default": disposition.get("default") == 1,
            "is_forced": disposition.get("forced") == 1,
            "is_hearing_impaired": disposition.get("hearing_impaired") == 1,
            "is_captions": disposition.get("captions") == 1,
        })

    english = [mapping for mapping in mappings if subtitle_is_english(mapping)]
    undefined = [mapping for mapping in mappings if mapping["language"] == "und"]
    selection_pool = english or undefined
    if input_file and subtitle_needs_content_inspection(selection_pool):
        for mapping in selection_pool:
            metrics = inspect_subtitle_content(input_file, int(mapping["index"]))
            if metrics:
                mapping["cue_count"], mapping["accessibility_marker_count"] = metrics

    preferred = preferred_subtitle_mapping(selection_pool)
    return [preferred] if preferred else []


def subtitle_metrics_from_file(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as subtitle_file:
            data = subtitle_file.read()
    except OSError:
        return None

    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return subtitle_content_metrics(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return None


def sibling_srt_mapping(input_file: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Choose one matching English sidecar only when embedded text is unavailable."""
    directory = os.path.dirname(os.path.abspath(input_file))
    source_stem = os.path.splitext(os.path.basename(input_file))[0].casefold()
    try:
        names = [name for name in os.listdir(directory) if not name.startswith(".")]
    except OSError:
        return None

    all_srts = [name for name in names if os.path.splitext(name)[1].lower() == ".srt"]
    matching_srts = [
        name for name in all_srts
        if (stem := os.path.splitext(name)[0].casefold()) == source_stem
        or stem.startswith(source_stem + ".")
        or stem.startswith(source_stem + " ")
    ]

    if not matching_srts and len(all_srts) == 1:
        video_extensions = {".mkv", ".mp4", ".avi", ".mov", ".m4v"}
        video_count = sum(os.path.splitext(name)[1].lower() in video_extensions for name in names)
        if video_count == 1:
            matching_srts = all_srts

    candidates: list[dict[str, Any]] = []
    for subtitle_index, name in enumerate(matching_srts):
        path = os.path.join(directory, name)
        label = os.path.splitext(name)[0].lower()
        metrics = subtitle_metrics_from_file(path)
        hearing_impaired = re.search(
            r"(^|[ ._-])(sdh|hi|hearing[ ._-]?impaired)($|[ ._-])",
            label,
        ) is not None
        candidates.append({
            "index": 0,
            "input_index": 1,
            "subtitle_index": subtitle_index,
            "codec": "subrip",
            "language": "eng",
            "title": "English (SDH)" if hearing_impaired else "English",
            "handler_name": "English (SDH)" if hearing_impaired else "English",
            "cue_count": metrics[0] if metrics else None,
            "accessibility_marker_count": metrics[1] if metrics else 0,
            "is_default": True,
            "is_forced": re.search(
                r"(^|[ ._-])forced($|[ ._-])",
                label,
            ) is not None,
            "is_hearing_impaired": hearing_impaired,
            "is_captions": hearing_impaired,
            "sidecar_path": path,
        })

    preferred = preferred_subtitle_mapping(candidates)
    if not preferred:
        return None
    return str(preferred["sidecar_path"]), [preferred]


def is_floating_point_pcm(mapping: dict[str, Any]) -> bool:
    codec = mapping.get("codec", "")
    codec_tag = mapping.get("codec_tag", "")
    sample_format = mapping.get("sample_format", "")
    return (
        (codec.startswith("pcm_") or "float" in codec or codec.startswith("pcm_f"))
        and any(marker in sample_format for marker in ("flt", "dbl", "float"))
    ) or any(marker in codec_tag for marker in ("fl32", "fl64", "f32", "f64", "float"))


def standardized_subtitle_title(subtitle: dict[str, Any]) -> str:
    """Create a useful title without ever writing the literal 'Undefined'."""
    language = str(subtitle.get("language") or "und").strip().lower()
    names = {
        "und": "", "eng": "English", "spa": "Spanish",
        "fra": "French", "fre": "French", "deu": "German", "ger": "German",
        "ita": "Italian", "por": "Portuguese", "nld": "Dutch", "dut": "Dutch",
        "pol": "Polish", "rus": "Russian", "ukr": "Ukrainian",
        "ces": "Czech", "cze": "Czech", "ara": "Arabic", "bul": "Bulgarian",
        "dan": "Danish", "est": "Estonian", "fin": "Finnish", "heb": "Hebrew",
        "hin": "Hindi", "hun": "Hungarian", "lav": "Latvian", "lit": "Lithuanian",
        "ell": "Greek", "gre": "Greek", "nor": "Norwegian",
        "ron": "Romanian", "rum": "Romanian", "slv": "Slovenian",
        "swe": "Swedish", "tur": "Turkish", "jpn": "Japanese",
        "chi": "Chinese", "zho": "Chinese", "cat": "Catalan",
    }
    language_name = names.get(language, language.upper())
    qualifiers: list[str] = []
    if subtitle.get("is_forced"):
        qualifiers.append("Forced")
    if subtitle.get("is_hearing_impaired") or subtitle.get("is_captions"):
        qualifiers.append("SDH")
    if not language_name:
        return ", ".join(qualifiers)
    return (
        f"{language_name} ({', '.join(qualifiers)})"
        if qualifiers
        else language_name
    )


def remux_compatibility_issue(
    video_probe: dict[str, Any],
    audio_mappings: list[dict[str, Any]],
) -> str | None:
    video_codec = get_video_codec(video_probe) or ""
    if video_codec not in {"h264", "hevc", "h265", "mpeg4"}:
        return f"unsupported MP4 video codec: {video_codec or 'unknown'}"

    compatible_audio = {"aac", "alac", "ac3", "eac3"}
    for mapping in audio_mappings:
        codec = mapping.get("codec", "")
        if "dts" in codec or "dca" in codec:
            return "DTS audio requires encoding instead of remuxing"
        if is_floating_point_pcm(mapping):
            return "floating-point PCM audio requires encoding instead of remuxing"
        if codec not in compatible_audio:
            return f"unsupported MP4 audio codec: {codec or 'unknown'}"
        if audio_requires_layout_normalization(mapping):
            return "ambiguous multichannel AAC layout requires audio normalization"
        if (
            (mapping.get("channels") or 0) > 2
            and not mapping.get("declared_channel_layout")
        ):
            return "multichannel audio has no declared layout and requires audio encoding"
    return None


def resolve_smart_mode(
    input_file: str,
    input_probe: dict[str, Any],
    audio_mappings: list[dict[str, Any]],
    target_mb_per_minute: float,
) -> str:
    """Choose remux for compact compatible inputs, otherwise H.265 encode."""
    duration = media_duration(input_probe)
    try:
        input_bytes = os.path.getsize(input_file)
    except OSError:
        input_bytes = 0

    if not duration or duration <= 0 or input_bytes <= 0:
        print(
            f"{SYMBOL_WARNING} Smart analysis could not read file size or runtime; "
            "using H.265 encode"
        )
        return "encode"

    megabytes_per_minute = (input_bytes / 1_000_000) / (duration / 60)
    print(
        f"{SYMBOL_INFO} Smart analysis: {megabytes_per_minute:.1f} MB/min "
        f"(target ≤ {target_mb_per_minute:g} MB/min)"
    )
    if megabytes_per_minute > target_mb_per_minute:
        print(f"{SYMBOL_INFO} Smart decision: Encode H.265")
        return "encode"

    compatibility_issue = remux_compatibility_issue(input_probe, audio_mappings)
    if compatibility_issue:
        print(
            f"{SYMBOL_WARNING} Smart remux unavailable: {compatibility_issue}; "
            "using H.265 encode"
        )
        print(f"{SYMBOL_INFO} Smart decision: Encode H.265")
        return "encode"

    print(f"{SYMBOL_INFO} Smart decision: Remux")
    return "remux"


def build_ffmpeg_cmd(
    input_file: str,
    temp_file: str,
    mode: str,
    video_codec: str | None,
    audio_mappings: list[dict[str, Any]],
    subtitle_streams: list[dict[str, Any]],
    subtitle_sidecar: str | None = None,
) -> list[str]:
    """Construct an explicit, metadata-safe FFmpeg command."""
    command = [executable_path("ffmpeg"), "-hide_banner", "-y", "-i", input_file]
    if subtitle_sidecar:
        command.extend(["-i", subtitle_sidecar])

    if mode == "encode":
        command.extend([
            "-c:v", "libx265",
            "-x265-params", "log-level=0:threads=0",
            "-preset", "fast",
            "-crf", "23",
        ])
        if not audio_mappings:
            command.append("-an")
    else:
        command.extend(["-c:v", "copy"])
        if audio_mappings:
            command.extend(["-c:a", "copy"])
        else:
            command.append("-an")

    # Do not rebuild QuickTime chapters from the source. FFmpeg represents them
    # with hidden text/data tracks, and malformed source chapters can produce an
    # MP4 that probes successfully but is rejected by Apple's media framework.
    command.extend(["-map", "0:v:0", "-map_metadata", "-1", "-map_chapters", "-1"])

    if mode == "encode" or video_codec == "hevc":
        command.extend(["-tag:v", "hvc1"])
    command.extend([
        "-metadata:s:v:0", "title=",
        "-metadata:s:v:0", "handler_name=",
    ])

    for output_index, mapping in enumerate(audio_mappings):
        command.extend(["-map", f"0:{mapping['index']}"])
        copies_audio = should_copy_audio(mapping, mode)
        command.extend([f"-c:a:{output_index}", "copy" if copies_audio else "aac"])
        command.extend([
            f"-metadata:s:a:{output_index}", f"language={mapping['language']}",
            f"-metadata:s:a:{output_index}", "title=",
            f"-metadata:s:a:{output_index}", "handler_name=",
            f"-disposition:a:{output_index}", "default" if output_index == 0 else "0",
        ])
        if not copies_audio and mapping.get("channel_layout"):
            command.extend([
                f"-channel_layout:a:{output_index}", str(mapping["channel_layout"]),
            ])
        if not copies_audio:
            command.extend([
                f"-b:a:{output_index}", aac_bitrate(mapping.get("channels")),
            ])

    for output_index, subtitle in enumerate(subtitle_streams):
        dispositions = ["default"]
        if subtitle.get("is_forced"):
            dispositions.append("forced")
        if subtitle.get("is_hearing_impaired"):
            dispositions.append("hearing_impaired")
        if subtitle.get("is_captions"):
            dispositions.append("captions")
        command.extend([
            "-map", f"{subtitle.get('input_index', 0)}:{subtitle['index']}",
            f"-c:s:{output_index}", "mov_text",
            f"-metadata:s:s:{output_index}", f"language={subtitle['language']}",
            f"-metadata:s:s:{output_index}", f"title={standardized_subtitle_title(subtitle)}",
            f"-metadata:s:s:{output_index}", f"handler_name={standardized_subtitle_title(subtitle)}",
            f"-disposition:s:{output_index}", "+".join(dispositions),
        ])

    command.extend([
        "-movflags", "+faststart",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        temp_file,
    ])
    return command


def build_subtitle_mux_cmd(
    encoded_av_file: str,
    subtitle_source: str,
    output_file: str,
    video_codec: str | None,
    audio_mappings: list[dict[str, Any]],
    subtitle_streams: list[dict[str, Any]],
) -> list[str]:
    """Add subtitles only after a long encode has produced valid video/audio."""
    command = [
        executable_path("ffmpeg"), "-hide_banner", "-y",
        "-i", encoded_av_file,
        "-i", subtitle_source,
        "-map", "0:v:0", "-c:v", "copy",
    ]
    if video_codec in {"hevc", "h265"}:
        command.extend(["-tag:v", "hvc1"])
    command.extend([
        "-metadata:s:v:0", "title=",
        "-metadata:s:v:0", "handler_name=",
    ])

    for output_index, mapping in enumerate(audio_mappings):
        command.extend([
            "-map", f"0:a:{output_index}",
            f"-c:a:{output_index}", "copy",
            f"-metadata:s:a:{output_index}", f"language={mapping['language']}",
            f"-metadata:s:a:{output_index}", "title=",
            f"-metadata:s:a:{output_index}", "handler_name=",
            f"-disposition:a:{output_index}", "default" if output_index == 0 else "0",
        ])

    for output_index, subtitle in enumerate(subtitle_streams):
        dispositions = ["default"]
        if subtitle.get("is_forced"):
            dispositions.append("forced")
        if subtitle.get("is_hearing_impaired"):
            dispositions.append("hearing_impaired")
        if subtitle.get("is_captions"):
            dispositions.append("captions")
        command.extend([
            "-map", f"1:{subtitle['index']}",
            f"-c:s:{output_index}", "mov_text",
            f"-metadata:s:s:{output_index}", f"language={subtitle['language']}",
            f"-metadata:s:s:{output_index}", f"title={standardized_subtitle_title(subtitle)}",
            f"-metadata:s:s:{output_index}", f"handler_name={standardized_subtitle_title(subtitle)}",
            f"-disposition:s:{output_index}", "+".join(dispositions),
        ])

    command.extend([
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        output_file,
    ])
    return command


def parse_ffmpeg_time(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":")
        return (float(hours) * 3600) + (float(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None


def run_ffmpeg(
    command: list[str],
    input_file: str,
    temp_file: str,
    duration: float | None,
    frame_rate: float | None,
    enforce_progress_duration: bool = True,
) -> FFmpegRunResult:
    stderr_lines: deque[str] = deque(maxlen=80)
    start_time = time.time()
    latest_media_time = 0.0
    latest_timestamp_time = 0.0
    latest_frame: int | None = None
    last_timestamp_advance = 0.0
    last_progress_display = 0.0
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if OS_NAME == "Windows" else 0

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=OS_NAME != "Windows",
        creationflags=creation_flags,
    )
    _set_active_process(process)

    def collect_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            cleaned = line.strip()
            if cleaned:
                stderr_lines.append(cleaned)

    stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
    stderr_thread.start()

    progress_values: dict[str, str] = {}
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                progress_values[key] = value

                if key == "out_time_us":
                    try:
                        reported_time = float(value) / 1_000_000
                        latest_timestamp_time = max(latest_timestamp_time, reported_time)
                        if reported_time > latest_media_time + 0.001:
                            latest_media_time = reported_time
                            last_timestamp_advance = time.time()
                    except ValueError:
                        pass
                elif key == "out_time":
                    parsed_time = parse_ffmpeg_time(value)
                    if parsed_time is not None:
                        latest_timestamp_time = max(latest_timestamp_time, parsed_time)
                        if parsed_time > latest_media_time + 0.001:
                            latest_media_time = parsed_time
                            last_timestamp_advance = time.time()
                elif key == "frame":
                    try:
                        latest_frame = int(value)
                        if frame_rate and (
                            last_timestamp_advance == 0
                            or time.time() - last_timestamp_advance >= 5
                        ):
                            latest_media_time = max(latest_media_time, latest_frame / frame_rate)
                    except ValueError:
                        pass

                if key == "progress":
                    now = time.time()
                    if now - last_progress_display >= PROGRESS_INTERVAL_SECONDS or value == "end":
                        elapsed = now - start_time
                        percent = None
                        eta = None
                        if duration and duration > 0:
                            fraction = min(max(latest_media_time / duration, 0), 1)
                            percent = fraction * 100
                            if fraction > 0.01:
                                eta = max((elapsed / fraction) - elapsed, 0)

                        original_size = os.path.getsize(input_file) if os.path.exists(input_file) else 0
                        output_size = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
                        percent_text = f"{percent:.1f}%" if percent is not None else "working"
                        speed = progress_values.get("speed", "")
                        eta_text = f" | ETA {format_duration(eta)}" if eta is not None else ""
                        speed_text = f" | {speed}" if speed and speed != "N/A" else ""
                        print_progress(
                            f"Processing {os.path.basename(input_file)} | {percent_text}"
                            f" | {format_duration(elapsed)} elapsed{eta_text}{speed_text}"
                            f" | {formatted_bytes(original_size)} → {formatted_bytes(output_size)}"
                        )
                        last_progress_display = now

                if CANCELLATION_REQUESTED.is_set() and process.poll() is None:
                    _terminate_process(process)
                    break

        return_code = process.wait()
    except KeyboardInterrupt:
        CANCELLATION_REQUESTED.set()
        _terminate_process(process)
        return_code = process.wait()
    finally:
        stderr_thread.join(timeout=1)
        clear_progress_line()
        _set_active_process(None)

    elapsed = time.time() - start_time
    if CANCELLATION_REQUESTED.is_set():
        return FFmpegRunResult(False, True, elapsed, "Cancelled")
    if return_code == 0:
        final_media_time = latest_timestamp_time
        if final_media_time <= 0 and frame_rate and latest_frame is not None:
            final_media_time = latest_frame / frame_rate
        if duration and duration > 0 and final_media_time > 0:
            tolerance = max(5.0, min(30.0, duration * 0.005))
            if duration - final_media_time > tolerance:
                progress_message = (
                    f"FFmpeg progress ended at {final_media_time:.2f}s of "
                    f"{duration:.2f}s"
                )
                if enforce_progress_duration:
                    return FFmpegRunResult(
                        False,
                        False,
                        elapsed,
                        progress_message,
                    )
                print(
                    f"{SYMBOL_WARNING} {progress_message}; checking the completed "
                    "remux with ffprobe instead"
                )
        return FFmpegRunResult(True, False, elapsed)

    stderr_diagnostic = "\n".join(stderr_lines)
    if return_code < 0:
        signal_number = -return_code
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = "unknown signal"
        diagnostic = (
            f"FFmpeg was terminated by signal {signal_number} ({signal_name})"
        )
    else:
        diagnostic = f"FFmpeg exited with code {return_code}"
    if stderr_diagnostic:
        diagnostic += f":\n{stderr_diagnostic}"
    else:
        diagnostic += " without diagnostic output"
    return FFmpegRunResult(False, False, elapsed, diagnostic)


def audio_duration_mismatch_indexes(
    output_file: str,
    expected_audio_mappings: list[dict[str, Any]],
) -> list[int]:
    """Return selected audio tracks whose produced duration is incomplete."""
    try:
        output_probe = probe_media(output_file)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return list(range(len(expected_audio_mappings)))

    output_audio = [
        stream
        for stream in output_probe.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    mismatches: list[int] = []
    for audio_index, mapping in enumerate(expected_audio_mappings):
        if audio_index >= len(output_audio):
            mismatches.append(audio_index)
            continue
        expected_duration = mapping.get("duration")
        actual_duration = stream_duration_seconds(output_audio[audio_index])
        if not expected_duration or not actual_duration:
            continue
        tolerance = max(2.0, min(10.0, expected_duration * 0.001))
        if abs(expected_duration - actual_duration) > tolerance:
            mismatches.append(audio_index)
    return mismatches


def rebuild_incomplete_audio_tracks(
    output_file: str,
    source_file: str,
    audio_mappings: list[dict[str, Any]],
    mismatched_indexes: list[int],
    duration: float | None,
    frame_rate: float | None,
) -> FFmpegRunResult:
    """Re-encode only incomplete audio while preserving completed video/audio."""
    descriptor, repaired_file = tempfile.mkstemp(
        dir=os.path.dirname(output_file) or None,
        prefix=".mp4-steward-audio-repair-",
        suffix=".mp4",
    )
    os.close(descriptor)
    mismatched = set(mismatched_indexes)
    command = [
        executable_path("ffmpeg"), "-hide_banner", "-y",
        "-i", output_file,
        "-i", source_file,
        "-map", "0:v:0", "-c:v", "copy", "-tag:v", "hvc1",
        "-metadata:s:v:0", "title=",
        "-metadata:s:v:0", "handler_name=",
    ]
    for output_index, mapping in enumerate(audio_mappings):
        if output_index in mismatched:
            command.extend([
                "-map", f"1:{mapping['index']}",
                f"-c:a:{output_index}", "aac",
            ])
            if mapping.get("channel_layout"):
                command.extend([
                    f"-channel_layout:a:{output_index}",
                    str(mapping["channel_layout"]),
                ])
            command.extend([
                f"-b:a:{output_index}", aac_bitrate(mapping.get("channels")),
            ])
        else:
            command.extend([
                "-map", f"0:a:{output_index}",
                f"-c:a:{output_index}", "copy",
            ])
        command.extend([
            f"-metadata:s:a:{output_index}", f"language={mapping['language']}",
            f"-metadata:s:a:{output_index}", "title=",
            f"-metadata:s:a:{output_index}", "handler_name=",
            f"-disposition:a:{output_index}", "default" if output_index == 0 else "0",
        ])

    command.extend([
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        repaired_file,
    ])
    try:
        print(f"{SYMBOL_INFO} Audio repair command: {shlex.join(command)}")
        result = run_ffmpeg(
            command,
            source_file,
            repaired_file,
            duration,
            frame_rate,
        )
        if not result.success:
            return result
        remaining = audio_duration_mismatch_indexes(repaired_file, audio_mappings)
        if remaining:
            tracks = ", ".join(str(index + 1) for index in remaining)
            return FFmpegRunResult(
                False,
                False,
                result.elapsed,
                f"Rebuilt audio track(s) {tracks} are still incomplete",
            )
        os.replace(repaired_file, output_file)
        repaired_file = ""
        return result
    finally:
        if repaired_file and os.path.exists(repaired_file):
            try:
                os.remove(repaired_file)
            except OSError:
                pass


def validate_output(
    input_probe: dict[str, Any],
    output_file: str,
    expected_audio_mappings: list[dict[str, Any]],
    require_encoded_layouts: bool,
    expected_subtitle_mappings: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    if not os.path.isfile(output_file) or os.path.getsize(output_file) < 1024:
        return False, "output is missing or empty"

    try:
        output_probe = probe_media(output_file)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as error:
        return False, f"ffprobe could not read the output: {error}"

    output_streams = output_probe.get("streams", [])
    output_video_streams = [
        stream for stream in output_streams if stream.get("codec_type") == "video"
    ]
    if not output_video_streams:
        return False, "output contains no video stream"

    source_video_stream = get_video_stream(input_probe)
    expected_video_duration = (
        stream_duration_seconds(source_video_stream)
        if source_video_stream
        else None
    )
    output_video_duration = stream_duration_seconds(output_video_streams[0])
    if expected_video_duration and output_video_duration:
        tolerance = max(5.0, min(30.0, expected_video_duration * 0.005))
        if expected_video_duration - output_video_duration > tolerance:
            return False, (
                f"video duration mismatch: source {expected_video_duration:.2f}s, "
                f"output {output_video_duration:.2f}s"
            )

    auxiliary_count = sum(
        stream.get("codec_type") == "data" for stream in output_streams
    )
    if auxiliary_count:
        return False, (
            f"output contains {auxiliary_count} unsupported auxiliary data track(s)"
        )

    output_audio_count = sum(stream.get("codec_type") == "audio" for stream in output_streams)
    expected_audio_count = len(expected_audio_mappings)
    if output_audio_count != expected_audio_count:
        return False, f"expected {expected_audio_count} audio track(s), found {output_audio_count}"

    if expected_subtitle_mappings is not None:
        output_subtitle_count = sum(
            stream.get("codec_type") == "subtitle" for stream in output_streams
        )
        expected_subtitle_count = len(expected_subtitle_mappings)
        if output_subtitle_count != expected_subtitle_count:
            return False, (
                f"expected {expected_subtitle_count} subtitle track(s), "
                f"found {output_subtitle_count}"
            )

    if require_encoded_layouts:
        output_audio_streams = [
            stream for stream in output_streams if stream.get("codec_type") == "audio"
        ]
        for audio_index, mapping in enumerate(expected_audio_mappings):
            expected_layout = str(mapping.get("channel_layout") or "").strip().lower()
            expected_channels = mapping.get("channels")
            if not expected_layout:
                continue
            actual_stream = output_audio_streams[audio_index]
            actual_channels = actual_stream.get("channels")
            actual_layout = str(actual_stream.get("channel_layout") or "").strip().lower()
            if actual_channels != expected_channels:
                return False, (
                    f"audio track {audio_index + 1} expected {expected_channels} channels, "
                    f"found {actual_channels}"
                )
            if (
                not actual_layout
                or channel_layout_family(actual_layout, actual_channels)
                != channel_layout_family(expected_layout, expected_channels)
            ):
                return False, (
                    f"audio track {audio_index + 1} has incompatible layout "
                    f"{actual_layout or 'unknown'}; expected {expected_layout}"
                )

    output_audio_streams = [
        stream for stream in output_streams if stream.get("codec_type") == "audio"
    ]
    for audio_index, mapping in enumerate(expected_audio_mappings):
        expected_duration = mapping.get("duration")
        if not expected_duration or audio_index >= len(output_audio_streams):
            continue
        actual_duration = stream_duration_seconds(output_audio_streams[audio_index])
        if not actual_duration:
            continue
        tolerance = max(2.0, min(10.0, expected_duration * 0.001))
        if abs(expected_duration - actual_duration) > tolerance:
            return False, (
                f"audio track {audio_index + 1} duration mismatch: "
                f"expected {expected_duration:.2f}s, output {actual_duration:.2f}s"
            )

    decode_issue = audio_decode_issue(output_file)
    if decode_issue:
        return False, f"audio decode check failed: {decode_issue}"

    input_duration = media_duration(input_probe)
    output_duration = media_duration(output_probe)
    if input_duration and output_duration:
        tolerance = max(5.0, min(30.0, input_duration * 0.005))
        if abs(input_duration - output_duration) > tolerance:
            return False, (
                f"duration mismatch: source {input_duration:.2f}s, "
                f"output {output_duration:.2f}s"
            )

    return True, ""


def convert_to_mp4(
    input_file: str,
    temp_file: str,
    mode: str,
    smart_target_mb_per_minute: float = DEFAULT_SMART_TARGET_MB_PER_MINUTE,
) -> ConversionResult:
    """Probe, convert/remux, and validate a single file."""
    try:
        input_probe = probe_media(input_file)
        audio_probe = probe_media(input_file, "a")
        subtitle_probe = probe_media(input_file, "s")

        source_video_stream = get_video_stream(input_probe)
        if source_video_stream is None:
            reason = "No video track was found"
            print(f"{SYMBOL_WARNING} {reason}. Skipping before FFmpeg starts.")
            return ConversionResult("skipped", reason)

        audio_mappings = get_english_audio_mappings(audio_probe)
        if not audio_mappings:
            all_audio_streams = audio_probe.get("streams", [])
            reason = (
                "No audio tracks were found"
                if not all_audio_streams
                else "No English or undefined-language audio tracks were found"
            )
            print(f"{SYMBOL_WARNING} {reason}. Skipping before FFmpeg starts.")
            return ConversionResult("skipped", reason)

        if mode == "smart":
            mode = resolve_smart_mode(
                input_file,
                input_probe,
                audio_mappings,
                smart_target_mb_per_minute,
            )

        video_codec = get_video_codec(input_probe)
        if mode == "remux":
            issue = remux_compatibility_issue(input_probe, audio_mappings)
            if issue:
                print(f"{SYMBOL_ERROR} Remux rejected: {issue}")
                return ConversionResult(
                    "failed",
                    f"Remux compatibility check failed: {issue}",
                )

        subtitle_mappings = get_subtitle_streams(subtitle_probe, input_file)
        subtitle_sidecar: str | None = None
        if not subtitle_mappings:
            sidecar_selection = sibling_srt_mapping(input_file)
            if sidecar_selection:
                subtitle_sidecar, subtitle_mappings = sidecar_selection
                print(
                    f"{SYMBOL_INFO} Selected sibling subtitle: "
                    f"{os.path.basename(subtitle_sidecar)}"
                )
        skipped_subtitles = len(subtitle_probe.get("streams", [])) - len(subtitle_mappings)

        print(f"{SYMBOL_INFO} Selected audio tracks:")
        for mapping in audio_mappings:
            audio_action = (
                "copied without re-encoding"
                if should_copy_audio(mapping, mode)
                else f"converted to AAC {aac_bitrate(mapping.get('channels'))}"
            )
            print(
                f"  0:{mapping['index']} | {mapping['language']} | "
                f"{mapping.get('channel_layout') or 'unknown layout'} | "
                f"{mapping.get('title') or mapping.get('handler_name') or 'untitled'} | "
                f"{audio_action}"
            )
        print(f"{SYMBOL_INFO} Selected {len(subtitle_mappings)} subtitle track(s).")
        if skipped_subtitles > 0:
            print(
                f"{SYMBOL_WARNING} Skipped {skipped_subtitles} alternate, unsupported, "
                "or non-English subtitle track(s)."
            )

        uses_separate_subtitle_mux = bool(subtitle_mappings) and (
            mode == "encode" or subtitle_sidecar is not None
        )
        encoded_av_file = temp_file + ".av.mp4" if uses_separate_subtitle_mux else temp_file
        command = build_ffmpeg_cmd(
            input_file,
            encoded_av_file,
            mode,
            video_codec,
            audio_mappings,
            [] if uses_separate_subtitle_mux else subtitle_mappings,
            None if uses_separate_subtitle_mux else subtitle_sidecar,
        )
        print(f"{SYMBOL_INFO} Running in {mode} mode")
        if uses_separate_subtitle_mux:
            print(
                f"{SYMBOL_INFO} Encoding video and audio first; "
                "subtitles will be added in a separate remux"
            )
        print(f"{SYMBOL_INFO} FFmpeg command: {shlex.join(command)}")

        run_result = run_ffmpeg(
            command,
            input_file,
            encoded_av_file,
            media_duration(input_probe),
            get_frame_rate(input_probe),
        )
        if run_result.cancelled:
            print(f"{SYMBOL_WARNING} Processing cancelled")
            return ConversionResult("cancelled", "Cancelled", run_result.elapsed)
        if not run_result.success:
            print(f"{SYMBOL_ERROR} FFmpeg failed:\n{run_result.error}")
            return ConversionResult(
                "failed",
                f"Primary {mode} failed: {run_result.error}",
                run_result.elapsed,
            )

        if mode == "encode":
            mismatched_audio = audio_duration_mismatch_indexes(
                encoded_av_file,
                audio_mappings,
            )
            if mismatched_audio:
                tracks = ", ".join(str(index + 1) for index in mismatched_audio)
                print(
                    f"{SYMBOL_WARNING} Audio duration mismatch detected in "
                    f"track(s) {tracks}. Rebuilding affected audio..."
                )
                repair_result = rebuild_incomplete_audio_tracks(
                    encoded_av_file,
                    input_file,
                    audio_mappings,
                    mismatched_audio,
                    media_duration(input_probe),
                    get_frame_rate(input_probe),
                )
                run_result.elapsed += repair_result.elapsed
                if repair_result.cancelled:
                    return ConversionResult("cancelled", "Cancelled", run_result.elapsed)
                if not repair_result.success:
                    return ConversionResult(
                        "failed",
                        f"Audio repair failed: {repair_result.error}",
                        run_result.elapsed,
                    )

        if uses_separate_subtitle_mux:
            valid, validation_error = validate_output(
                input_probe,
                encoded_av_file,
                audio_mappings,
                require_encoded_layouts=mode == "encode",
                expected_subtitle_mappings=[],
            )
            if not valid:
                return ConversionResult(
                    "failed",
                    f"Encoded video/audio validation failed: {validation_error}",
                    run_result.elapsed,
                )

            subtitle_source = subtitle_sidecar or input_file
            subtitle_command = build_subtitle_mux_cmd(
                encoded_av_file,
                subtitle_source,
                temp_file,
                video_codec="hevc" if mode == "encode" else video_codec,
                audio_mappings=audio_mappings,
                subtitle_streams=subtitle_mappings,
            )
            print(f"{SYMBOL_INFO} Adding {len(subtitle_mappings)} subtitle track(s)")
            print(f"{SYMBOL_INFO} Subtitle remux command: {shlex.join(subtitle_command)}")
            subtitle_result = run_ffmpeg(
                subtitle_command,
                subtitle_source,
                temp_file,
                media_duration(input_probe),
                get_frame_rate(input_probe),
                enforce_progress_duration=False,
            )
            run_result.elapsed += subtitle_result.elapsed
            try:
                os.remove(encoded_av_file)
            except OSError:
                pass
            if subtitle_result.cancelled:
                return ConversionResult("cancelled", "Cancelled", run_result.elapsed)
            if not subtitle_result.success:
                return ConversionResult(
                    "failed",
                    f"Subtitle remux failed: {subtitle_result.error}",
                    run_result.elapsed,
                )

        valid, validation_error = validate_output(
            input_probe,
            temp_file,
            audio_mappings,
            require_encoded_layouts=mode == "encode",
            expected_subtitle_mappings=subtitle_mappings,
        )
        if not valid:
            print(f"{SYMBOL_ERROR} Output validation failed: {validation_error}")
            return ConversionResult(
                "failed",
                f"Final output validation failed: {validation_error}",
                run_result.elapsed,
            )

        print(f"{SYMBOL_SUCCESS} FFmpeg completed and the output passed validation")
        show_time(run_result.elapsed)
        return ConversionResult("success", elapsed=run_result.elapsed)

    except subprocess.CalledProcessError as error:
        diagnostic = (error.stderr or "").strip() if hasattr(error, "stderr") else ""
        reason = diagnostic or str(error)
        print(f"{SYMBOL_ERROR} Probe or FFmpeg error: {reason}")
        return ConversionResult("failed", f"Media probe failed: {reason}")
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as error:
        print(f"{SYMBOL_ERROR} Unexpected processing error: {error}")
        return ConversionResult(
            "failed",
            f"Unexpected processing error: {error}",
        )
    finally:
        encoded_av_file = locals().get("encoded_av_file")
        if encoded_av_file and encoded_av_file != temp_file and os.path.exists(encoded_av_file):
            try:
                os.remove(encoded_av_file)
            except OSError:
                pass


def processing_directory() -> str:
    configured = os.environ.get("MP4_PROCESSING_DIR")
    if configured:
        directory = configured
    elif os.path.isdir("/data/downloads"):
        directory = "/data/downloads/processing"
    else:
        directory = os.path.join(tempfile.gettempdir(), "mp4-converter-processing")
    os.makedirs(directory, exist_ok=True)
    return directory


def paths_refer_to_same_file(first_path: str, second_path: str) -> bool:
    """Resolve same-file identity, including case aliases on shared volumes."""
    try:
        if os.path.exists(first_path) and os.path.exists(second_path):
            return os.path.samefile(first_path, second_path)
    except OSError:
        pass
    return os.path.realpath(first_path) == os.path.realpath(second_path)


def log_unsuccessful_conversion(
    file_name: str,
    input_file: str,
    output_file: str,
    result: ConversionResult,
) -> None:
    """Persist a complete, grep-friendly summary for every unsuccessful file."""
    if result.status == "skipped":
        heading = "SKIPPED"
        symbol = SYMBOL_WARNING
    elif result.status == "cancelled":
        heading = "CANCELLED"
        symbol = SYMBOL_WARNING
    else:
        heading = "FAILED"
        symbol = SYMBOL_ERROR

    reason = result.reason.strip() or "No failure reason was reported"
    print(f"{symbol}{heading}: {file_name}")
    print(f"{symbol}Reason: {reason}")
    print(f"{SYMBOL_INFO}Source: {input_file}")
    print(f"{SYMBOL_INFO}Intended output: {output_file}")
    print(f"{SYMBOL_INFO}Original kept: {'yes' if os.path.exists(input_file) else 'no'}")
    if result.elapsed > 0:
        print(f"{SYMBOL_INFO}Failed after: {format_duration(result.elapsed)}")


def commit_validated_output(
    staged_output: str,
    input_file: str,
    output_file: str,
    replacing_source: bool,
) -> None:
    """Commit a validated output without exposing a partial destination file.

    The potentially cross-volume copy happens under a unique sibling name in the
    destination directory. Only after that copy is complete and readable do we
    rename it to the final path. When input and output are the same file, that
    final rename atomically replaces the source and no separate deletion occurs.
    """
    output_directory = os.path.dirname(output_file)
    os.makedirs(output_directory, exist_ok=True)

    descriptor, sibling_output = tempfile.mkstemp(
        dir=output_directory,
        prefix=".mp4-steward-final-",
        suffix=".mp4",
    )
    os.close(descriptor)

    try:
        expected_size = os.path.getsize(staged_output)
        shutil.copyfile(staged_output, sibling_output)
        if os.path.getsize(sibling_output) != expected_size:
            raise OSError("destination-volume copy size did not match the validated output")

        copied_probe = probe_media(sibling_output)
        copied_streams = copied_probe.get("streams", [])
        if not any(stream.get("codec_type") == "video" for stream in copied_streams):
            raise OSError("destination-volume copy could not be read as a video")
        if any(stream.get("codec_type") == "data" for stream in copied_streams):
            raise OSError("destination-volume copy contains unsupported auxiliary data tracks")

        if replacing_source:
            if not os.path.exists(input_file):
                raise OSError("source disappeared before it could be safely replaced")
        elif os.path.exists(output_file):
            raise FileExistsError(f"destination appeared during processing: {output_file}")

        os.replace(sibling_output, output_file)
        sibling_output = ""

        if not os.path.isfile(output_file) or os.path.getsize(output_file) != expected_size:
            raise OSError("committed output could not be verified")

        if not replacing_source:
            os.remove(input_file)
    finally:
        if sibling_output and os.path.exists(sibling_output):
            try:
                os.remove(sibling_output)
            except OSError as error:
                print(
                    f"{SYMBOL_WARNING} Could not remove destination staging file "
                    f"{sibling_output}: {error}"
                )


def process_folder(
    input_path: str,
    output_path: str,
    first_run: str,
    create_subfolders: bool,
    automatic_rename: bool = True,
    smart_target_mb_per_minute: float = DEFAULT_SMART_TARGET_MB_PER_MINUTE,
) -> bool:
    """Process a folder and return True only when every discovered file succeeds."""
    global totalRunTime
    totalRunTime = 0
    CANCELLATION_REQUESTED.clear()

    mode = first_run.lower()
    if mode not in {"smart", "encode", "remux"}:
        print(f"{SYMBOL_ERROR} Mode must be 'smart', 'encode', or 'remux'")
        return False
    if not os.path.isdir(input_path):
        print(f"{SYMBOL_ERROR} Input directory does not exist: {input_path}")
        return False

    try:
        os.makedirs(output_path, exist_ok=True)
        executable_path("ffmpeg")
        executable_path("ffprobe")
        temp_directory = processing_directory()
    except (OSError, RuntimeError) as error:
        print(f"{SYMBOL_ERROR} Setup failed: {error}")
        return False

    smart_target_line = (
        f"Smart Target       : {smart_target_mb_per_minute:g} MB/min\n"
        if mode == "smart"
        else ""
    )
    print(
        f"Started             : {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"Input Directory    : {input_path}\n"
        f"Output Directory   : {output_path}\n"
        f"Mode               : {mode}\n"
        f"{smart_target_line}"
        f"Create Subfolders  : {create_subfolders}\n"
        f"Automatic Rename   : {automatic_rename}\n"
        f"Processing Temp    : {temp_directory}\n"
        f"FFmpeg             : {tool_version('ffmpeg')}\n"
        f"FFprobe            : {tool_version('ffprobe')}"
    )

    words_to_ignore = {"sample", ".ds_store"}
    video_formats = {".mkv", ".mp4", ".avi", ".mov", ".m4v"}
    files = [
        file_name
        for file_name in sorted(os.listdir(input_path), key=str.casefold)
        if os.path.isfile(os.path.join(input_path, file_name))
        and os.path.splitext(file_name)[1].lower() in video_formats
        and not any(word in file_name.lower() for word in words_to_ignore)
    ]

    if not files:
        print(f"{SYMBOL_ERROR} No supported video files were found")
        return False

    completed = 0
    failed = 0
    skipped = 0
    total_original_bytes = 0
    total_output_bytes = 0

    for index, file_name in enumerate(files, 1):
        if CANCELLATION_REQUESTED.is_set():
            break

        input_file = os.path.join(input_path, file_name)
        source_stem = os.path.splitext(file_name)[0]
        output_stem = (
            automatic_output_stem(file_name)
            if automatic_rename
            else source_stem
        )
        output_file_name = output_stem + ".mp4"
        output_directory = (
            os.path.join(output_path, output_stem)
            if create_subfolders
            else output_path
        )
        output_file = os.path.join(output_directory, output_file_name)
        replacing_source = paths_refer_to_same_file(input_file, output_file)

        print(f"\n{SYMBOL_INFO} File {index}/{len(files)}")
        print(f"{SYMBOL_INFO} Input: {input_file}")
        if output_stem != source_stem:
            print(f"{SYMBOL_INFO} Automatic name: {output_file_name}")
        print(f"{SYMBOL_INFO} Output: {output_file}")

        if os.path.exists(output_file) and not replacing_source:
            log_unsuccessful_conversion(
                file_name,
                input_file,
                output_file,
                ConversionResult(
                    "failed",
                    f"Destination already exists: {output_file}",
                ),
            )
            failed += 1
            continue

        descriptor, temp_output = tempfile.mkstemp(dir=temp_directory, suffix=".mp4")
        os.close(descriptor)

        try:
            result = convert_to_mp4(
                input_file,
                temp_output,
                mode,
                smart_target_mb_per_minute,
            )
            if not result.success:
                totalRunTime += result.elapsed
                log_unsuccessful_conversion(
                    file_name,
                    input_file,
                    output_file,
                    result,
                )
                if result.status == "skipped":
                    skipped += 1
                elif result.status == "cancelled":
                    print(f"{SYMBOL_WARNING} Batch cancelled")
                else:
                    failed += 1
                continue

            original_size = os.path.getsize(input_file)
            output_size = os.path.getsize(temp_output)
            if replacing_source:
                print(f"{SYMBOL_INFO} Safely replacing the source with the validated output")
            commit_validated_output(
                staged_output=temp_output,
                input_file=input_file,
                output_file=output_file,
                replacing_source=replacing_source,
            )
            completed += 1
            total_original_bytes += original_size
            total_output_bytes += output_size
            saved = original_size - output_size
            print(f"{SYMBOL_SUCCESS} Saved: {output_file}")
            print(
                f"{SYMBOL_INFO} Size: {formatted_bytes(original_size)} → "
                f"{formatted_bytes(output_size)} | Saved {formatted_bytes(saved)}"
            )
        except OSError as error:
            failed += 1
            log_unsuccessful_conversion(
                file_name,
                input_file,
                output_file,
                ConversionResult(
                    "failed",
                    f"Finalizing validated output failed: {error}",
                ),
            )
        finally:
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError as error:
                    print(f"{SYMBOL_WARNING} Could not remove temporary file {temp_output}: {error}")

    cancelled = CANCELLATION_REQUESTED.is_set()
    saved_total = total_original_bytes - total_output_bytes
    print("\n════════ MP4 Converter Summary ════════")
    print(f"Completed: {completed}")
    print(f"Skipped:   {skipped}")
    print(f"Failed:    {failed}")
    print(f"Runtime:   {format_duration(totalRunTime)}")
    if completed:
        print(
            f"Size:      {formatted_bytes(total_original_bytes)} → "
            f"{formatted_bytes(total_output_bytes)}"
        )
        print(f"Saved:     {formatted_bytes(saved_total)}")
    if cancelled:
        print("Status:    Cancelled")
    else:
        print("Status:    Success" if failed == 0 and skipped == 0 else "Status:    Needs attention")
    print("══════════════════════════════════════")

    success = not cancelled and completed > 0 and failed == 0 and skipped == 0
    if success:
        print(f"{SYMBOL_FINAL} All files processed successfully")
    return success


def main(arguments: list[str]) -> int:
    def print_usage() -> None:
        print(
            "Usage: MP4_Steward.py <inputPath> <outputPath> "
            "<smart=true|encode=true|remux=true> subfolders=<true|false> "
            "[rename=<true|false>] [smart_target=<MB/min>]"
        )

    if len(arguments) < 4:
        print_usage()
        return 2

    def parse_boolean(value: str, argument_name: str) -> bool | None:
        normalized = value.strip().lower()
        if normalized in {"true", "t", "yes", "y", "1"}:
            return True
        if normalized in {"false", "f", "no", "n", "0"}:
            return False
        print(f"{SYMBOL_ERROR} {argument_name} must be a recognizable boolean value")
        return None

    option_arguments = arguments[3:]
    if not all("=" in argument for argument in option_arguments):
        print(f"{SYMBOL_ERROR} Options must use the documented name=value format")
        print_usage()
        return 2

    mode: str | None = None
    create_subfolders: bool | None = None
    automatic_rename_value = os.environ.get("MP4_AUTOMATIC_RENAME", "True")
    smart_target_value = DEFAULT_SMART_TARGET_MB_PER_MINUTE

    for argument in option_arguments:
        raw_name, raw_value = argument.split("=", 1)
        name = raw_name.strip().lower()
        value = raw_value.strip()

        if name in {"smart", "encode", "remux"}:
            enabled = parse_boolean(value, name)
            if enabled is None:
                return 2
            if enabled:
                if mode is not None and mode != name:
                    print(
                        f"{SYMBOL_ERROR} Choose one of smart=true, encode=true, "
                        "or remux=true"
                    )
                    return 2
                mode = name
        elif name == "subfolders":
            create_subfolders = parse_boolean(value, "subfolders")
            if create_subfolders is None:
                return 2
        elif name == "rename":
            automatic_rename_value = value
        elif name in {"smart_target", "smart-target"}:
            try:
                smart_target_value = float(value)
            except ValueError:
                print(f"{SYMBOL_ERROR} smart_target must be a number in MB/min")
                return 2
            if smart_target_value <= 0:
                print(f"{SYMBOL_ERROR} smart_target must be greater than zero")
                return 2
        else:
            print(f"{SYMBOL_ERROR} Unknown option: {raw_name}")
            print_usage()
            return 2

    if mode is None:
        print(f"{SYMBOL_ERROR} Specify smart=true, encode=true, or remux=true")
        return 2
    if create_subfolders is None:
        print(f"{SYMBOL_ERROR} Specify subfolders=true or subfolders=false")
        return 2

    automatic_rename = parse_boolean(automatic_rename_value, "rename")
    if automatic_rename is None:
        return 2

    return 0 if process_folder(
        arguments[1],
        arguments[2],
        mode,
        create_subfolders,
        automatic_rename,
        smart_target_value,
    ) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
