import json
import os
from typing import Any
from urllib.parse import urlparse


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _read_manifest(uri: str) -> dict[str, Any]:
    bucket, key = _parse_s3_uri(uri)
    import boto3

    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def build_job_payload(
    manifest: dict[str, Any],
    role_arn: str,
    queue_arn: str,
    output_prefix_uri: str,
) -> dict[str, Any]:
    dubbed_audio_s3_uri = manifest.get("dubbed_audio_s3_uri")
    if not dubbed_audio_s3_uri:
        raise ValueError("Manifest must include dubbed_audio_s3_uri from assemble_dubbed_audio before MediaConvert packaging")

    destination = output_prefix_uri.rstrip("/") + f"/{manifest['job_name']}/"

    return {
        "Role": role_arn,
        "Queue": queue_arn,
        "UserMetadata": {
            "job_name": manifest["job_name"],
            "translation_engine": manifest.get("translation_engine", "unknown"),
        },
        "Settings": {
            "TimecodeConfig": {"Source": "ZEROBASED"},
            "Inputs": [
                {
                    "FileInput": manifest["original_video_uri"],
                    "TimecodeSource": "ZEROBASED",
                    "VideoSelector": {},
                    "AudioSelectors": {
                        "EnglishDub": {
                            "DefaultSelection": "DEFAULT",
                            "ExternalAudioFileInput": dubbed_audio_s3_uri,
                        }
                    },
                }
            ],
            "OutputGroups": [
                {
                    "Name": "File Group",
                    "OutputGroupSettings": {
                        "Type": "FILE_GROUP_SETTINGS",
                        "FileGroupSettings": {
                            "Destination": destination,
                        },
                    },
                    "Outputs": [
                        {
                            "NameModifier": "_dubbed_en",
                            "ContainerSettings": {
                                "Container": "MP4",
                                "Mp4Settings": {
                                    "CslgAtom": "INCLUDE",
                                    "FreeSpaceBox": "EXCLUDE",
                                    "MoovPlacement": "PROGRESSIVE_DOWNLOAD",
                                },
                            },
                            "VideoDescription": {
                                "CodecSettings": {
                                    "Codec": "H_264",
                                    "H264Settings": {
                                        "RateControlMode": "QVBR",
                                        "QvbrSettings": {"QvbrQualityLevel": 8},
                                        "MaxBitrate": 5000000,
                                    },
                                }
                            },
                            "AudioDescriptions": [
                                {
                                    "AudioSourceName": "EnglishDub",
                                    "CodecSettings": {
                                        "Codec": "AAC",
                                        "AacSettings": {
                                            "Bitrate": 128000,
                                            "CodingMode": "CODING_MODE_2_0",
                                            "SampleRate": 48000,
                                        },
                                    },
                                    "LanguageCode": "ENG",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "StatusUpdateInterval": "SECONDS_60",
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    manifest = event.get("manifest")
    if not manifest:
        manifest = _read_manifest(event["manifest_s3_uri"])

    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    output_prefix_uri = event.get("output_prefix_uri") or f"s3://{bucket}/video_output/mediaconvert"
    job_payload = build_job_payload(
        manifest=manifest,
        role_arn=event.get("mediaconvert_role_arn") or os.environ["MEDIACONVERT_ROLE_ARN"],
        queue_arn=event.get("mediaconvert_queue_arn") or os.environ["MEDIACONVERT_QUEUE_ARN"],
        output_prefix_uri=output_prefix_uri,
    )
    return {
        "job_name": manifest["job_name"],
        "original_video_uri": manifest["original_video_uri"],
        "job_payload": job_payload,
        "expected_output_prefix_uri": output_prefix_uri.rstrip("/") + f"/{manifest['job_name']}/",
    }
