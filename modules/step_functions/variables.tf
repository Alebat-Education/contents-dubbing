variable "name_prefix" {
  description = "Project/environment prefix for named resources."
  type        = string
}

variable "role_arn" {
  description = "IAM role ARN for Step Functions."
  type        = string
}

variable "state_machines" {
  description = "State machine definitions by logical name."
  type = map(object({
    definition = string
  }))
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}
