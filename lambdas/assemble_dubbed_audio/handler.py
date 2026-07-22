import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_OUTPUT_PREFIX = "audio_output/assembled"
DEFAULT_TRAILING_PADDING_SECONDS = 2.0
DEFAULT_AUDIO_TIMING_OFFSET_MS = 0
DEFAULT_LOUDNESS_TARGET_LUFS = -16
DEFAULT_LOUDNESS_LRA = 11
DEFAULT_LOUDNESS_TRUE_PEAK = -1.5
DEFAULT_FIT_TO_SEGMENT_DURATION = True
DEFAULT_MIN_TEMPO_FACTOR = 0.80
DEFAULT_MAX_TEMPO_FACTOR = 1.20
DEFAULT_TARGET_PADDING_MS = 120
DEFAULT_SHORT_SEGMENT_PADDING_THRESHOLD_MS = 1500
DEFAULT_FFMPEG_SEGMENT_CHUNK_SIZE = 80
DEFAULT_FFPROBE_CONCURRENCY = 8
MIN_FFPROBE_CONCURRENCY = 1
MAX_FFPROBE_CONCURRENCY = 16


def _log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str, separators=(",", ":")))


def _remaining_time_ms(context: Any | None) -> int | None:
    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return None
    try:
        return int(context.get_remaining_time_in_millis())
    except Exception:
        return None


def _record_stage(
    stage_timings: list[dict[str, Any]],
    stage: str,
    started_at: float,
    context: Any | None = None,
    **fields: Any,
) -> dict[str, Any]:
    entry = {
        "stage": stage,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        **fields,
    }
    remaining_ms = _remaining_time_ms(context)
    if remaining_ms is not None:
        entry["remaining_time_ms"] = remaining_ms
    stage_timings.append(entry)
    _log_event("assemble_stage_complete", **entry)
    return entry


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def assembled_audio_key(job_name: str, output_prefix: str = DEFAULT_OUTPUT_PREFIX) -> str:
    return f"{output_prefix.rstrip('/')}/{job_name}/dubbed.m4a"


def assembly_diagnostics_key(job_name: str, output_prefix: str = DEFAULT_OUTPUT_PREFIX) -> str:
    return f"{output_prefix.rstrip('/')}/{job_name}/diagnostics.json"


