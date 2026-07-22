#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

read -r -p "Enter job name: " JOB_NAME
if [[ -z "$JOB_NAME" ]]; then
  echo "Job name is required." >&2
  exit 1
fi

read -r -p "Local video path, or leave empty if already uploaded: " LOCAL_VIDEO
read -r -p "Translation path [translate/bedrock]: " TRANSLATION_PATH
TRANSLATION_PATH="${TRANSLATION_PATH:-translate}"

case "$TRANSLATION_PATH" in
  translate|amazon|amazon_translate)
    TRANSLATION_PATH="translate"
    ;;
  bedrock)
    TRANSLATION_PATH="bedrock"
    ;;
  *)
    echo "Unsupported translation path: ${TRANSLATION_PATH}. Use translate or bedrock." >&2
    exit 1
    ;;
esac

BUCKET=$(terraform -chdir=terraform output -raw data_bucket_name)
TRANSCRIBE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.transcription')
TRANSLATE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.translate_amazon')
BEDROCK_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.translate_bedrock')
POLLY_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.dubbing_polly')
PACKAGE_ARN=$(terraform -chdir=terraform output -json state_machine_arns | jq -r '.packaging_mediaconvert')

VIDEO_URI="s3://${BUCKET}/input/${JOB_NAME}.mp4"
# VIDEO_URI="s3://${BUCKET}/input/sample-consulta-01.mp4"
# VIDEO_URI="s3://${BUCKET}/input/123.mp4"

echo
echo "Job: ${JOB_NAME}"
echo "Bucket: ${BUCKET}"
echo "Video URI: ${VIDEO_URI}"
echo "Translation path: ${TRANSLATION_PATH}"

if [[ -n "$LOCAL_VIDEO" ]]; then
  echo
  echo "Uploading video..."
  aws s3 cp "$LOCAL_VIDEO" "$VIDEO_URI"
fi

wait_for_execution() {
  local execution_arn="$1"
  local label="$2"

  echo
  echo "Waiting for ${label}..."
  while true; do
    status=$(aws stepfunctions describe-execution \
      --execution-arn "$execution_arn" \
      --query status \
      --output text)

    echo "  ${label}: ${status}"

    case "$status" in
      SUCCEEDED)
        return 0
        ;;
      FAILED|TIMED_OUT|ABORTED)
        aws stepfunctions describe-execution \
          --execution-arn "$execution_arn" \
          --query '{status:status,error:error,cause:cause}' \
          --output json
        return 1
        ;;
      *)
        sleep 20
        ;;
    esac
  done
}

pull_output() {
  local execution_arn="$1"
  local output_file="$2"

  aws stepfunctions describe-execution \
    --execution-arn "$execution_arn" \
    --query output \
    --output text > "$output_file"

  echo "Saved: ${output_file}"
}

echo
echo "Starting transcription..."
TRANSCRIPTION_EXEC_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "$TRANSCRIBE_ARN" \
  --name "${JOB_NAME}-transcription-$(date +%s)" \
  --input "{
    \"job_name\": \"${JOB_NAME}\",
    \"media_uri\": \"${VIDEO_URI}\"
  }" \
  --query executionArn \
  --output text)
wait_for_execution "$TRANSCRIPTION_EXEC_ARN" "transcription"
pull_output "$TRANSCRIPTION_EXEC_ARN" "${JOB_NAME}-transcription-output.json"

TRANSLATION_OUTPUT_FILE="${JOB_NAME}-translation-output.json"

if [[ "$TRANSLATION_PATH" == "translate" ]]; then
  echo
  echo "Starting Amazon Translate..."
  TRANSLATE_EXEC_ARN=$(aws stepfunctions start-execution \
    --state-machine-arn "$TRANSLATE_ARN" \
    --name "${JOB_NAME}-translate-$(date +%s)" \
    --input file://"${JOB_NAME}-transcription-output.json" \
    --query executionArn \
    --output text)
  wait_for_execution "$TRANSLATE_EXEC_ARN" "amazon translate"
  pull_output "$TRANSLATE_EXEC_ARN" "$TRANSLATION_OUTPUT_FILE"
else
  echo
  echo "Starting Bedrock translation..."
  BEDROCK_EXEC_ARN=$(aws stepfunctions start-execution \
    --state-machine-arn "$BEDROCK_ARN" \
    --name "${JOB_NAME}-bedrock-$(date +%s)" \
    --input file://"${JOB_NAME}-transcription-output.json" \
    --query executionArn \
    --output text)
  wait_for_execution "$BEDROCK_EXEC_ARN" "bedrock"
  pull_output "$BEDROCK_EXEC_ARN" "$TRANSLATION_OUTPUT_FILE"
fi

echo
echo "Starting Polly from ${TRANSLATION_PATH} output..."
POLLY_EXEC_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "$POLLY_ARN" \
  --name "${JOB_NAME}-polly-$(date +%s)" \
  --input file://"$TRANSLATION_OUTPUT_FILE" \
  --query executionArn \
  --output text)
wait_for_execution "$POLLY_EXEC_ARN" "polly"
pull_output "$POLLY_EXEC_ARN" "${JOB_NAME}-polly-manifest-output.json"

echo
echo "Starting MediaConvert packaging..."
PACKAGE_EXEC_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "$PACKAGE_ARN" \
  --name "${JOB_NAME}-package-$(date +%s)" \
  --input file://"${JOB_NAME}-polly-manifest-output.json" \
  --query executionArn \
  --output text)
wait_for_execution "$PACKAGE_EXEC_ARN" "mediaconvert"
pull_output "$PACKAGE_EXEC_ARN" "${JOB_NAME}-package-output.json"

echo
echo "S3 outputs:"
aws s3 ls "s3://${BUCKET}/transcripts/segments/${JOB_NAME}/" || true
if [[ "$TRANSLATION_PATH" == "translate" ]]; then
  aws s3 ls "s3://${BUCKET}/translations/amazon_translate/${JOB_NAME}/" || true
else
  aws s3 ls "s3://${BUCKET}/translations/bedrock/${JOB_NAME}/" || true
fi
aws s3 ls "s3://${BUCKET}/audio_output/polly/${JOB_NAME}/" || true
aws s3 ls "s3://${BUCKET}/manifests/polly/${JOB_NAME}/" || true
aws s3 ls "s3://${BUCKET}/video_output/mediaconvert/${JOB_NAME}/" || true

echo
echo "Done."
echo "Final video prefix: s3://${BUCKET}/video_output/mediaconvert/${JOB_NAME}/"
