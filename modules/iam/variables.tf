variable "name_prefix" {
  description = "Project/environment prefix for named resources."
  type        = string
}

variable "account_id" {
  description = "AWS account ID."
  type        = string
}

variable "aws_region" {
  description = "AWS region."
  type        = string
}

variable "data_bucket_name" {
  description = "S3 data bucket name."
  type        = string
}

variable "data_bucket_arn" {
  description = "S3 data bucket ARN."
  type        = string
}

variable "polly_bucket_arn" {
  description = "S3 bucket ARN used for Polly synthesis output."
  type        = string
}

variable "bedrock_model_arn" {
  description = "Bedrock model ARN or inference profile ARN allowed for InvokeModel."
  type        = string
}

variable "bedrock_foundation_model_arn_pattern_for_profile" {
  description = "Foundation model ARN pattern allowed only when invoking through the configured inference profile."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}