def sorted_audio_segments(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    segments = list(manifest.get("audio_segments") or [])
    if not segments:
        raise ValueError("Manifest does not contain any audio_segments")
    return sorted(segments, key=lambda segment: int(segment.get("offset_ms", 0)))


def _format_filter_number(value: float | int) -> str:
    numeric_value = float(value)
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return f"{numeric_value:.6f}".rstrip("0").rstrip(".")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _clamp_int(value: int, lower_bound: int, upper_bound: int) -> int:
    return min(max(value, lower_bound), upper_bound)


def assembly_options(manifest: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_options = {}
    raw_options.update(manifest.get("assembly_options") or {})
    if event:
        raw_options.update((event.get("workflow_input") or {}).get("assembly_options") or {})
        raw_options.update(event.get("assembly_options") or {})

    options = {
        "audio_timing_offset_ms": int(raw_options.get("audio_timing_offset_ms", DEFAULT_AUDIO_TIMING_OFFSET_MS)),
        "loudness_target_lufs": float(raw_options.get("loudness_target_lufs", DEFAULT_LOUDNESS_TARGET_LUFS)),
        "loudness_lra": float(raw_options.get("loudness_lra", DEFAULT_LOUDNESS_LRA)),
        "loudness_true_peak": float(raw_options.get("loudness_true_peak", DEFAULT_LOUDNESS_TRUE_PEAK)),
        "fit_to_segment_duration": _as_bool(raw_options.get("fit_to_segment_duration", DEFAULT_FIT_TO_SEGMENT_DURATION)),
        "min_tempo_factor": float(raw_options.get("min_tempo_factor", DEFAULT_MIN_TEMPO_FACTOR)),
        "max_tempo_factor": float(raw_options.get("max_tempo_factor", DEFAULT_MAX_TEMPO_FACTOR)),
        "target_padding_ms": int(raw_options.get("target_padding_ms", DEFAULT_TARGET_PADDING_MS)),
        "short_segment_padding_threshold_ms": int(
            raw_options.get("short_segment_padding_threshold_ms", DEFAULT_SHORT_SEGMENT_PADDING_THRESHOLD_MS)
        ),
        "ffmpeg_segment_chunk_size": int(
            raw_options.get("ffmpeg_segment_chunk_size", DEFAULT_FFMPEG_SEGMENT_CHUNK_SIZE)
        ),
        "ffprobe_concurrency": _clamp_int(
            int(raw_options.get("ffprobe_concurrency", DEFAULT_FFPROBE_CONCURRENCY)),
            MIN_FFPROBE_CONCURRENCY,
            MAX_FFPROBE_CONCURRENCY,
        ),
    }

    if options["min_tempo_factor"] <= 0 or options["max_tempo_factor"] <= 0:
        raise ValueError("min_tempo_factor and max_tempo_factor must be positive")
    if options["min_tempo_factor"] > options["max_tempo_factor"]:
        raise ValueError("min_tempo_factor must be less than or equal to max_tempo_factor")
    if options["target_padding_ms"] < 0:
        raise ValueError("target_padding_ms must be non-negative")
    if options["short_segment_padding_threshold_ms"] < 0:
        raise ValueError("short_segment_padding_threshold_ms must be non-negative")
    if options["ffmpeg_segment_chunk_size"] <= 0:
        raise ValueError("ffmpeg_segment_chunk_size must be positive")
    return options


def apply_timing_offset(audio_segments: list[dict[str, Any]], audio_timing_offset_ms: int) -> list[dict[str, Any]]:
    adjusted_segments = []
    for segment in audio_segments:
        adjusted_segment = dict(segment)
        original_offset_ms = int(adjusted_segment.get("offset_ms", 0))
        adjusted_segment["effective_offset_ms"] = max(0, original_offset_ms + audio_timing_offset_ms)
        adjusted_segments.append(adjusted_segment)
    return adjusted_segments


def _segment_timeline_end_seconds(segment: dict[str, Any]) -> float:
    effective_offset_seconds = int(segment.get("effective_offset_ms", segment.get("offset_ms", 0))) / 1000.0
    original_offset_seconds = int(segment.get("offset_ms", 0)) / 1000.0
    start_time = float(segment.get("start_time", original_offset_seconds) or 0.0)
    end_time = float(segment.get("end_time") or 0.0)
    source_duration = max(0.0, end_time - start_time)
    final_duration = max(0.0, float(segment.get("final_duration_ms", 0) or 0) / 1000.0)

    if source_duration or final_duration:
        return effective_offset_seconds + max(source_duration, final_duration)
    return max(effective_offset_seconds, end_time)


def _segment_timeline_end_ms(segment: dict[str, Any]) -> int:
    effective_offset_ms = int(segment.get("effective_offset_ms", segment.get("offset_ms", 0)))
    source_duration_ms = _source_duration_ms(segment)
    final_duration_ms = int(segment.get("final_duration_ms", 0) or 0)
    return effective_offset_ms + max(source_duration_ms, final_duration_ms)


def timeline_duration_seconds(
    audio_segments: list[dict[str, Any]],
    trailing_padding_seconds: float = DEFAULT_TRAILING_PADDING_SECONDS,
) -> float:
    latest_end = 0.0
    for segment in audio_segments:
        latest_end = max(latest_end, _segment_timeline_end_seconds(segment))
    return round(latest_end + trailing_padding_seconds, 3)


def build_filter_complex(
    audio_segments: list[dict[str, Any]],
    loudness_target_lufs: float = DEFAULT_LOUDNESS_TARGET_LUFS,
    loudness_lra: float = DEFAULT_LOUDNESS_LRA,
    loudness_true_peak: float = DEFAULT_LOUDNESS_TRUE_PEAK,
    apply_loudnorm: bool = True,
) -> str:
    delayed_labels = []
    filter_parts = []

    for index, segment in enumerate(audio_segments):
        offset_ms = int(segment.get("effective_offset_ms", segment.get("offset_ms", 0)))
        tempo_factor = float(segment.get("applied_tempo_factor", 1.0))
        label = f"a{index}"
        delayed_labels.append(f"[{label}]")
        input_index = index + 1
        filter_parts.append(
            f"[{input_index}:a]aresample=48000,"
            f"atempo={_format_filter_number(tempo_factor)},"
            f"adelay={offset_ms}:all=1[{label}]"
        )

    input_labels = "[0:a]" + "".join(delayed_labels)
    filter_parts.append(
        f"{input_labels}amix=inputs={len(audio_segments) + 1}:duration=longest:dropout_transition=0:normalize=0,"
        f"{_loudnorm_filter(loudness_target_lufs, loudness_lra, loudness_true_peak) if apply_loudnorm else ''}"
        "aresample=48000[aout]"
    )
    return ";".join(filter_parts)


def _loudnorm_filter(loudness_target_lufs: float, loudness_lra: float, loudness_true_peak: float) -> str:
    return (
        f"loudnorm=I={_format_filter_number(loudness_target_lufs)}:"
        f"LRA={_format_filter_number(loudness_lra)}:"
        f"TP={_format_filter_number(loudness_true_peak)},"
    )


def build_input_mix_filter_complex(
    input_count: int,
    loudness_target_lufs: float = DEFAULT_LOUDNESS_TARGET_LUFS,
    loudness_lra: float = DEFAULT_LOUDNESS_LRA,
    loudness_true_peak: float = DEFAULT_LOUDNESS_TRUE_PEAK,
    apply_loudnorm: bool = True,
) -> str:
    if input_count <= 0:
        raise ValueError("input_count must be positive")
    input_labels = "[0:a]" + "".join(f"[{index}:a]" for index in range(1, input_count + 1))
    return (
        f"{input_labels}amix=inputs={input_count + 1}:duration=longest:dropout_transition=0:normalize=0,"
        f"{_loudnorm_filter(loudness_target_lufs, loudness_lra, loudness_true_peak) if apply_loudnorm else ''}"
        "aresample=48000[aout]"
    )


def build_delayed_input_mix_filter_complex(
    input_delays_ms: list[int],
    loudness_target_lufs: float = DEFAULT_LOUDNESS_TARGET_LUFS,
    loudness_lra: float = DEFAULT_LOUDNESS_LRA,
    loudness_true_peak: float = DEFAULT_LOUDNESS_TRUE_PEAK,
    apply_loudnorm: bool = True,
) -> str:
    if not input_delays_ms:
        raise ValueError("input_delays_ms must not be empty")

    delayed_labels = []
    filter_parts = []
    for index, delay_ms in enumerate(input_delays_ms):
        label = f"c{index}"
        delayed_labels.append(f"[{label}]")
        input_index = index + 1
        filter_parts.append(f"[{input_index}:a]aresample=48000,adelay={int(delay_ms)}:all=1[{label}]")

    input_labels = "[0:a]" + "".join(delayed_labels)
    filter_parts.append(
        f"{input_labels}amix=inputs={len(input_delays_ms) + 1}:duration=longest:dropout_transition=0:normalize=0,"
        f"{_loudnorm_filter(loudness_target_lufs, loudness_lra, loudness_true_peak) if apply_loudnorm else ''}"
        "aresample=48000[aout]"
    )
    return ";".join(filter_parts)


def build_assembly_diagnostics(
    original_audio_segments: list[dict[str, Any]],
    audio_segments: list[dict[str, Any]],
    options_applied: dict[str, Any],
    filter_complex: str,
    stage_timings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    segment_diagnostics = [
        {
            "segment_id": segment.get("segment_id"),
            "speaker": segment.get("speaker"),
            "voice_id": segment.get("voice_id"),
            "source_duration_ms": segment.get("source_duration_ms"),
            "polly_duration_ms": segment.get("polly_duration_ms"),
            "target_duration_ms": segment.get("target_duration_ms"),
            "target_padding_applied_ms": segment.get("target_padding_applied_ms"),
            "required_tempo_factor": segment.get("required_tempo_factor"),
            "applied_tempo_factor": segment.get("applied_tempo_factor"),
            "tempo_was_clamped": segment.get("tempo_was_clamped"),
            "remaining_gap_or_overlap_ms": segment.get("remaining_gap_or_overlap_ms"),
            "requires_upstream_fix": segment.get("requires_upstream_fix"),
        }
        for segment in audio_segments
    ]

    return {
        "assembly_options_applied": options_applied,
        "original_first_offset_ms": int(original_audio_segments[0].get("offset_ms", 0)),
        "effective_first_offset_ms": int(audio_segments[0].get("effective_offset_ms", 0)),
        "original_last_offset_ms": int(original_audio_segments[-1].get("offset_ms", 0)),
        "effective_last_offset_ms": int(audio_segments[-1].get("effective_offset_ms", 0)),
        "loudness_target_lufs": options_applied["loudness_target_lufs"],
        "segments_requiring_upstream_fix": sum(1 for segment in audio_segments if segment.get("requires_upstream_fix")),
        "segment_diagnostics": segment_diagnostics,
        "ffmpeg_filter_complex": filter_complex,
        "stage_timings": stage_timings or [],
    }


def build_ffmpeg_command(
    ffmpeg_path: str,
    segment_paths: list[Path],
    output_path: Path,
    filter_complex: str,
    duration_seconds: float,
) -> list[str]:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-t",
        str(duration_seconds),
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
    ]

    for segment_path in segment_paths:
        command.extend(["-i", str(segment_path)])

    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    )
    return command


def build_silence_command(ffmpeg_path: str, output_path: Path, duration_seconds: float) -> list[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-t",
        _format_filter_number(duration_seconds),
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(output_path),
    ]


def build_concat_command(ffmpeg_path: str, concat_list_path: Path, output_path: Path) -> list[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c",
        "copy",
        str(output_path),
    ]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _planned_chunk(paths: list[Path], segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "paths": paths,
        "segments": segments,
        "start_ms": min(int(segment.get("effective_offset_ms", segment.get("offset_ms", 0))) for segment in segments),
        "end_ms": max(_segment_timeline_end_ms(segment) for segment in segments),
    }


def plan_timeline_chunks(
    segment_paths: list[Path],
    audio_segments: list[dict[str, Any]],
    target_chunk_size: int,
) -> list[dict[str, Any]]:
    if target_chunk_size <= 0:
        raise ValueError("target_chunk_size must be positive")
    if len(segment_paths) != len(audio_segments):
        raise ValueError("segment_paths and audio_segments must have the same length")

    ordered_pairs = sorted(
        zip(segment_paths, audio_segments, strict=True),
        key=lambda pair: (
            int(pair[1].get("effective_offset_ms", pair[1].get("offset_ms", 0))),
            str(pair[1].get("segment_id", "")),
        ),
    )
    planned_chunks: list[dict[str, Any]] = []
    current_paths: list[Path] = []
    current_segments: list[dict[str, Any]] = []
    current_end_ms = 0

    for segment_path, segment in ordered_pairs:
        segment_start_ms = int(segment.get("effective_offset_ms", segment.get("offset_ms", 0)))
        segment_end_ms = _segment_timeline_end_ms(segment)
        if current_segments and len(current_segments) >= target_chunk_size and segment_start_ms >= current_end_ms:
            planned_chunks.append(_planned_chunk(current_paths, current_segments))
            current_paths = []
            current_segments = []
            current_end_ms = 0

        current_paths.append(segment_path)
        current_segments.append(segment)
        current_end_ms = max(current_end_ms, segment_end_ms)

    if current_segments:
        planned_chunks.append(_planned_chunk(current_paths, current_segments))
    return planned_chunks


def _localize_chunk_segments(audio_segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, float]:
    chunk_start_ms = min(int(segment.get("effective_offset_ms", segment.get("offset_ms", 0))) for segment in audio_segments)
    chunk_end_ms = max(_segment_timeline_end_ms(segment) for segment in audio_segments)
    localized_segments = []
    for segment in audio_segments:
        localized_segment = dict(segment)
        localized_segment["effective_offset_ms"] = int(
            localized_segment.get("effective_offset_ms", localized_segment.get("offset_ms", 0))
        ) - chunk_start_ms
        localized_segments.append(localized_segment)
    return localized_segments, chunk_start_ms, max(0.001, (chunk_end_ms - chunk_start_ms) / 1000.0)


def _concat_file_line(path: Path) -> str:
    escaped_path = str(path).replace("'", "'\\''")
    return f"file '{escaped_path}'"


def _write_concat_list(concat_list_path: Path, input_paths: list[Path]) -> None:
    concat_list_path.write_text("\n".join(_concat_file_line(path) for path in input_paths) + "\n", encoding="utf-8")


def _render_silence_file(ffmpeg_path: str, output_path: Path, duration_ms: int) -> None:
    run_ffmpeg(build_silence_command(ffmpeg_path, output_path, duration_ms / 1000.0))


def run_assembly_ffmpeg(
    ffmpeg_path: str,
    segment_paths: list[Path],
    audio_segments: list[dict[str, Any]],
    output_path: Path,
    work_dir: Path,
    duration_seconds: float,
    options_applied: dict[str, Any],
    stage_timings: list[dict[str, Any]] | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    stage_timings = stage_timings if stage_timings is not None else []
    chunk_size = int(options_applied["ffmpeg_segment_chunk_size"])
    loudness_target_lufs = float(options_applied["loudness_target_lufs"])
    loudness_lra = float(options_applied["loudness_lra"])
    loudness_true_peak = float(options_applied["loudness_true_peak"])

    if len(audio_segments) <= chunk_size:
        filter_complex = build_filter_complex(
            audio_segments,
            loudness_target_lufs,
            loudness_lra,
            loudness_true_peak,
        )
        command = build_ffmpeg_command(ffmpeg_path, segment_paths, output_path, filter_complex, duration_seconds)
        _log_event(
            "assemble_ffmpeg_start",
            stage="single_pass_mix",
            input_count=len(segment_paths),
            duration_seconds=duration_seconds,
            remaining_time_ms=_remaining_time_ms(context),
        )
        stage_started = time.perf_counter()
        run_ffmpeg(command)
        _record_stage(
            stage_timings,
            "single_pass_mix",
            stage_started,
            context,
            input_count=len(segment_paths),
            duration_seconds=duration_seconds,
        )
        return {
            "ffmpeg_mix_mode": "single_pass",
            "ffmpeg_segment_chunk_size": chunk_size,
            "ffmpeg_chunk_count": 1,
            "ffmpeg_filter_complex": filter_complex,
        }

    chunk_paths: list[Path] = []
    planned_chunks = plan_timeline_chunks(segment_paths, audio_segments, chunk_size)

    for chunk_index, planned_chunk in enumerate(planned_chunks):
        path_chunk = planned_chunk["paths"]
        segment_chunk = planned_chunk["segments"]
        chunk_output_path = work_dir / f"chunk_{chunk_index:04d}.m4a"
        localized_segment_chunk, chunk_delay_ms, chunk_duration_seconds = _localize_chunk_segments(segment_chunk)
        chunk_filter_complex = build_filter_complex(
            localized_segment_chunk,
            loudness_target_lufs,
            loudness_lra,
            loudness_true_peak,
        )
        command = build_ffmpeg_command(
            ffmpeg_path,
            path_chunk,
            chunk_output_path,
            chunk_filter_complex,
            chunk_duration_seconds,
        )
        _log_event(
            "assemble_ffmpeg_start",
            stage="chunk_render",
            chunk_index=chunk_index,
            input_count=len(path_chunk),
            duration_seconds=round(chunk_duration_seconds, 3),
            chunk_delay_ms=chunk_delay_ms,
            remaining_time_ms=_remaining_time_ms(context),
        )
        stage_started = time.perf_counter()
        run_ffmpeg(command)
        _record_stage(
            stage_timings,
            "chunk_render",
            stage_started,
            context,
            chunk_index=chunk_index,
            input_count=len(path_chunk),
            duration_seconds=round(chunk_duration_seconds, 3),
            chunk_delay_ms=chunk_delay_ms,
        )
        chunk_paths.append(chunk_output_path)

    _log_event(
        "assemble_ffmpeg_start",
        stage="final_mix",
        input_count=len(chunk_paths),
        duration_seconds=duration_seconds,
        remaining_time_ms=_remaining_time_ms(context),
    )
    stage_started = time.perf_counter()
    concat_input_paths: list[Path] = []
    current_position_ms = 0
    final_duration_ms = int(round(duration_seconds * 1000))
    for chunk_index, (planned_chunk, chunk_path) in enumerate(zip(planned_chunks, chunk_paths, strict=True)):
        gap_ms = int(planned_chunk["start_ms"]) - current_position_ms
        if gap_ms < 0:
            raise ValueError(
                "Cannot concat overlapping chunks: "
                f"chunk {chunk_index} starts at {planned_chunk['start_ms']}ms before previous chunk ends at "
                f"{current_position_ms}ms"
            )
        if gap_ms > 0:
            silence_path = work_dir / f"gap_{chunk_index:04d}.m4a"
            _render_silence_file(ffmpeg_path, silence_path, gap_ms)
            concat_input_paths.append(silence_path)
        concat_input_paths.append(chunk_path)
        current_position_ms = int(planned_chunk["end_ms"])

    trailing_gap_ms = final_duration_ms - current_position_ms
    if trailing_gap_ms < 0:
        raise ValueError(
            "Cannot concat assembled chunks because planned chunk duration exceeds final timeline duration: "
            f"{current_position_ms}ms > {final_duration_ms}ms"
        )
    if trailing_gap_ms > 0:
        trailing_silence_path = work_dir / "gap_trailing.m4a"
        _render_silence_file(ffmpeg_path, trailing_silence_path, trailing_gap_ms)
        concat_input_paths.append(trailing_silence_path)

    concat_list_path = work_dir / "concat.txt"
    _write_concat_list(concat_list_path, concat_input_paths)
    run_ffmpeg(build_concat_command(ffmpeg_path, concat_list_path, output_path))
    _record_stage(
        stage_timings,
        "final_mix",
        stage_started,
        context,
        input_count=len(concat_input_paths),
        duration_seconds=duration_seconds,
    )
    return {
        "ffmpeg_mix_mode": "chunked",
        "ffmpeg_segment_chunk_size": chunk_size,
        "ffmpeg_chunk_count": len(chunk_paths),
        "ffmpeg_filter_complex": "concat_demuxer",
    }


def read_manifest_from_s3(uri: str) -> dict[str, Any]:
    bucket, key = parse_s3_uri(uri)
    import boto3

    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def parse_ffprobe_duration(output: str) -> float:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            duration = float(line)
        except ValueError:
            continue
        if duration > 0:
            return duration
    raise ValueError(f"Could not parse positive ffprobe duration from output: {output!r}")


def measure_audio_duration_seconds(ffprobe_path: str, segment_path: Path) -> float:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(segment_path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "ffprobe failed with exit code "
            f"{completed.returncode}: {completed.stderr[-4000:] or completed.stdout[-4000:]}"
        )
    return parse_ffprobe_duration(completed.stdout)


def _source_duration_ms(segment: dict[str, Any]) -> int:
    offset_seconds = int(segment.get("offset_ms", 0)) / 1000.0
    start_time = float(segment.get("start_time", offset_seconds) or 0.0)
    end_time = float(segment.get("end_time", start_time) or start_time)
    return max(0, int(round((end_time - start_time) * 1000)))


def _target_duration_ms(
    source_duration_ms: int,
    target_padding_ms: int,
    short_segment_padding_threshold_ms: int = DEFAULT_SHORT_SEGMENT_PADDING_THRESHOLD_MS,
) -> tuple[int, int]:
    if source_duration_ms <= 0:
        return 0, 0
    if source_duration_ms < short_segment_padding_threshold_ms:
        return source_duration_ms, 0
    effective_padding_ms = min(target_padding_ms, max(0, source_duration_ms - 250))
    return max(1, source_duration_ms - effective_padding_ms), effective_padding_ms


def _clamp(value: float, lower_bound: float, upper_bound: float) -> float:
    return min(max(value, lower_bound), upper_bound)


def calculate_segment_timing(
    segment: dict[str, Any],
    polly_duration_seconds: float,
    options_applied: dict[str, Any],
) -> dict[str, Any]:
    source_duration_ms = _source_duration_ms(segment)
    polly_duration_ms = max(1, int(round(polly_duration_seconds * 1000)))
    target_duration_ms, target_padding_applied_ms = _target_duration_ms(
        source_duration_ms,
        int(options_applied["target_padding_ms"]),
        int(options_applied["short_segment_padding_threshold_ms"]),
    )

    fit_enabled = bool(options_applied["fit_to_segment_duration"]) and target_duration_ms > 0
    required_tempo_factor = polly_duration_ms / target_duration_ms if target_duration_ms > 0 else 1.0
    if fit_enabled:
        applied_tempo_factor = _clamp(
            required_tempo_factor,
            float(options_applied["min_tempo_factor"]),
            float(options_applied["max_tempo_factor"]),
        )
    else:
        applied_tempo_factor = 1.0

    final_duration_ms = max(1, int(round(polly_duration_ms / applied_tempo_factor)))
    remaining_gap_or_overlap_ms = target_duration_ms - final_duration_ms if target_duration_ms > 0 else 0
    tempo_was_clamped = fit_enabled and abs(required_tempo_factor - applied_tempo_factor) > 0.000001

    adjusted_segment = dict(segment)
    adjusted_segment.update(
        {
            "source_duration_ms": source_duration_ms,
            "polly_duration_ms": polly_duration_ms,
            "target_duration_ms": target_duration_ms,
            "target_padding_applied_ms": target_padding_applied_ms,
            "required_tempo_factor": round(required_tempo_factor, 6),
            "applied_tempo_factor": round(applied_tempo_factor, 6),
            "tempo_was_clamped": tempo_was_clamped,
            "remaining_gap_or_overlap_ms": remaining_gap_or_overlap_ms,
            "final_duration_ms": final_duration_ms,
            "requires_upstream_fix": tempo_was_clamped,
        }
    )
    return adjusted_segment


def apply_segment_timing(
    audio_segments: list[dict[str, Any]],
    segment_paths: list[Path],
    ffprobe_path: str,
    options_applied: dict[str, Any],
) -> list[dict[str, Any]]:
    if not bool(options_applied["fit_to_segment_duration"]):
        timed_segments = []
        for segment in audio_segments:
            source_duration_ms = _source_duration_ms(segment)
            target_duration_ms, target_padding_applied_ms = _target_duration_ms(
                source_duration_ms,
                int(options_applied["target_padding_ms"]),
                int(options_applied["short_segment_padding_threshold_ms"]),
            )
            timed_segment = dict(segment)
            timed_segment.update(
                {
                    "source_duration_ms": source_duration_ms,
                    "polly_duration_ms": None,
                    "target_duration_ms": target_duration_ms,
                    "target_padding_applied_ms": target_padding_applied_ms,
                    "required_tempo_factor": None,
                    "applied_tempo_factor": 1.0,
                    "tempo_was_clamped": False,
                    "remaining_gap_or_overlap_ms": None,
                    "final_duration_ms": 0,
                    "requires_upstream_fix": False,
                }
            )
            timed_segments.append(timed_segment)
        return timed_segments

    ffprobe_concurrency = int(options_applied["ffprobe_concurrency"])

    def measure(segment_path: Path) -> float:
        return measure_audio_duration_seconds(ffprobe_path, segment_path)

    with ThreadPoolExecutor(max_workers=ffprobe_concurrency) as executor:
        durations = list(executor.map(measure, segment_paths))

    return [
        calculate_segment_timing(segment, duration_seconds, options_applied)
        for segment, duration_seconds in zip(audio_segments, durations, strict=True)
    ]


def download_segments(audio_segments: list[dict[str, Any]], work_dir: Path) -> list[Path]:
    import boto3

    s3 = boto3.client("s3")
    segment_paths = []
    for index, segment in enumerate(audio_segments):
        bucket, key = parse_s3_uri(segment["audio_s3_uri"])
        local_path = work_dir / f"segment_{index:04d}.mp3"
        s3.download_file(bucket, key, str(local_path))
        segment_paths.append(local_path)
    return segment_paths


def upload_assembled_audio(bucket: str, key: str, local_path: Path) -> str:
    import boto3

    boto3.client("s3").upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": "audio/mp4"},
    )
    return f"s3://{bucket}/{key}"


def upload_json(bucket: str, key: str, payload: dict[str, Any]) -> str:
    import boto3

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


def run_ffmpeg(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed with exit code "
            f"{completed.returncode}: {completed.stderr[-4000:] or completed.stdout[-4000:]}"
        )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    total_started = time.perf_counter()
    stage_timings: list[dict[str, Any]] = []

    stage_started = time.perf_counter()
    manifest = event.get("manifest")
    if not manifest:
        manifest = read_manifest_from_s3(event["manifest_s3_uri"])
        manifest_source = "s3"
    else:
        manifest_source = "inline"
    _record_stage(stage_timings, "manifest_load", stage_started, _context, manifest_source=manifest_source)

    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    output_prefix = event.get("assembled_audio_prefix") or DEFAULT_OUTPUT_PREFIX
    output_key = event.get("dubbed_audio_key") or assembled_audio_key(manifest["job_name"], output_prefix)
    diagnostics_key = event.get("assembly_diagnostics_key") or assembly_diagnostics_key(manifest["job_name"], output_prefix)
    ffmpeg_path = event.get("ffmpeg_path") or os.environ.get("FFMPEG_PATH", "/opt/bin/ffmpeg")
    ffprobe_path = event.get("ffprobe_path") or os.environ.get("FFPROBE_PATH", "/opt/bin/ffprobe")

    original_audio_segments = sorted_audio_segments(manifest)
    options_applied = assembly_options(manifest, event)
    audio_segments = apply_timing_offset(original_audio_segments, int(options_applied["audio_timing_offset_ms"]))
    source_timeline_duration_seconds = timeline_duration_seconds(audio_segments)
    lambda_memory_mb = getattr(_context, "memory_limit_in_mb", None) or os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
    _log_event(
        "assemble_start",
        job_name=manifest["job_name"],
        segment_count=len(audio_segments),
        source_timeline_duration_seconds=source_timeline_duration_seconds,
        lambda_memory_mb=lambda_memory_mb,
        assembly_options=options_applied,
        remaining_time_ms=_remaining_time_ms(_context),
    )

    work_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"assemble-{manifest['job_name']}"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / "dubbed.m4a"

    stage_started = time.perf_counter()
    segment_paths = download_segments(audio_segments, work_dir)
    _record_stage(
        stage_timings,
        "segment_download",
        stage_started,
        _context,
        segment_count=len(segment_paths),
    )

    stage_started = time.perf_counter()
    audio_segments = apply_segment_timing(audio_segments, segment_paths, ffprobe_path, options_applied)
    _record_stage(
        stage_timings,
        "ffprobe_timing",
        stage_started,
        _context,
        segment_count=len(audio_segments),
        fit_to_segment_duration=bool(options_applied["fit_to_segment_duration"]),
        ffprobe_concurrency=int(options_applied["ffprobe_concurrency"]),
    )
    duration_seconds = timeline_duration_seconds(audio_segments)
    _log_event(
        "assemble_timeline_ready",
        job_name=manifest["job_name"],
        timeline_duration_seconds=duration_seconds,
        remaining_time_ms=_remaining_time_ms(_context),
    )
    ffmpeg_metadata = run_assembly_ffmpeg(
        ffmpeg_path,
        segment_paths,
        audio_segments,
        output_path,
        work_dir,
        duration_seconds,
        options_applied,
        stage_timings,
        _context,
    )
    stage_started = time.perf_counter()
    dubbed_audio_s3_uri = upload_assembled_audio(bucket, output_key, output_path)
    _record_stage(stage_timings, "assembled_audio_upload", stage_started, _context, output_key=output_key)
    diagnostics = build_assembly_diagnostics(
        original_audio_segments,
        audio_segments,
        options_applied,
        ffmpeg_metadata["ffmpeg_filter_complex"],
        stage_timings,
    )
    diagnostics.update(
        {
            "job_name": manifest["job_name"],
            "dubbed_audio_key": output_key,
            "dubbed_audio_s3_uri": dubbed_audio_s3_uri,
            "timeline_duration_seconds": duration_seconds,
            "ffmpeg_mix_mode": ffmpeg_metadata["ffmpeg_mix_mode"],
            "ffmpeg_segment_chunk_size": ffmpeg_metadata["ffmpeg_segment_chunk_size"],
            "ffmpeg_chunk_count": ffmpeg_metadata["ffmpeg_chunk_count"],
            "total_elapsed_seconds": round(time.perf_counter() - total_started, 3),
        }
    )
    stage_started = time.perf_counter()
    diagnostics_s3_uri = upload_json(bucket, diagnostics_key, diagnostics)
    _record_stage(stage_timings, "diagnostics_write", stage_started, _context, diagnostics_key=diagnostics_key)
    _log_event(
        "assemble_complete",
        job_name=manifest["job_name"],
        total_elapsed_seconds=round(time.perf_counter() - total_started, 3),
        diagnostics_s3_uri=diagnostics_s3_uri,
        remaining_time_ms=_remaining_time_ms(_context),
    )

    return {
        "job_name": manifest["job_name"],
        "original_video_uri": manifest["original_video_uri"],
        "translation_engine": manifest.get("translation_engine", "unknown"),
        "dubbed_audio_key": output_key,
        "dubbed_audio_s3_uri": dubbed_audio_s3_uri,
        "assembly_diagnostics_key": diagnostics_key,
        "assembly_diagnostics_s3_uri": diagnostics_s3_uri,
        "timeline_duration_seconds": duration_seconds,
        "assembled_audio_format": "m4a",
        "ffmpeg_mix_mode": ffmpeg_metadata["ffmpeg_mix_mode"],
        "ffmpeg_segment_chunk_size": ffmpeg_metadata["ffmpeg_segment_chunk_size"],
        "ffmpeg_chunk_count": ffmpeg_metadata["ffmpeg_chunk_count"],
    }
