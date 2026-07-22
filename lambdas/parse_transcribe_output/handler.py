import json
import os
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen


SENTENCE_END = {".", "?", "!", "¿", "¡"}
NATURAL_SPLIT_PUNCTUATION = {",", ";", ":"}
FILLER_WORDS = {"eh", "um", "uh", "mmm", "mm", "hmm", "ah", "em"}
DEFAULT_MAX_GAP_SECONDS = 1.25
DEFAULT_MAX_SEGMENT_DURATION_SECONDS = 8.5
DEFAULT_MAX_SEGMENT_WORDS = 24
DEFAULT_OMIT_FILLER_MICRO_SEGMENTS = True
DEFAULT_FILLER_MICRO_SEGMENT_MAX_SECONDS = 0.75
DEFAULT_PAUSE_SPLIT_SECONDS = 0.45
DEFAULT_MIN_DUBBING_SEGMENT_DURATION_SECONDS = 1.25
DEFAULT_MIN_DUBBING_SEGMENT_WORDS = 3
DEFAULT_MAX_MERGE_GAP_SECONDS = 0.85
DEFAULT_SHORT_FRAGMENT_MERGE_GAP_SECONDS = 1.50
DEFAULT_DEGENERATE_SEGMENT_MAX_SECONDS = 0.10
DEFAULT_HARD_MAX_TOLERANCE = 1.20
CONTINUATION_WORDS = {
    "a",
    "al",
    "ante",
    "bajo",
    "como",
    "con",
    "cuando",
    "de",
    "del",
    "desde",
    "donde",
    "e",
    "el",
    "en",
    "entre",
    "hacia",
    "hasta",
    "la",
    "las",
    "los",
    "ni",
    "o",
    "para",
    "por",
    "que",
    "quien",
    "sin",
    "sobre",
    "un",
    "una",
    "unas",
    "unos",
    "y",
}
STRUCTURAL_QUALITY_FLAGS = {
    "long_segment",
    "micro_segment",
    "short_segment",
    "low_word_count",
    "degenerate_timing",
    "short_unmerged",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalise_token(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def _speaker_lookup(transcript: dict[str, Any]) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for segment in transcript.get("results", {}).get("speaker_labels", {}).get("segments", []):
        speaker = segment.get("speaker_label", "spk_0")
        for item in segment.get("items", []):
            start = item.get("start_time")
            end = item.get("end_time")
            if start and end:
                lookup[(str(start), str(end))] = speaker
    return lookup


def _word_items(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    lookup = _speaker_lookup(transcript)
    words: list[dict[str, Any]] = []
    last_word_index: int | None = None

    for item in transcript.get("results", {}).get("items", []):
        item_type = item.get("type")
        alternatives = item.get("alternatives") or []
        content = alternatives[0].get("content", "") if alternatives else ""
        confidence = alternatives[0].get("confidence") if alternatives else None

        if item_type == "pronunciation":
            start = str(item.get("start_time"))
            end = str(item.get("end_time"))
            speaker = item.get("speaker_label") or lookup.get((start, end), "spk_0")
            words.append(
                {
                    "content": content,
                    "start_time": _as_float(start),
                    "end_time": _as_float(end),
                    "speaker": speaker,
                    "confidence": _as_float(confidence, 0.0),
                    "punctuation": "",
                }
            )
            last_word_index = len(words) - 1
        elif item_type == "punctuation" and last_word_index is not None:
            words[last_word_index]["punctuation"] += content

    return words


def _append_token(parts: list[str], token: str, punctuation: str) -> None:
    if not token:
        return
    if parts:
        parts.append(" ")
    parts.append(token)
    if punctuation:
        parts.append(punctuation)


def _segment_text(words: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for word in words:
        _append_token(parts, word["content"], word.get("punctuation", ""))
    return "".join(parts).strip()


def _duration_seconds(words: list[dict[str, Any]]) -> float:
    if not words:
        return 0.0
    return max(0.0, float(words[-1]["end_time"]) - float(words[0]["start_time"]))


def _segment_duration(segment: dict[str, Any]) -> float:
    return max(0.0, float(segment["end_time"]) - float(segment["start_time"]))


def _segment_is_short_duration(segment: dict[str, Any], options: dict[str, Any]) -> bool:
    return _segment_duration(segment) < float(options["min_dubbing_segment_duration_seconds"])


def _segment_is_low_word_count(segment: dict[str, Any], options: dict[str, Any]) -> bool:
    return int(segment.get("word_count", 0)) < int(options["min_dubbing_segment_words"])


def _segment_is_degenerate(segment: dict[str, Any], options: dict[str, Any]) -> bool:
    return _segment_duration(segment) <= float(options["degenerate_segment_max_seconds"])


def _refresh_segment_metrics(segment: dict[str, Any], options: dict[str, Any], extra_flags: set[str] | None = None) -> None:
    duration = _segment_duration(segment)
    word_count = int(segment.get("word_count", 0))
    flags = set(segment.get("quality_flags", [])) | set(extra_flags or set())
    flags -= STRUCTURAL_QUALITY_FLAGS

    if duration > _hard_max_duration(options) or word_count > int(options["max_segment_words"]):
        flags.add("long_segment")
    if duration <= float(options["filler_micro_segment_max_seconds"]):
        flags.add("micro_segment")
    if _segment_is_short_duration(segment, options):
        flags.add("short_segment")
    if _segment_is_low_word_count(segment, options):
        flags.add("low_word_count")
    if duration <= float(options["degenerate_segment_max_seconds"]):
        flags.add("degenerate_timing")
    if segment.get("short_unmerged"):
        flags.add("short_unmerged")

    segment["duration_seconds"] = round(duration, 3)
    segment["word_count"] = word_count
    segment["words_per_second"] = round(word_count / duration, 3) if duration > 0 else 0.0
    segment["quality_flags"] = sorted(flags)


def _segment_from_words(
    words: list[dict[str, Any]],
    reason: str,
    options: dict[str, Any],
    extra_flags: set[str] | None = None,
) -> dict[str, Any]:
    text = _segment_text(words)
    duration = _duration_seconds(words)
    word_count = len(words)
    confidences = [float(word.get("confidence", 0.0)) for word in words]
    quality_flags = set(extra_flags or set())

    segment = {
        "speaker": words[0].get("speaker", "spk_0"),
        "start_time": round(float(words[0]["start_time"]), 3),
        "end_time": round(float(words[-1]["end_time"]), 3),
        "text": text,
        "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "duration_seconds": round(duration, 3),
        "word_count": word_count,
        "words_per_second": round(word_count / duration, 3) if duration > 0 else 0.0,
        "segmentation_reason": reason,
        "quality_flags": sorted(quality_flags),
    }
    _refresh_segment_metrics(segment, options)
    return segment


def _is_filler_only_text(text: str) -> bool:
    tokens = [_normalise_token(token) for token in text.split()]
    tokens = [token for token in tokens if token]
    return bool(tokens) and all(token in FILLER_WORDS for token in tokens)


def _should_omit_segment(segment: dict[str, Any], options: dict[str, Any]) -> bool:
    return (
        bool(options["omit_filler_micro_segments"])
        and float(segment["duration_seconds"]) <= float(options["filler_micro_segment_max_seconds"])
        and _is_filler_only_text(segment["text"])
    )


def _hard_max_duration(options: dict[str, Any]) -> float:
    return float(options["max_segment_duration_seconds"]) * float(options["hard_max_tolerance"])


def _over_hard_duration(words: list[dict[str, Any]], options: dict[str, Any]) -> bool:
    return _duration_seconds(words) > _hard_max_duration(options)


def _limit_reason(words: list[dict[str, Any]], options: dict[str, Any]) -> str:
    duration_exceeded = _duration_seconds(words) > float(options["max_segment_duration_seconds"])
    words_exceeded = len(words) > int(options["max_segment_words"])
    if duration_exceeded and words_exceeded:
        return "max_duration_and_words"
    if duration_exceeded:
        return "max_duration"
    return "max_words"


def _natural_split_candidates(words: list[dict[str, Any]], options: dict[str, Any]) -> list[int]:
    candidates = []
    for index in range(len(words) - 1):
        punctuation = set(words[index].get("punctuation", ""))
        gap_seconds = float(words[index + 1]["start_time"]) - float(words[index]["end_time"])
        if (
            punctuation & SENTENCE_END
            or punctuation & NATURAL_SPLIT_PUNCTUATION
            or gap_seconds >= float(options["pause_split_seconds"])
        ):
            candidates.append(index)
    return candidates


def _within_hard_limits(words: list[dict[str, Any]], options: dict[str, Any]) -> bool:
    return (
        _duration_seconds(words) <= _hard_max_duration(options)
        and len(words) <= int(options["max_segment_words"])
    )


def _meets_minimum_dubbing_window(words: list[dict[str, Any]], options: dict[str, Any]) -> bool:
    return (
        _duration_seconds(words) >= float(options["min_dubbing_segment_duration_seconds"])
        and len(words) >= int(options["min_dubbing_segment_words"])
    )


def _split_markers(index: int, words: list[dict[str, Any]], options: dict[str, Any]) -> tuple[bool, bool, bool]:
    punctuation = set(words[index].get("punctuation", ""))
    gap_seconds = float(words[index + 1]["start_time"]) - float(words[index]["end_time"])
    return (
        bool(punctuation & SENTENCE_END),
        bool(punctuation & NATURAL_SPLIT_PUNCTUATION),
        gap_seconds >= float(options["pause_split_seconds"]),
    )


def _is_continuation_word(word: dict[str, Any]) -> bool:
    return _normalise_token(str(word.get("content", ""))) in CONTINUATION_WORDS


def _is_linguistically_complete_boundary(index: int, words: list[dict[str, Any]]) -> bool:
    return not _is_continuation_word(words[index]) and not _is_continuation_word(words[index + 1])


def _split_candidate_score(index: int, words: list[dict[str, Any]], options: dict[str, Any]) -> tuple[int, ...]:
    sentence_boundary, preferred_punctuation, long_pause = _split_markers(index, words, options)
    left = words[: index + 1]
    remainder = words[index + 1 :]
    soft_target_ms = int(round(float(options["max_segment_duration_seconds"]) * 1000))
    left_duration_ms = int(round(_duration_seconds(left) * 1000))
    return (
        0 if sentence_boundary else 1,
        0 if _is_linguistically_complete_boundary(index, words) else 1,
        0 if preferred_punctuation else 1,
        0 if long_pause else 1,
        0 if _meets_minimum_dubbing_window(remainder, options) else 1,
        abs(left_duration_ms - soft_target_ms),
        -index,
    )


def _best_split_candidate(
    words: list[dict[str, Any]],
    options: dict[str, Any],
    max_duration_seconds: float,
    natural_only: bool,
    require_clean_natural: bool = False,
) -> int | None:
    candidates = []
    natural_candidates = set(_natural_split_candidates(words, options))
    for index in range(len(words) - 1):
        left = words[: index + 1]
        if _duration_seconds(left) > max_duration_seconds or len(left) > int(options["max_segment_words"]):
            continue
        if not _meets_minimum_dubbing_window(left, options):
            continue
        is_natural = index in natural_candidates
        if natural_only and not is_natural:
            continue
        if require_clean_natural:
            sentence_boundary, _, _ = _split_markers(index, words, options)
            if not sentence_boundary and not _is_linguistically_complete_boundary(index, words):
                continue
        candidates.append(index)

    if not candidates:
        return None
    return min(candidates, key=lambda index: _split_candidate_score(index, words, options))


def _should_split_current_words(words: list[dict[str, Any]], options: dict[str, Any]) -> bool:
    if len(words) > int(options["max_segment_words"]):
        return True
    if _duration_seconds(words) <= float(options["max_segment_duration_seconds"]):
        return False
    if _over_hard_duration(words, options):
        return True
    return (
        _best_split_candidate(
            words,
            options,
            _hard_max_duration(options),
            natural_only=True,
            require_clean_natural=True,
        )
        is not None
    )


def _choose_split_index(words: list[dict[str, Any]], options: dict[str, Any]) -> tuple[int, bool]:
    max_words_index = min(max(int(options["max_segment_words"]) - 1, 0), len(words) - 2)
    if len(words) > int(options["max_segment_words"]):
        candidate = _best_split_candidate(
            words,
            options,
            _hard_max_duration(options),
            natural_only=False,
        )
        return (candidate if candidate is not None else max_words_index), True

    natural_candidate = _best_split_candidate(
        words,
        options,
        _hard_max_duration(options),
        natural_only=True,
        require_clean_natural=not _over_hard_duration(words, options),
    )
    if natural_candidate is not None:
        return natural_candidate, False

    forced_candidate = _best_split_candidate(
        words,
        options,
        _hard_max_duration(options),
        natural_only=False,
    )
    if forced_candidate is not None:
        return forced_candidate, True

    split_index = 0
    for index in range(len(words) - 1):
        if _within_hard_limits(words[: index + 1], options):
            split_index = index
    return min(split_index, len(words) - 2), True


def _segmentation_options(
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    max_segment_duration_seconds: float = DEFAULT_MAX_SEGMENT_DURATION_SECONDS,
    max_segment_words: int = DEFAULT_MAX_SEGMENT_WORDS,
    omit_filler_micro_segments: bool = DEFAULT_OMIT_FILLER_MICRO_SEGMENTS,
    filler_micro_segment_max_seconds: float = DEFAULT_FILLER_MICRO_SEGMENT_MAX_SECONDS,
    pause_split_seconds: float = DEFAULT_PAUSE_SPLIT_SECONDS,
    min_dubbing_segment_duration_seconds: float = DEFAULT_MIN_DUBBING_SEGMENT_DURATION_SECONDS,
    min_dubbing_segment_words: int = DEFAULT_MIN_DUBBING_SEGMENT_WORDS,
    max_merge_gap_seconds: float = DEFAULT_MAX_MERGE_GAP_SECONDS,
    short_fragment_merge_gap_seconds: float = DEFAULT_SHORT_FRAGMENT_MERGE_GAP_SECONDS,
    degenerate_segment_max_seconds: float = DEFAULT_DEGENERATE_SEGMENT_MAX_SECONDS,
    hard_max_tolerance: float = DEFAULT_HARD_MAX_TOLERANCE,
) -> dict[str, Any]:
    return {
        "max_gap_seconds": float(max_gap_seconds),
        "max_segment_duration_seconds": float(max_segment_duration_seconds),
        "max_segment_words": int(max_segment_words),
        "omit_filler_micro_segments": bool(omit_filler_micro_segments),
        "filler_micro_segment_max_seconds": float(filler_micro_segment_max_seconds),
        "pause_split_seconds": float(pause_split_seconds),
        "min_dubbing_segment_duration_seconds": float(min_dubbing_segment_duration_seconds),
        "min_dubbing_segment_words": int(min_dubbing_segment_words),
        "max_merge_gap_seconds": float(max_merge_gap_seconds),
        "short_fragment_merge_gap_seconds": float(short_fragment_merge_gap_seconds),
        "degenerate_segment_max_seconds": float(degenerate_segment_max_seconds),
        "hard_max_tolerance": float(hard_max_tolerance),
    }


def _segmentation_options_from_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_options: dict[str, Any] = {}
    raw_options.update(event.get("segmentation_options") or {})
    for key in (
        "max_gap_seconds",
        "max_segment_duration_seconds",
        "max_segment_words",
        "omit_filler_micro_segments",
        "filler_micro_segment_max_seconds",
        "pause_split_seconds",
        "min_dubbing_segment_duration_seconds",
        "min_dubbing_segment_words",
        "max_merge_gap_seconds",
        "short_fragment_merge_gap_seconds",
        "degenerate_segment_max_seconds",
        "hard_max_tolerance",
    ):
        if key in event:
            raw_options[key] = event[key]

    return _segmentation_options(
        max_gap_seconds=raw_options.get("max_gap_seconds", DEFAULT_MAX_GAP_SECONDS),
        max_segment_duration_seconds=raw_options.get("max_segment_duration_seconds", DEFAULT_MAX_SEGMENT_DURATION_SECONDS),
        max_segment_words=raw_options.get("max_segment_words", DEFAULT_MAX_SEGMENT_WORDS),
        omit_filler_micro_segments=_as_bool(
            raw_options.get("omit_filler_micro_segments", DEFAULT_OMIT_FILLER_MICRO_SEGMENTS)
        ),
        filler_micro_segment_max_seconds=raw_options.get(
            "filler_micro_segment_max_seconds", DEFAULT_FILLER_MICRO_SEGMENT_MAX_SECONDS
        ),
        pause_split_seconds=raw_options.get("pause_split_seconds", DEFAULT_PAUSE_SPLIT_SECONDS),
        min_dubbing_segment_duration_seconds=raw_options.get(
            "min_dubbing_segment_duration_seconds", DEFAULT_MIN_DUBBING_SEGMENT_DURATION_SECONDS
        ),
        min_dubbing_segment_words=raw_options.get("min_dubbing_segment_words", DEFAULT_MIN_DUBBING_SEGMENT_WORDS),
        max_merge_gap_seconds=raw_options.get("max_merge_gap_seconds", DEFAULT_MAX_MERGE_GAP_SECONDS),
        short_fragment_merge_gap_seconds=raw_options.get(
            "short_fragment_merge_gap_seconds", DEFAULT_SHORT_FRAGMENT_MERGE_GAP_SECONDS
        ),
        degenerate_segment_max_seconds=raw_options.get(
            "degenerate_segment_max_seconds", DEFAULT_DEGENERATE_SEGMENT_MAX_SECONDS
        ),
        hard_max_tolerance=raw_options.get("hard_max_tolerance", DEFAULT_HARD_MAX_TOLERANCE),
    )


def _segment_gap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    return float(right["start_time"]) - float(left["end_time"])


def _needs_coalescing(segment: dict[str, Any], options: dict[str, Any]) -> bool:
    return _segment_is_short_duration(segment, options) or _segment_is_degenerate(segment, options)


def _merge_gap_limit(left: dict[str, Any], right: dict[str, Any], options: dict[str, Any]) -> float:
    if _needs_coalescing(left, options) or _needs_coalescing(right, options):
        return float(options["short_fragment_merge_gap_seconds"])
    return float(options["max_merge_gap_seconds"])


def _can_merge_segments(left: dict[str, Any], right: dict[str, Any], options: dict[str, Any]) -> bool:
    if left.get("speaker") != right.get("speaker"):
        return False
    if _segment_gap_seconds(left, right) > _merge_gap_limit(left, right, options):
        return False
    combined_duration = max(0.0, float(right["end_time"]) - float(left["start_time"]))
    combined_words = int(left.get("word_count", 0)) + int(right.get("word_count", 0))
    return (
        combined_duration <= float(options["max_segment_duration_seconds"])
        and combined_words <= int(options["max_segment_words"])
    )


def _merge_segments(left: dict[str, Any], right: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    left_words = int(left.get("word_count", 0))
    right_words = int(right.get("word_count", 0))
    total_words = left_words + right_words
    confidence = 0.0
    if total_words:
        confidence = (
            float(left.get("confidence", 0.0)) * left_words + float(right.get("confidence", 0.0)) * right_words
        ) / total_words

    merged = {
        "speaker": left.get("speaker", "spk_0"),
        "start_time": round(float(left["start_time"]), 3),
        "end_time": round(float(right["end_time"]), 3),
        "text": f"{left.get('text', '').strip()} {right.get('text', '').strip()}".strip(),
        "confidence": round(confidence, 4),
        "duration_seconds": 0.0,
        "word_count": total_words,
        "words_per_second": 0.0,
        "segmentation_reason": "coalesced",
        "quality_flags": sorted((set(left.get("quality_flags", [])) | set(right.get("quality_flags", [])) | {"coalesced"})),
    }
    _refresh_segment_metrics(merged, options)
    return merged


def _mark_short_unmerged(segment: dict[str, Any], options: dict[str, Any]) -> None:
    segment["short_unmerged"] = True
    _refresh_segment_metrics(segment, options)


def _natural_duration_distance(duration_seconds: float) -> float:
    if duration_seconds < 2.0:
        return 2.0 - duration_seconds
    if duration_seconds > DEFAULT_MAX_SEGMENT_DURATION_SECONDS:
        return duration_seconds - DEFAULT_MAX_SEGMENT_DURATION_SECONDS
    return 0.0


def _merge_candidate_score(
    direction: str,
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[float, float, int]:
    combined_duration = max(0.0, float(right["end_time"]) - float(left["start_time"]))
    direction_rank = 0 if direction == "forward" else 1
    return (_segment_gap_seconds(left, right), _natural_duration_distance(combined_duration), direction_rank)


def _best_merge_candidate(
    segments: list[dict[str, Any]], index: int, options: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    candidates: list[tuple[tuple[float, float, int], str, dict[str, Any]]] = []
    segment = segments[index]

    if index + 1 < len(segments) and _can_merge_segments(segment, segments[index + 1], options):
        merged = _merge_segments(segment, segments[index + 1], options)
        candidates.append((_merge_candidate_score("forward", segment, segments[index + 1]), "forward", merged))

    if index > 0 and _can_merge_segments(segments[index - 1], segment, options):
        merged = _merge_segments(segments[index - 1], segment, options)
        candidates.append((_merge_candidate_score("backward", segments[index - 1], segment), "backward", merged))

    if not candidates:
        return None

    _, direction, merged = min(candidates, key=lambda candidate: candidate[0])
    return direction, merged


def coalesce_dubbing_segments(segments: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    coalesced_segments = [dict(segment) for segment in segments]
    index = 0
    while index < len(coalesced_segments):
        segment = coalesced_segments[index]
        if not _needs_coalescing(segment, options):
            index += 1
            continue

        candidate = _best_merge_candidate(coalesced_segments, index, options)
        if candidate is None:
            _mark_short_unmerged(segment, options)
            index += 1
            continue

        direction, merged = candidate
        if direction == "forward":
            coalesced_segments[index : index + 2] = [merged]
        else:
            coalesced_segments[index - 1 : index + 1] = [
                merged
            ]
            index = max(0, index - 1)

    for segment in coalesced_segments:
        _refresh_segment_metrics(segment, options)
    return coalesced_segments


def _summarise_segments(
    kept_segments: list[dict[str, Any]],
    omitted_segments: list[dict[str, Any]],
    raw_segment_count: int,
    options: dict[str, Any],
) -> dict[str, Any]:
    return {
        "raw_segment_count": raw_segment_count,
        "segment_count": len(kept_segments),
        "omitted_segment_count": len(omitted_segments),
        "filler_omitted_count": sum(1 for segment in omitted_segments if "filler_omitted" in segment.get("quality_flags", [])),
        "forced_split_count": sum(1 for segment in kept_segments if "forced_split" in segment.get("quality_flags", [])),
        "long_segment_count": sum(1 for segment in kept_segments if "long_segment" in segment.get("quality_flags", [])),
        "micro_segment_count": sum(1 for segment in kept_segments if "micro_segment" in segment.get("quality_flags", [])),
        "coalesced_segment_count": sum(1 for segment in kept_segments if "coalesced" in segment.get("quality_flags", [])),
        "short_segment_count": sum(1 for segment in kept_segments if "short_segment" in segment.get("quality_flags", [])),
        "low_word_count_count": sum(1 for segment in kept_segments if "low_word_count" in segment.get("quality_flags", [])),
        "degenerate_timing_count": sum(1 for segment in kept_segments if "degenerate_timing" in segment.get("quality_flags", [])),
        "short_unmerged_count": sum(1 for segment in kept_segments if "short_unmerged" in segment.get("quality_flags", [])),
        "max_segment_duration_seconds": options["max_segment_duration_seconds"],
        "max_segment_words": options["max_segment_words"],
        "filler_micro_segment_max_seconds": options["filler_micro_segment_max_seconds"],
        "min_dubbing_segment_duration_seconds": options["min_dubbing_segment_duration_seconds"],
        "min_dubbing_segment_words": options["min_dubbing_segment_words"],
        "max_merge_gap_seconds": options["max_merge_gap_seconds"],
        "short_fragment_merge_gap_seconds": options["short_fragment_merge_gap_seconds"],
        "degenerate_segment_max_seconds": options["degenerate_segment_max_seconds"],
    }


def build_segmentation_result(
    transcript: dict[str, Any],
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    max_segment_duration_seconds: float = DEFAULT_MAX_SEGMENT_DURATION_SECONDS,
    max_segment_words: int = DEFAULT_MAX_SEGMENT_WORDS,
    omit_filler_micro_segments: bool = DEFAULT_OMIT_FILLER_MICRO_SEGMENTS,
    filler_micro_segment_max_seconds: float = DEFAULT_FILLER_MICRO_SEGMENT_MAX_SECONDS,
    pause_split_seconds: float = DEFAULT_PAUSE_SPLIT_SECONDS,
    min_dubbing_segment_duration_seconds: float = DEFAULT_MIN_DUBBING_SEGMENT_DURATION_SECONDS,
    min_dubbing_segment_words: int = DEFAULT_MIN_DUBBING_SEGMENT_WORDS,
    max_merge_gap_seconds: float = DEFAULT_MAX_MERGE_GAP_SECONDS,
    short_fragment_merge_gap_seconds: float = DEFAULT_SHORT_FRAGMENT_MERGE_GAP_SECONDS,
    degenerate_segment_max_seconds: float = DEFAULT_DEGENERATE_SEGMENT_MAX_SECONDS,
    hard_max_tolerance: float = DEFAULT_HARD_MAX_TOLERANCE,
) -> dict[str, Any]:
    options = _segmentation_options(
        max_gap_seconds,
        max_segment_duration_seconds,
        max_segment_words,
        omit_filler_micro_segments,
        filler_micro_segment_max_seconds,
        pause_split_seconds,
        min_dubbing_segment_duration_seconds,
        min_dubbing_segment_words,
        max_merge_gap_seconds,
        short_fragment_merge_gap_seconds,
        degenerate_segment_max_seconds,
        hard_max_tolerance,
    )
    words = _word_items(transcript)
    if not words:
        return {
            "segments": [],
            "omitted_segments": [],
            "segment_quality_summary": _summarise_segments([], [], 0, options),
            "segmentation_options": options,
        }

    raw_segments: list[dict[str, Any]] = []
    current_words: list[dict[str, Any]] = []

    def flush(reason: str, forced_split: bool = False) -> None:
        nonlocal current_words
        if not current_words:
            return
        flags = {"forced_split"} if forced_split else set()
        raw_segments.append(_segment_from_words(current_words, reason, options, flags))
        current_words = []

    def split_current_until_within_limits() -> None:
        nonlocal current_words
        while len(current_words) > 1 and _should_split_current_words(current_words, options):
            reason = _limit_reason(current_words, options)
            split_index, forced_split = _choose_split_index(current_words, options)
            segment_words = current_words[: split_index + 1]
            current_words = current_words[split_index + 1 :]
            flags = {"forced_split"} if forced_split else set()
            raw_segments.append(_segment_from_words(segment_words, reason, options, flags))

    previous = None
    for word in words:
        current_speaker = current_words[0]["speaker"] if current_words else word["speaker"]
        speaker_changed = previous is not None and word["speaker"] != current_speaker
        gap_too_large = previous is not None and word["start_time"] - previous["end_time"] > max_gap_seconds
        previous_ended_sentence = previous is not None and any(char in SENTENCE_END for char in previous["punctuation"])

        if current_words and (speaker_changed or gap_too_large or previous_ended_sentence):
            if speaker_changed:
                flush("speaker_change")
            elif gap_too_large:
                flush("long_gap")
            else:
                flush("sentence_boundary")

        current_words.append(word)
        split_current_until_within_limits()
        previous = word

    flush("end_of_transcript")

    candidate_segments: list[dict[str, Any]] = []
    omitted_segments: list[dict[str, Any]] = []
    for segment in raw_segments:
        if _should_omit_segment(segment, options):
            omitted = dict(segment)
            omitted["id"] = f"omitted_{len(omitted_segments):04d}"
            omitted["omitted_reason"] = "filler_only_micro_segment"
            omitted["quality_flags"] = sorted(set(omitted.get("quality_flags", [])) | {"filler_omitted"})
            omitted_segments.append(omitted)
            continue

        candidate_segments.append(dict(segment))

    coalesced_segments = coalesce_dubbing_segments(candidate_segments, options)
    kept_segments: list[dict[str, Any]] = []
    for segment in coalesced_segments:
        kept = dict(segment)
        kept["id"] = f"seg_{len(kept_segments):04d}"
        kept_segments.append(kept)

    return {
        "segments": kept_segments,
        "omitted_segments": omitted_segments,
        "segment_quality_summary": _summarise_segments(kept_segments, omitted_segments, len(raw_segments), options),
        "segmentation_options": options,
    }


def build_segments(
    transcript: dict[str, Any],
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    max_segment_duration_seconds: float = DEFAULT_MAX_SEGMENT_DURATION_SECONDS,
    max_segment_words: int = DEFAULT_MAX_SEGMENT_WORDS,
    omit_filler_micro_segments: bool = DEFAULT_OMIT_FILLER_MICRO_SEGMENTS,
    filler_micro_segment_max_seconds: float = DEFAULT_FILLER_MICRO_SEGMENT_MAX_SECONDS,
    pause_split_seconds: float = DEFAULT_PAUSE_SPLIT_SECONDS,
    min_dubbing_segment_duration_seconds: float = DEFAULT_MIN_DUBBING_SEGMENT_DURATION_SECONDS,
    min_dubbing_segment_words: int = DEFAULT_MIN_DUBBING_SEGMENT_WORDS,
    max_merge_gap_seconds: float = DEFAULT_MAX_MERGE_GAP_SECONDS,
    short_fragment_merge_gap_seconds: float = DEFAULT_SHORT_FRAGMENT_MERGE_GAP_SECONDS,
    degenerate_segment_max_seconds: float = DEFAULT_DEGENERATE_SEGMENT_MAX_SECONDS,
    hard_max_tolerance: float = DEFAULT_HARD_MAX_TOLERANCE,
) -> list[dict[str, Any]]:
    result = build_segmentation_result(
        transcript,
        max_gap_seconds,
        max_segment_duration_seconds,
        max_segment_words,
        omit_filler_micro_segments,
        filler_micro_segment_max_seconds,
        pause_split_seconds,
        min_dubbing_segment_duration_seconds,
        min_dubbing_segment_words,
        max_merge_gap_seconds,
        short_fragment_merge_gap_seconds,
        degenerate_segment_max_seconds,
        hard_max_tolerance,
    )
    return result["segments"]


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _read_json_from_uri(uri: str) -> dict[str, Any]:
    if uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(uri)
        import boto3

        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)

    with urlopen(uri, timeout=30) as response:
        return json.loads(response.read())


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_segments(bucket: str, segments_key: str, jsonl_key: str, payload: dict[str, Any]) -> None:
    import boto3

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=segments_key,
        Body=json.dumps(payload, ensure_ascii=False, default=_json_default, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    lines = "\n".join(json.dumps(segment, ensure_ascii=False, default=_json_default) for segment in payload["segments"])
    s3.put_object(
        Bucket=bucket,
        Key=jsonl_key,
        Body=(lines + "\n").encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _slim_inline_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": segment["id"], "text": segment["text"]} for segment in segments]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    job_name = event["job_name"]
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    raw_transcript_uri = event.get("raw_transcript_uri")
    if not raw_transcript_uri:
        raw_key = event.get("raw_transcript_key") or f"transcripts/raw/{job_name}/raw.json"
        raw_transcript_uri = f"s3://{bucket}/{raw_key}"

    segments_key = event.get("segments_key") or f"transcripts/segments/{job_name}/segments.json"
    segments_jsonl_key = event.get("segments_jsonl_key") or f"transcripts/segments/{job_name}/segments.jsonl"

    transcript = _read_json_from_uri(raw_transcript_uri)
    segmentation_options = _segmentation_options_from_event(event)
    segmentation_result = build_segmentation_result(transcript, **segmentation_options)
    segments = segmentation_result["segments"]
    payload = {
        "job_name": job_name,
        "original_video_uri": event["original_video_uri"],
        "source_language_code": event.get("source_language_code", "es-US"),
        "segments": segments,
        "segment_count": len(segments),
        "segment_quality_summary": segmentation_result["segment_quality_summary"],
        "omitted_segments": segmentation_result["omitted_segments"],
        "segmentation_options": segmentation_result["segmentation_options"],
        "segments_s3_uri": f"s3://{bucket}/{segments_key}",
        "segments_jsonl_s3_uri": f"s3://{bucket}/{segments_jsonl_key}",
        "segments_key": segments_key,
        "segments_jsonl_key": segments_jsonl_key,
    }
    write_segments(bucket, segments_key, segments_jsonl_key, payload)
    return {
        "job_name": job_name,
        "original_video_uri": event["original_video_uri"],
        "source_language_code": event.get("source_language_code", "es-US"),
        "segments": _slim_inline_segments(segments),
        "segments_inline_format": "id_text",
        "segment_count": len(segments),
        "segment_quality_summary": segmentation_result["segment_quality_summary"],
        "omitted_segment_count": len(segmentation_result["omitted_segments"]),
        "segments_s3_uri": f"s3://{bucket}/{segments_key}",
        "segments_jsonl_s3_uri": f"s3://{bucket}/{segments_jsonl_key}",
        "segments_key": segments_key,
        "segments_jsonl_key": segments_jsonl_key,
    }
