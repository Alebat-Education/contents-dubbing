# Alebat - AI-Powered Video Translation on AWS

## Executive Summary

This repository provisions a serverless AWS proof of concept for Spanish-to-English medical video dubbing. The design uses Step Functions direct AWS SDK integrations for Transcribe, Translate, Bedrock, S3, and MediaConvert, with a thin Polly proxy Lambda for cross-Region generative voices and small Python Lambdas where payloads need to be parsed, reshaped, or rendered.

The final artifact is a playable English-dubbed MP4 in S3. The original video stream is preserved, the original Spanish audio is replaced, and the English audio is synthesized with Amazon Polly generative voices in `eu-central-1`.

## Architectural Overview

The deployable Terraform root is `terraform/`. It creates one encrypted primary S3 data bucket, one encrypted Polly output bucket in `polly_region`, IAM roles, transformer Lambdas, an FFmpeg Lambda layer, custom Transcribe and Translate assets, a MediaConvert queue, and five independent Standard Step Functions state machines.

Workflow order:

1. Upload a Spanish medical sample video to `s3://<bucket>/input/`.
2. Run `transcription` to generate raw Transcribe output and normalized speaker segments.
3. Run either `translate_amazon` or `translate_bedrock`.
4. Run `dubbing_polly` with translated segments to create timed MP3 segment audio and a manifest.
5. Run `packaging_mediaconvert` with the Polly manifest. This first assembles one continuous dubbed audio track, then MediaConvert muxes it with the original video.

S3 layout:

```text
input/
transcripts/raw/
transcripts/segments/
translations/amazon_translate/
translations/bedrock/
audio_output/polly/
audio_output/assembled/
video_output/mediaconvert/
manifests/
config/
```

Polly MP3 segment objects are stored in the regional Polly output bucket because Amazon Polly synthesis tasks must write to a bucket in the Polly service Region. The manifest stores those returned S3 URIs, and the assembly workflow reads them directly.

Large intermediate objects are kept in S3 rather than being carried through Step Functions state. The transcription workflow returns only slim inline segment records for direct Amazon Translate mapping, while full segment metadata is stored at `segments_s3_uri`. Later handoffs use S3 pointers such as `translated_segments_s3_uri`, Bedrock batch/result references, Polly task references, Polly synthesis result references, and `manifest_s3_uri`. This avoids `States.DataLimitExceeded` failures on longer transcripts while keeping the external workflow inputs mostly unchanged.

## Key Integrations

- Amazon Transcribe uses a Spanish medical custom vocabulary from `assets/transcribe/medical_vocabulary_es.tsv` and built-in speaker diarization.
- Amazon Translate uses custom terminology from `assets/translate/medical_terminology.csv`.
- Bedrock translates contiguous segment batches with a Claude Messages API prompt tailored for medical dubbing context and spoken-duration guidance. Batch request and response bodies are stored under `translations/bedrock/<job_name>/`.
- Polly uses a lightweight proxy Lambda so the `eu-west-1` workflow can call generative voices in `eu-central-1`, and maps `spk_0`, `spk_1`, and additional speakers to distinct configured voices. Full Polly synthesis task payloads and manifest audio segments are stored under `manifests/polly/<job_name>/`, and the synthesis loop uses a Distributed Map with S3 result export so per-segment polling does not exhaust Step Functions history or payload limits.
- FFmpeg and FFprobe run from a Lambda layer to measure Polly segment durations, apply bounded per-segment tempo correction, place audio at transcript offsets, and create one continuous dubbed audio file. Large jobs are mixed in bounded FFmpeg chunks before the final mix to avoid oversized FFmpeg input graphs.
- MediaConvert uses the original video plus the assembled dubbed audio file to produce an English-only MP4.

## Deployment

Prerequisites:

- Terraform `>= 1.6`
- AWS CLI configured for the target account
- AWS CLI available on the machine running `terraform apply` so Terraform can import/delete Amazon Translate terminology
- `curl`, `tar`, `zip`, and `unzip` available locally to build the FFmpeg Lambda layer
- Bedrock model access enabled for the configured model
- MediaConvert available in `eu-west-1`

Build the FFmpeg layer, then deploy:

