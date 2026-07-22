output "data_bucket_name" {
  description = "S3 bucket used for input, intermediate artifacts, and outputs."
  value       = module.s3.bucket_name
}

output "polly_output_bucket_name" {
  description = "Regional S3 bucket used for Amazon Polly synthesis output."
  value       = module.polly_s3.bucket_name
}

output "state_machine_arns" {
  description = "Step Functions state machine ARNs by workflow."
  value       = module.step_functions.state_machine_arns
}

output "lambda_function_names" {
  description = "Transformer Lambda function names by logical key."
  value       = module.lambda.function_names
}

output "assemble_dubbed_audio_lambda_name" {
  description = "FFmpeg audio assembly Lambda function name."
  value       = module.lambda.function_names.assemble_dubbed_audio
}

output "mediaconvert_queue_arn" {
  description = "MediaConvert queue ARN used by the packaging workflow."
  value       = module.mediaconvert.queue_arn
}

output "transcribe_vocabulary_name" {
  description = "Amazon Transcribe custom vocabulary name."
  value       = aws_transcribe_vocabulary.medical_es.vocabulary_name
}

output "translate_terminology_name" {
  description = "Amazon Translate custom terminology name."
  value       = terraform_data.translate_terminology.input.name
}
