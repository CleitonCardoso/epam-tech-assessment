variable "project" {
  description = "Prefix for resource names."
  type        = string
  default     = "async-pdf"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "prod"
}

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique bucket name for source PDFs (no default: it must be deliberate)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the application host."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Root volume size in GB (holds the Redis volume and images)."
  type        = number
  default     = 30
}

variable "key_name" {
  description = "Existing EC2 key pair for SSH. Leave null to keep SSH closed and use SSM instead."
  type        = string
  default     = null
}

variable "ssh_cidr_blocks" {
  description = "CIDRs allowed to reach SSH. Empty means the port stays closed."
  type        = list(string)
  default     = []
}
