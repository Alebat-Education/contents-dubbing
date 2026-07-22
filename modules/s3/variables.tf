variable "name_prefix" {
  description = "Project/environment prefix for named resources."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name."
  type        = string
}

variable "force_destroy" {
  description = "Whether Terraform may delete non-empty buckets for PoC teardown."
  type        = bool
  default     = false
}

variable "folder_prefixes" {
  description = "S3 placeholder prefixes to create in the bucket."
  type        = list(string)
  default = [
    "input/",
    "transcripts/raw/",
    "transcripts/segments/",
    "translations/amazon_translate/",
    "translations/bedrock/",
    "audio_output/polly/",
    "video_output/mediaconvert/",
    "manifests/",
    "config/",
  ]
}

variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}