```bash
./scripts/build_ffmpeg_layer.sh
cd terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
# Edit backend.hcl and set the existing S3 state bucket name.
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

Terraform uploads `layers/ffmpeg/ffmpeg-layer.zip` to the project S3 bucket and publishes the Lambda layer from S3. This avoids the Lambda direct upload request-size limit for the static FFmpeg binary package.

Useful outputs:

```bash
terraform output data_bucket_name
terraform output polly_output_bucket_name
terraform output state_machine_arns
```

### Terraform state

Terraform stores this environment's state in the primary S3 bucket at
`terraform/terraform.tfstate`. The backend uses S3-native lockfiles
(`terraform/terraform.tfstate.tflock`) with `use_lockfile = true`, it does not
use DynamoDB. The bucket is versioned and the backend encrypts both the state
and lockfile objects.


The state bucket must already exist before Terraform can initialize this root
module. Backend configuration is evaluated before Terraform can create the
resources in `main.tf`.

For an existing environment, create the ignored `terraform/backend.hcl` from
`backend.hcl.example`, replace the placeholder bucket name, and initialize:

```bash
cd terraform
terraform init -backend-config=backend.hcl
terraform plan
```

For a normal cleanup, leave `force_destroy_bucket = false` and run
`terraform destroy`. The state object intentionally keeps the primary backend
bucket non-empty, so Terraform removes the other resources it can destroy and
then reports an expected `BucketNotEmpty` error when it attempts that bucket.
The bucket and remote state remain available; no state migration is required.

For the final project teardown, when the backend bucket must also be deleted:

1. Run `terraform init -migrate-state` and confirm migration from S3 to the
   local backend.
2. Set `force_destroy_bucket = true` in `terraform.tfvars`, then run
   `terraform destroy`.
3. Restore `terraform/versions.tf` from Git if the repository will continue to
   be used, and securely retain or dispose of the local state backup as needed.


## Running The PoC

Use 2-3 short, non-PHI Spanish medical sample videos. Store only synthetic or consented content in this PoC. Upload them yourself rather than committing media files to Git.

The easiest path is the runbook script. It prompts for a job name, an optional local video path, and either the Amazon Translate or Bedrock translation path. If a local video path is provided, the script uploads it to `s3://<data-bucket>/input/<job_name>.mp4`, runs all five workflows in order, and writes each Step Functions output to local JSON files.

```bash
./scripts/run_dubbing_runbook.sh
```

Manual execution is useful when rerunning only one stage or inspecting intermediate output.

Example upload:

```bash
BUCKET=$(terraform -chdir=terraform output -raw data_bucket_name)
aws s3 cp ./sample-consulta-01.mp4 "s3://${BUCKET}/input/sample-consulta-01.mp4"
```

Start transcription:

```bash
TRANSCRIBE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.transcription')
aws stepfunctions start-execution \
  --state-machine-arn "${TRANSCRIBE_ARN}" \
  --name sample-consulta-01-transcription \
  --input "{
    \"job_name\": \"sample-consulta-01\",
    \"media_uri\": \"s3://${BUCKET}/input/sample-consulta-01.mp4\"
  }"
```

The transcription output includes both a slim `segments` array and `segments_s3_uri`. Use the full transcription output as input to one of the translation workflows. The inline `segments` records contain only `id` and `text` for Amazon Translate mapping; `segments_s3_uri` is the durable source for full timestamps, speaker metadata, diagnostics, and larger downstream processing.

Amazon Translate workflow:

```bash
TRANSLATE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.translate_amazon')
aws stepfunctions start-execution \
  --state-machine-arn "${TRANSLATE_ARN}" \
  --name sample-consulta-01-translate \
  --input file://transcription-output.json
```

Bedrock evaluation workflow:

```bash
BEDROCK_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.translate_bedrock')
aws stepfunctions start-execution \
  --state-machine-arn "${BEDROCK_ARN}" \
  --name sample-consulta-01-bedrock \
  --input file://transcription-output.json
```

The Bedrock workflow defaults to 12 transcript segments per model call. To experiment without changing Terraform, include `bedrock_options.batch_size` in the workflow input, accepted values are clamped to 1-20.

Translation workflow outputs are intentionally slim. They include S3 pointers such as `translated_segments_s3_uri` and subtitle paths rather than returning the full translated segment array through Step Functions. The full translated JSON, JSONL, and VTT artifacts remain in S3.

Run Polly dubbing with the translation workflow output:

```bash
POLLY_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.dubbing_polly')
aws stepfunctions start-execution \
  --state-machine-arn "${POLLY_ARN}" \
  --name sample-consulta-01-polly \
  --input file://translation-output.json
```

Run packaging with the Polly output JSON. The full manifest remains in S3; the workflow output contains `manifest_s3_uri` for the packaging workflow:

```bash
PACKAGE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.packaging_mediaconvert')
aws stepfunctions start-execution \
  --state-machine-arn "${PACKAGE_ARN}" \
  --name sample-consulta-01-package \
  --input file://polly-manifest-output.json
```

