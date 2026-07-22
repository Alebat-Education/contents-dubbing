import html
import json
import os
from typing import Any
from urllib.parse import urlparse


DEFAULT_VOICE_SEQUENCE = ["Joanna", "Matthew", "Ruth", "Stephen", "Danielle", "Gregory"]
_S3_CLIENT = None


def _s3_client() -> Any:
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3

        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def _voice_for_speaker(speaker: str, voice_map: dict[str, str], index_by_speaker: dict[str, int]) -> str:
    if speaker in voice_map:
        return voice_map[speaker]
    if speaker not in index_by_speaker:
        index_by_speaker[speaker] = len(index_by_speaker)
    return DEFAULT_VOICE_SEQUENCE[index_by_speaker[speaker] % len(DEFAULT_VOICE_SEQUENCE)]


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


def _translation_result(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("translation_result") or event


def _translated_segments_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    translation_result = _translation_result(event)
    if "translated_segments_s3_uri" in translation_result:
        bucket, key = _parse_s3_uri(translation_result["translated_segments_s3_uri"])
        payload = _get_json(bucket, key)
        return payload["translated_segments"]
    return translation_result["translated_segments"]


def build_ssml(
    text: str,
    start_time: float,
    end_time: float,
    prosody_rate: str | None = None,
    trailing_break_ms: int = 0,
) -> str:
    escaped = html.escape(text, quote=False)
    speech = escaped
    if prosody_rate:
        speech = f"<prosody rate=\"{html.escape(prosody_rate, quote=True)}\">{escaped}</prosody>"
    if trailing_break_ms > 0:
        speech = f"{speech}<break time=\"{trailing_break_ms}ms\"/>"
    return f"<speak>{speech}</speak>"


def build_polly_tasks(
    job_name: str,
    translated_segments: list[dict[str, Any]],
    voice_map: dict[str, str],
    output_prefix: str,
    engine: str = "neural",
    prosody_rate: str | None = None,
    trailing_break_ms: int = 0,
) -> list[dict[str, Any]]:
    tasks = []
    index_by_speaker: dict[str, int] = {}
    for segment in translated_segments:
        speaker = segment.get("speaker", "spk_0")
        voice_id = _voice_for_speaker(speaker, voice_map, index_by_speaker)
        output_key_prefix = f"{output_prefix}/{segment['id']}/"
        tasks.append(
            {
                "job_name": job_name,
                "segment_id": segment["id"],
                "speaker": speaker,
                "voice_id": voice_id,
                "engine": engine,
                "start_time": float(segment["start_time"]),
                "end_time": float(segment["end_time"]),
                "offset_ms": int(round(float(segment["start_time"]) * 1000)),
                "text": segment.get("translated_text") or segment.get("text", ""),
                "ssml": build_ssml(
                    segment.get("translated_text") or segment.get("text", ""),
                    float(segment["start_time"]),
                    float(segment["end_time"]),
                    prosody_rate,
                    trailing_break_ms,
                ),
                "output_key_prefix": output_key_prefix,
            }
        )
    return tasks


def write_task_refs(bucket: str, job_name: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = []
    for task in tasks:
        key = f"manifests/polly/{job_name}/tasks/{task['segment_id']}.json"
        _put_json(bucket, key, task)
        refs.append(
            {
                "segment_id": task["segment_id"],
                "task_s3_bucket": bucket,
                "task_s3_key": key,
            }
        )
    return refs


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    translation_result = _translation_result(event)
    job_name = translation_result["job_name"]
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    output_prefix = event.get("audio_output_prefix") or f"audio_output/polly/{job_name}"
    voice_map = event.get("voice_map") or {}
    tasks = build_polly_tasks(
        job_name,
        _translated_segments_from_event(event),
        voice_map,
        output_prefix,
        event.get("engine", "neural"),
        event.get("ssml_prosody_rate"),
        int(event.get("ssml_trailing_break_ms", 0)),
    )
    task_refs = write_task_refs(bucket, job_name, tasks)
    return {
        "job_name": job_name,
        "original_video_uri": translation_result["original_video_uri"],
        "translation_engine": translation_result.get("translation_engine", "unknown"),
        "task_refs": task_refs,
        "task_count": len(task_refs),
        "audio_output_prefix": output_prefix,
    }
