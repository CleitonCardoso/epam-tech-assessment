variable "name" {
  description = "Prefix for every resource this module creates."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the application host."
  type        = string
  default     = "t3.small"
}

variable "bucket_arn" {
  description = "ARN of the S3 bucket the instance is allowed to read and write."
  type        = string
}

variable "key_name" {
  description = "Existing EC2 key pair name. Null means no key is attached (use SSM instead)."
  type        = string
  default     = null
}

variable "ssh_cidr_blocks" {
  description = "CIDRs allowed to reach port 22. Empty (default) means SSH is closed."
  type        = list(string)
  default     = []
}

variable "root_volume_gb" {
  description = "Root volume size. Holds the Redis volume and the container images, so do not undersize it."
  type        = number
  default     = 30
}

variable "app_dir" {
  description = "Directory on the host where the compose stack lives."
  type        = string
  default     = "/opt/app"
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
