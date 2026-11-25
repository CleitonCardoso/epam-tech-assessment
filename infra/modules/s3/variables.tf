variable "bucket_name" {
  description = "Globally unique S3 bucket name for source PDFs."
  type        = string
}

variable "enable_lifecycle" {
  description = "Create the non-current-version expiry rule. Disable against LocalStack, which never confirms it."
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow destroy to delete objects first. True locally, false in production."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