Final MP4 output lands under:

```text
s3://<bucket>/video_output/mediaconvert/<job_name>/
```

The assembled dubbed audio used by MediaConvert lands under:

```text
s3://<bucket>/audio_output/assembled/<job_name>/dubbed.m4a
```

Assembly diagnostics land next to the audio:

```text
s3://<bucket>/audio_output/assembled/<job_name>/diagnostics.json
```

The packaging input can include optional assembly controls:

```json
{
  "assembly_options": {
    "fit_to_segment_duration": true,
    "min_tempo_factor": 0.8,
    "max_tempo_factor": 1.2,
    "target_padding_ms": 120,
    "short_segment_padding_threshold_ms": 1500,
    "audio_timing_offset_ms": 0,
    "loudness_target_lufs": -16,
    "ffmpeg_segment_chunk_size": 80,
    "ffprobe_concurrency": 8
  }
}
```

For large jobs, keep `"fit_to_segment_duration": true` and tune `"ffprobe_concurrency"` first. The default is `8`, clamped to `1-16`, so Polly duration measurement stays objective while avoiding a long serial `ffprobe` pass. For long sparse timelines, start with `"ffprobe_concurrency": 16` and `"ffmpeg_segment_chunk_size": 25`. If a job still hits the Lambda 900-second limit, `"fit_to_segment_duration": false` remains an emergency fast path that skips duration measurement and tempo fitting while preserving timestamp placement, chunked FFmpeg mixing, loudness normalization, and MediaConvert packaging.

## Configuration Notes

- Default AWS Region is `eu-west-1`.
- Default Transcribe language is `es-US`; set `transcribe_language_code = "es-ES"` if your videos use Spain Spanish.
- Transcription parsing currently uses the moderate segment expansion experiment by default: max `8.5` seconds and max `24` words, with a hard duration tolerance of `1.20x` for finding a cleaner nearby boundary. The parser preserves speaker changes and long pauses, ranks split candidates by sentence boundary, linguistic completeness, preferred punctuation, pause, and closeness to the soft duration target, then chooses the least bad valid boundary when a split is unavoidable. Short-duration fragments under `1.25` seconds are coalesced across same-speaker gaps up to `1.50` seconds, and filler-only micro segments under `750ms` are omitted. Segments under `3` words are reported with `low_word_count` but are not treated as failed timing targets when their duration is adequate. These parser settings can be overridden in the parser Lambda event without adding Terraform variables.
- Polly uses `polly_region = "eu-central-1"` and `polly_engine = "generative"` by default so the workflow can access generative voices while remaining deployed in `eu-west-1`. Polly segment audio is written to a separate S3 bucket in `eu-central-1`.
- Polly SSML prosody and trailing breaks are optional hints. The default SSML does not add a trailing `<break>` because transcript offsets and FFmpeg assembly control segment timing.
- Bedrock model ID defaults to `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`. This is an EU Bedrock inference profile, so AWS may route inference to another EU Region while the workflow still runs from `eu-west-1`.
- The Bedrock prompt currently runs a prompt-only duration expansion experiment: it asks for complete spoken dubbing phrasing when direct English translation is too short, while forbidding unsupported clinical facts and duration estimates.
- The packaging workflow aligns synthesized speech by segment start timestamp and applies bounded segment-specific tempo correction. Normal segments reserve `target_padding_ms`; segments shorter than `short_segment_padding_threshold_ms` keep their full target window. If a segment needs correction outside the configured tempo bounds, the remaining gap or overlap is kept and `requires_upstream_fix` is set in diagnostics. For large segment counts, the assembly Lambda renders local overlap-safe chunk spans with `ffmpeg_segment_chunk_size`, inserts explicit silence between chunks, and concatenates the final audio without a full-timeline remixer.
- Same-speaker gap compression and offset compaction are intentionally not part of the active experiment. Review the `8.5s / 24 words` segmentation diagnostics and the dubbed MP4 first, then evaluate gap compression separately if preserved pauses still dominate the listening issue.
- Intermediate transcription, translation, and Polly handoffs are S3-pointer based to avoid Step Functions payload limits. Inline transcription `segments` are intentionally slim; Polly synthesis tasks, synthesis results, Map result exports, and full manifests are stored as S3 objects; new consumers should prefer the S3 pointer fields returned by each workflow when full metadata is needed. The Polly synthesis Map is distributed and does not carry aggregate child output forward in state, avoiding both the 25,000-event history limit and the 256 KiB state output limit.
- S3 buckets do not configure lifecycle expiration or age-based deletion in this PoC. Objects are retained until explicitly deleted or the bucket is destroyed during teardown.
