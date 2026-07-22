import json
import os
from typing import Any
from urllib.parse import urlparse


def _output_uri(result: dict[str, Any]) -> str | None:
    if result.get("audio_s3_uri"):
        return str(result["audio_s3_uri"])
    task = result.get("synthesis_task") or result.get("SynthesisTask") or {}
    return task.get("OutputUri")


def normalize_s3_audio_uri(uri: str) -> str:
    if uri.startswith("s3://"):
        return uri

    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"}:
        return uri

    host_parts = parsed.netloc.split(".")
    path_parts = parsed.path.lstrip("/").split("/", 1)

    if len(host_parts) >= 3 and host_parts[1] == "s3":
        bucket = host_parts[0]
        key = parsed.path.lstrip("/")
        return f"s3://{bucket}/{key}"

    if host_parts[0].startswith("s3") and len(path_parts) == 2:
        bucket, key = path_parts
        return f"s3://{bucket}/{key}"

    return uri


def _loads_json_object(text: str) -> Any:
    payload: Any = json.loads(text)
    for _ in range(2):
        if not isinstance(payload, str):
            return payload
        payload = json.loads(payload)
    return payload


def _get_json(bucket: str, key: str) -> Any:
    import boto3

    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return _loads_json_object(response["Body"].read().decode("utf-8"))


def _synthesis_result_refs_from_task_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    job_name = event["job_name"]
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    refs = []
    for task_ref in event["task_refs"]:
        segment_id = task_ref["segment_id"]
        refs.append(
            {
                "segment_id": segment_id,
                "result_s3_bucket": bucket,
                "result_s3_key": f"manifests/polly/{job_name}/synthesis_results/{segment_id}.json",
            }
        )
    return refs


def _synthesis_results_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    if "synthesis_result_refs" in event:
        refs = event["synthesis_result_refs"]
    elif "task_refs" in event:
        refs = _synthesis_result_refs_from_task_refs(event)
    else:
        return event["synthesis_results"]
    return [_get_json(ref["result_s3_bucket"], ref["result_s3_key"]) for ref in refs]


def build_manifest(polly_plan: dict[str, Any], synthesis_results: list[dict[str, Any]]) -> dict[str, Any]:
    audio_segments = []
    for item in synthesis_results:
        task = item.get("task") or item
        output_uri = _output_uri(item)
        if not output_uri:
            raise ValueError(f"Missing Polly OutputUri for segment {task['segment_id']}")
        audio_segments.append(
            {
                "segment_id": task["segment_id"],
                "speaker": task["speaker"],
                "voice_id": task["voice_id"],
                "engine": task["engine"],
                "start_time": task["start_time"],
                "end_time": task["end_time"],
                "offset_ms": task["offset_ms"],
                "audio_s3_uri": normalize_s3_audio_uri(output_uri),
            }
        )

    offsets = [int(segment["offset_ms"]) for segment in audio_segments]
    end_times = [float(segment["end_time"]) for segment in audio_segments]
    first_result = synthesis_results[0] if synthesis_results else {}

    return {
        "job_name": polly_plan.get("job_name") or first_result.get("job_name"),
        "original_video_uri": polly_plan.get("original_video_uri") or first_result.get("original_video_uri"),
        "translation_engine": polly_plan.get("translation_engine") or first_result.get("translation_engine", "unknown"),
        "segment_count": len(audio_segments),
        "first_offset_ms": min(offsets) if offsets else None,
        "last_offset_ms": max(offsets) if offsets else None,
        "timeline_duration_seconds": max(end_times) if end_times else 0.0,
        "audio_segments": audio_segments,
    }


def slim_manifest_response(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_name": manifest["job_name"],
        "original_video_uri": manifest["original_video_uri"],
        "translation_engine": manifest["translation_engine"],
        "segment_count": manifest["segment_count"],
        "first_offset_ms": manifest["first_offset_ms"],
        "last_offset_ms": manifest["last_offset_ms"],
        "timeline_duration_seconds": manifest["timeline_duration_seconds"],
        "manifest_key": manifest["manifest_key"],
        "manifest_s3_uri": manifest["manifest_s3_uri"],
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    polly_plan = event.get("polly_plan") or {
        "job_name": event.get("job_name"),
        "original_video_uri": event.get("original_video_uri"),
        "translation_engine": event.get("translation_engine"),
    }
    manifest = build_manifest(polly_plan, _synthesis_results_from_event(event))
    key = event.get("manifest_key") or f"manifests/polly/{manifest['job_name']}/manifest.json"
    manifest["manifest_key"] = key
    manifest["manifest_s3_uri"] = f"s3://{bucket}/{key}"

    import boto3

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return slim_manifest_response(manifest)
