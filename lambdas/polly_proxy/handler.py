import os
from datetime import date, datetime
from typing import Any


DEFAULT_POLLY_REGION = "eu-central-1"
DEFAULT_POLLY_ENGINE = "generative"
DEFAULT_LANGUAGE_CODE = "en-US"
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_TEXT_TYPE = "ssml"


def polly_client(region: str):
    import boto3

    return boto3.client("polly", region_name=region)


def build_start_request(event: dict[str, Any]) -> dict[str, Any]:
    task = event["task"]
    bucket = event.get("bucket") or os.environ.get("POLLY_OUTPUT_BUCKET") or os.environ["DATA_BUCKET"]

    return {
        "Engine": event.get("engine") or os.environ.get("POLLY_ENGINE", DEFAULT_POLLY_ENGINE),
        "LanguageCode": event.get("language_code", DEFAULT_LANGUAGE_CODE),
        "OutputFormat": event.get("output_format", DEFAULT_OUTPUT_FORMAT),
        "OutputS3BucketName": bucket,
        "OutputS3KeyPrefix": task["output_key_prefix"],
        "Text": task["ssml"],
        "TextType": event.get("text_type", DEFAULT_TEXT_TYPE),
        "VoiceId": task["voice_id"],
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    operation = event.get("operation")
    region = event.get("polly_region") or os.environ.get("POLLY_REGION", DEFAULT_POLLY_REGION)
    client = polly_client(region)

    if operation == "start":
        return json_safe(client.start_speech_synthesis_task(**build_start_request(event)))

    if operation == "get":
        return json_safe(client.get_speech_synthesis_task(TaskId=event["task_id"]))

    raise ValueError("operation must be 'start' or 'get'")
