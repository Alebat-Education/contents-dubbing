import base64
import json
import os
import re
from typing import Any
from urllib.parse import urlparse


WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)
_S3_CLIENT = None


def _s3_client() -> Any:
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3

        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def _format_vtt_timestamp(seconds: float) -> str:
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
        start = _format_vtt_timestamp(float(segment["start_time"]))
        end = _format_vtt_timestamp(float(segment["end_time"]))
        speaker = segment.get("speaker", "spk_0")
        text = segment.get("translated_text") or segment.get("text") or ""
        lines.extend([str(index), f"{start} --> {end}", f"<v {speaker}>{text}", ""])
    return "\n".join(lines)


def _body_to_text(body: Any) -> str:
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = json.loads(base64.b64decode(body).decode("utf-8"))
    elif isinstance(body, bytes):
        parsed = json.loads(body.decode("utf-8"))
    elif isinstance(body, dict):
        parsed = body
    else:
        raise ValueError(f"Unsupported Bedrock body type: {type(body).__name__}")

    if "content" in parsed and parsed["content"]:
        first = parsed["content"][0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
        return str(first)
    return str(parsed.get("completion") or parsed.get("outputText") or parsed)


def _load_json_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", text):
        try:
            payload, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, (dict, list)):
            return payload
    return None


def _extract_ordered_translation_strings(text: str, expected_segment_ids: list[str]) -> dict[str, str] | None:
    matches = list(re.finditer(r'"translation"\s*:\s*("(?:(?:\\.)|[^"\\])*")', text))
    if len(matches) != len(expected_segment_ids):
        return None

    translations: list[str] = []
    for match in matches:
        value = json.loads(match.group(1))
        if not isinstance(value, str) or not value.strip():
            return None
        translations.append(value.strip())

    return dict(zip(expected_segment_ids, translations, strict=True))


def extract_translations(body: Any, expected_segment_ids: list[str]) -> dict[str, str]:
    text = _body_to_text(body).strip()
    payload = _load_json_payload(text)
    if payload is None:
        recovered = _extract_ordered_translation_strings(text, expected_segment_ids)
        if recovered is not None:
            return recovered
        raise ValueError("Bedrock response did not include JSON")

    translations = payload.get("translations") if isinstance(payload, dict) else payload
    if not isinstance(translations, list):
        recovered = _extract_ordered_translation_strings(text, expected_segment_ids)
        if recovered is not None:
            return recovered
        raise ValueError("Bedrock response JSON did not include a translations array")

    by_segment_id: dict[str, str] = {}
    for item in translations:
        if not isinstance(item, dict):
            raise ValueError("Each Bedrock translation item must be an object")
        segment_id = str(item.get("segment_id") or item.get("id") or "")
        translation = item.get("translation")
        if not segment_id:
            raise ValueError("Bedrock translation item is missing segment_id")
        if segment_id in by_segment_id:
            raise ValueError(f"Duplicate translation for segment_id {segment_id}")
        if not isinstance(translation, str) or not translation.strip():
            raise ValueError(f"Empty translation for segment_id {segment_id}")
        by_segment_id[segment_id] = translation.strip()

    expected_set = set(expected_segment_ids)
    actual_set = set(by_segment_id)
    missing = [segment_id for segment_id in expected_segment_ids if segment_id not in by_segment_id]
    extra = sorted(actual_set - expected_set)
    if missing or extra:
        raise ValueError(f"Bedrock response segment_id mismatch. Missing: {missing}. Extra: {extra}")

    return by_segment_id


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def _segment_id(segment: dict[str, Any]) -> str:
    segment_id = segment.get("id") or segment.get("segment_id")
    if not segment_id:
        raise ValueError("Segment is missing id")
    return str(segment_id)


def _source_duration_ms(segment: dict[str, Any]) -> int:
    start_time = float(segment.get("start_time", 0) or 0)
    end_time = float(segment.get("end_time", start_time) or start_time)
    return int(round(max(0.0, end_time - start_time) * 1000))


