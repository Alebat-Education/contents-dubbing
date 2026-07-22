import json
import os
from typing import Any
from urllib.parse import urlparse


def format_vtt_timestamp(seconds: float) -> str:
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    whole_seconds = int(seconds)
    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def build_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for index, segment in enumerate(segments, start=1):
        start = format_vtt_timestamp(float(segment["start_time"]))
        end = format_vtt_timestamp(float(segment["end_time"]))
        speaker = segment.get("speaker", "spk_0")
        text = segment.get("translated_text") or segment.get("text") or ""
        lines.extend([str(index), f"{start} --> {end}", f"<v {speaker}>{text}", ""])
    return "\n".join(lines)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _get_json(bucket: str, key: str) -> Any:
    import boto3

    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def _put_json(bucket: str, key: str, payload: dict[str, Any]) -> None:
    import boto3

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _put_text(bucket: str, key: str, text: str, content_type: str) -> None:
    import boto3

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType=content_type,
    )


def _translated_segments_from_event(event: dict[str, Any], engine: str) -> list[dict[str, Any]]:
    if "translated_refs" not in event:
        return event["translated_segments"]

    if "segments_s3_uri" not in event:
        raise ValueError("segments_s3_uri is required when translated_refs is provided")

    bucket, key = _parse_s3_uri(event["segments_s3_uri"])
    source_payload = _get_json(bucket, key)
    source_segments = source_payload["segments"] if isinstance(source_payload, dict) else source_payload
    segments_by_id = {str(segment["id"]): segment for segment in source_segments}
    translated_segments = []
    seen_ids: set[str] = set()

    for ref in event["translated_refs"]:
        segment_id = str(ref["id"])
        if segment_id in seen_ids:
            raise ValueError(f"Duplicate translated ref for segment_id {segment_id}")
        if segment_id not in segments_by_id:
            raise ValueError(f"Translated ref does not match a source segment: {segment_id}")
        seen_ids.add(segment_id)
        segment = dict(segments_by_id[segment_id])
        segment["translated_text"] = ref["translated_text"]
        segment["translation_engine"] = engine
        translated_segments.append(segment)

    return translated_segments


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    job_name = event["job_name"]
    engine = event.get("translation_engine", "amazon_translate")
    segments = _translated_segments_from_event(event, engine)
    output_prefix = event.get("output_prefix") or f"translations/{engine}/{job_name}"

    json_key = f"{output_prefix}/segments.json"
    jsonl_key = f"{output_prefix}/segments.jsonl"
    vtt_key = f"{output_prefix}/subtitles.vtt"
    artifact_payload = {
        "job_name": job_name,
        "translation_engine": engine,
        "original_video_uri": event["original_video_uri"],
        "translated_segments": segments,
        "translated_segments_key": json_key,
        "translated_segments_s3_uri": f"s3://{bucket}/{json_key}",
        "vtt_key": vtt_key,
        "vtt_s3_uri": f"s3://{bucket}/{vtt_key}",
    }
    result = {
        "job_name": job_name,
        "translation_engine": engine,
        "original_video_uri": event["original_video_uri"],
        "translated_segment_count": len(segments),
        "translated_segments_key": json_key,
        "translated_segments_s3_uri": f"s3://{bucket}/{json_key}",
        "vtt_key": vtt_key,
        "vtt_s3_uri": f"s3://{bucket}/{vtt_key}",
    }

    _put_json(bucket, json_key, artifact_payload)
    _put_text(
        bucket,
        jsonl_key,
        "\n".join(json.dumps(segment, ensure_ascii=False) for segment in segments) + "\n",
        "application/x-ndjson",
    )
    _put_text(bucket, vtt_key, build_vtt(segments), "text/vtt")
    return result
