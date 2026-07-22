variable "name_prefix" {
  description = "Project/environment prefix for named resources."
  type        = string
}

variable "role_arn" {
  description = "IAM role ARN for Lambda functions."
  type        = string
}

variable "functions" {
  description = "Lambda function definitions."
  type = map(object({
    source_dir    = string
    handler       = optional(string, "handler.handler")
    runtime       = optional(string, "python3.12")
    timeout       = optional(number, 60)
    memory_size   = optional(number, 256)
    environment   = optional(map(string), {})
    architectures = optional(list(string), ["arm64"])
    layers        = optional(list(string), [])
    ephemeral_storage_size = optional(number)
  }))
}

variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}