def _add_translation_diagnostics(segment: dict[str, Any], batch_id: str | None) -> None:
    spanish_word_count = int(segment.get("word_count") or _word_count(segment.get("text", "")))
    english_word_count = _word_count(segment.get("translated_text", ""))
    segment["bedrock_batch_id"] = batch_id
    segment["source_duration_ms"] = _source_duration_ms(segment)
    segment["spanish_word_count"] = spanish_word_count
    segment["english_word_count"] = english_word_count
    segment["english_to_spanish_word_ratio"] = (
        round(english_word_count / spanish_word_count, 4) if spanish_word_count > 0 else None
    )


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _loads_json_object(text: str) -> Any:
    payload: Any = json.loads(text)
    for _ in range(2):
        if not isinstance(payload, str):
            return payload
        payload = json.loads(payload)
    return payload


def _get_json(bucket: str, key: str) -> Any:
    response = _s3_client().get_object(Bucket=bucket, Key=key)
    return _loads_json_object(response["Body"].read().decode("utf-8"))


def _put_object(bucket: str, key: str, body: bytes, content_type: str) -> None:
    _s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def _source_segments_by_id(segments_s3_uri: str) -> dict[str, dict[str, Any]]:
    bucket, key = _parse_s3_uri(segments_s3_uri)
    payload = _get_json(bucket, key)
    source_segments = payload["segments"] if isinstance(payload, dict) else payload
    return {_segment_id(segment): segment for segment in source_segments}


def _translated_segments_from_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    source_segments = _source_segments_by_id(event["segments_s3_uri"])
    translated_segments: list[dict[str, Any]] = []

    for ref in event["model_result_refs"]:
        result_payload = _get_json(ref["result_s3_bucket"], ref["result_s3_key"])
        response = result_payload["response"]
        expected_segment_ids = [str(segment_id) for segment_id in result_payload["segment_ids"]]
        try:
            translations = extract_translations(response["Body"], expected_segment_ids)
        except ValueError as exc:
            batch_id = result_payload.get("batch_id") or ref.get("batch_id", "unknown")
            result_key = ref.get("result_s3_key", "unknown")
            raise ValueError(f"Failed to parse Bedrock result {batch_id} at {result_key}: {exc}") from exc
        for segment_id in expected_segment_ids:
            if segment_id not in source_segments:
                raise ValueError(f"Bedrock result references unknown segment_id {segment_id}")
            segment = dict(source_segments[segment_id])
            segment["translated_text"] = translations[segment_id]
            segment["translation_engine"] = "bedrock"
            _add_translation_diagnostics(segment, result_payload.get("batch_id"))
            translated_segments.append(segment)

    return translated_segments


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    job_name = event["job_name"]
    output_prefix = event.get("output_prefix") or f"translations/bedrock/{job_name}"
    translated_segments = _translated_segments_from_refs(event)

    json_key = f"{output_prefix}/segments.json"
    jsonl_key = f"{output_prefix}/segments.jsonl"
    vtt_key = f"{output_prefix}/subtitles.vtt"
    artifact_payload = {
        "job_name": job_name,
        "translation_engine": "bedrock",
        "original_video_uri": event["original_video_uri"],
        "translated_segments": translated_segments,
        "bedrock_batch_count": int(event.get("bedrock_batch_count") or len(event["model_result_refs"])),
        "bedrock_batch_size": int(event.get("bedrock_batch_size") or 1),
        "translated_segment_count": len(translated_segments),
        "translated_segments_key": json_key,
        "translated_segments_s3_uri": f"s3://{bucket}/{json_key}",
        "vtt_key": vtt_key,
        "vtt_s3_uri": f"s3://{bucket}/{vtt_key}",
    }
    result = {
        "job_name": job_name,
        "translation_engine": "bedrock",
        "original_video_uri": event["original_video_uri"],
        "bedrock_batch_count": int(event.get("bedrock_batch_count") or len(event["model_result_refs"])),
        "bedrock_batch_size": int(event.get("bedrock_batch_size") or 1),
        "translated_segment_count": len(translated_segments),
        "translated_segments_key": json_key,
        "translated_segments_s3_uri": f"s3://{bucket}/{json_key}",
        "vtt_key": vtt_key,
        "vtt_s3_uri": f"s3://{bucket}/{vtt_key}",
    }

    _put_object(bucket, json_key, json.dumps(artifact_payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
    _put_object(
        bucket,
        jsonl_key,
        ("\n".join(json.dumps(segment, ensure_ascii=False) for segment in translated_segments) + "\n").encode("utf-8"),
        "application/x-ndjson",
    )
    _put_object(bucket, vtt_key, build_vtt(translated_segments).encode("utf-8"), "text/vtt")
    return result
