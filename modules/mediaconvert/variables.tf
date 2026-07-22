variable "name_prefix" {
  description = "Project/environment prefix for named resources."
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}
