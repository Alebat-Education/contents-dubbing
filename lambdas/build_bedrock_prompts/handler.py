import json
import os
from typing import Any
from urllib.parse import urlparse


DEFAULT_BATCH_SIZE = 12
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 20
MIN_TEMPO_FACTOR = 0.80
MAX_TEMPO_FACTOR = 1.20

SYSTEM_CONTEXT = (
    "You are a professional medical translator. Translate Spanish clinical dialogue "
    "to natural English for dubbing. Preserve medical meaning, drug names, units, "
    "test names, and speaker intent. Return only compact JSON."
)

_S3_CLIENT = None


def _s3_client() -> Any:
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3

        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _segment_id(segment: dict[str, Any]) -> str:
    segment_id = segment.get("id") or segment.get("segment_id")
    if not segment_id:
        raise ValueError("Segment is missing id")
    return str(segment_id)


def _batch_size(options: dict[str, Any] | None) -> int:
    if not options or "batch_size" not in options:
        return DEFAULT_BATCH_SIZE
    try:
        requested = int(options["batch_size"])
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, requested))


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _get_json(bucket: str, key: str) -> Any:
    response = _s3_client().get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def _put_json(bucket: str, key: str, payload: dict[str, Any]) -> None:
    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )


def _segments_from_event(event: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    if "segments_s3_uri" in event:
        bucket, key = _parse_s3_uri(event["segments_s3_uri"])
        payload = _get_json(bucket, key)
        segments = payload["segments"] if isinstance(payload, dict) else payload
        return segments, event["segments_s3_uri"], bucket

    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    key = f"translations/bedrock/{event['job_name']}/source_segments.json"
    segments = event["segments"]
    _put_json(bucket, key, {"segments": segments})
    return segments, f"s3://{bucket}/{key}", bucket


def _segment_prompt_payload(segment: dict[str, Any]) -> dict[str, Any]:
    start_time = _float_value(segment.get("start_time"), 0.0)
    end_time = _float_value(segment.get("end_time"), start_time)
    target_duration_seconds = max(0.25, end_time - start_time)
    return {
        "segment_id": _segment_id(segment),
        "speaker": segment.get("speaker", "spk_0"),
        "start_time": start_time,
        "end_time": end_time,
        "target_duration_seconds": round(target_duration_seconds, 2),
        "acceptable_raw_speech_duration_seconds": {
            "min": round(target_duration_seconds * MIN_TEMPO_FACTOR, 2),
            "max": round(target_duration_seconds * MAX_TEMPO_FACTOR, 2),
        },
        "spanish_text": segment.get("text", ""),
    }


def build_prompt(segments: list[dict[str, Any]]) -> dict[str, Any]:
    segment_payloads = [_segment_prompt_payload(segment) for segment in segments]
    prompt = (
        f"{SYSTEM_CONTEXT}\n\n"
        "Return one top-level JSON object, not a bare array. "
        "Return this JSON shape exactly: "
        "{\"translations\":[{\"segment_id\":\"seg_0000\",\"translation\":\"...\"}]}.\n"
        "Rules:\n"
        "- Return one translation for every input segment_id, in the same order.\n"
        "- Do not merge, split, reorder, summarize, compress, or omit meaningful content.\n"
        "- Preserve medical meaning exactly. Do not add new clinical facts or unsupported details.\n"
        "- Prefer complete spoken phrasing over terse subtitle-style translation.\n"
        "- Prefer natural spoken English suitable for voice dubbing. Preserve conversational "
        "pacing and emphasis. Do not unnecessarily compress explanations or omit discourse "
        "markers when they contribute to the natural spoken rhythm.\n"
        "- If a direct English translation is too short for the target duration, use natural "
        "spoken phrasing, explicit transitions, and contextually faithful wording to make it "
        "more speakable without adding facts.\n"
        "- Use each target duration and acceptable raw speech duration range as guidance for "
        "natural spoken length, but do not include duration estimates in the response.\n\n"
        "Input segments JSON:\n"
        f"{json.dumps(segment_payloads, ensure_ascii=False, separators=(',', ':'))}"
    )
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": min(4000, 500 + len(segment_payloads) * 220),
        "temperature": 0.1,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
    }


def build_prompt_batches(segments: list[dict[str, Any]], batch_size: int) -> list[dict[str, Any]]:
    batches = []
    for batch_index, start in enumerate(range(0, len(segments), batch_size)):
        batch_segments = segments[start : start + batch_size]
        batch_id = f"batch_{batch_index:04d}"
        batches.append(
            {
                "batch_id": batch_id,
                "segment_ids": [_segment_id(segment) for segment in batch_segments],
                "body": build_prompt(batch_segments),
            }
        )
    return batches


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    batch_size = _batch_size(event.get("bedrock_options"))
    segments, segments_s3_uri, bucket = _segments_from_event(event)
    prompt_batches = build_prompt_batches(segments, batch_size)
    job_name = event["job_name"]
    batch_refs = []

    for batch in prompt_batches:
        batch_id = batch["batch_id"]
        batch_key = f"translations/bedrock/{job_name}/batches/{batch_id}.json"
        result_key = f"translations/bedrock/{job_name}/results/{batch_id}.json"
        _put_json(bucket, batch_key, batch)
        batch_refs.append(
            {
                "batch_id": batch_id,
                "batch_s3_bucket": bucket,
                "batch_s3_key": batch_key,
                "result_s3_bucket": bucket,
                "result_s3_key": result_key,
                "segment_ids": batch["segment_ids"],
            }
        )

    return {
        "job_name": job_name,
        "original_video_uri": event["original_video_uri"],
        "segments_s3_uri": segments_s3_uri,
        "batch_refs": batch_refs,
        "bedrock_batch_size": batch_size,
        "bedrock_batch_count": len(batch_refs),
    }
