variable "project_name" {
  description = "Base project name used in AWS resource names."
  type        = string
  default     = "video-dubbing-poc"
}

variable "environment" {
  description = "Environment name used in AWS resource names."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "Primary AWS region for the PoC workflows."
  type        = string
  default     = "eu-west-1"
}

variable "data_bucket_name" {
  description = "Optional globally unique S3 bucket name. If null, Terraform creates one with a random suffix."
  type        = string
  default     = null
}

variable "force_destroy_bucket" {
  description = "Allow Terraform to destroy non-empty S3 buckets. Useful for PoC teardown."
  type        = bool
  default     = false
}

variable "transcribe_language_code" {
  description = "Amazon Transcribe language code for Spanish source media."
  type        = string
  default     = "es-US"
}

variable "transcribe_media_format" {
  description = "Input media format passed to Amazon Transcribe."
  type        = string
  default     = "mp4"
}

variable "max_speaker_labels" {
  description = "Maximum number of speakers for Transcribe diarization."
  type        = number
  default     = 4
}

variable "translate_max_concurrency" {
  description = "Maximum concurrent Amazon Translate calls from the Map state."
  type        = number
  default     = 5
}

variable "bedrock_model_id" {
  description = "Bedrock model ID used by the evaluation translation workflow."
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "bedrock_model_arn" {
  description = "Optional Bedrock model or inference profile ARN for IAM scoping. Defaults to the foundation model ARN for bedrock_model_id."
  type        = string
  default     = null
}

variable "bedrock_max_concurrency" {
  description = "Maximum concurrent Bedrock InvokeModel calls from the Map state."
  type        = number
  default     = 2
}

variable "polly_max_concurrency" {
  description = "Maximum concurrent Polly synthesis tasks from the Map state."
  type        = number
  default     = 4
}

variable "polly_region" {
  description = "AWS region used by the Polly proxy Lambda for speech synthesis."
  type        = string
  default     = "eu-central-1"
}

variable "polly_output_bucket_name" {
  description = "Optional S3 bucket name for Polly synthesis output in polly_region. If null, Terraform creates one."
  type        = string
  default     = null
}

variable "polly_engine" {
  description = "Amazon Polly engine used by the dubbing workflow."
  type        = string
  default     = "generative"
}

variable "polly_voice_map" {
  description = "Speaker-to-Polly-voice mapping."
  type        = map(string)
  default = {
    spk_0 = "Ruth"
    spk_1 = "Stephen"
    spk_2 = "Joanna"
    spk_3 = "Salli"
  }
}

variable "assemble_audio_memory_size" {
  description = "Memory size in MB for the FFmpeg audio assembly Lambda."
  type        = number
  default     = 8192
}

variable "assemble_audio_timeout_seconds" {
  description = "Timeout in seconds for the FFmpeg audio assembly Lambda."
  type        = number
  default     = 900
}

variable "assemble_audio_ephemeral_storage_mb" {
  description = "Ephemeral storage in MB for the FFmpeg audio assembly Lambda."
  type        = number
  default     = 4096
}

variable "log_retention_days" {
  description = "CloudWatch log retention for Step Functions logs."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
