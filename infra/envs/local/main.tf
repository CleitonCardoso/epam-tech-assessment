# Local environment: the same S3 module as production, pointed at LocalStack.
#
# This is applied automatically by the `provisioner` service in compose.yaml, so a reviewer never
# installs Terraform: `docker compose up` runs init + apply against LocalStack first, and only
# then starts the API and worker. State lives next to this file (gitignored).

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "bucket_name" {
  description = "Bucket for source PDFs. Must match S3_BUCKET for the application."
  type        = string
  default     = "documents"
}

variable "localstack_endpoint" {
  description = "LocalStack edge endpoint, reachable from the provisioner container."
  type        = string
  default     = "http://localstack:4566"
}

provider "aws" {
  region = "us-east-1"

  # LocalStack accepts any credentials; these are intentionally not secrets.
  access_key = "test"
  secret_key = "test"

  # All three skips avoid the provider trying to talk to real AWS metadata/STS before every call.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  # Without path-style addressing the provider builds virtual-hosted URLs (bucket.localhost) that
  # LocalStack cannot resolve, and bucket creation hangs instead of failing.
  s3_use_path_style = true

  endpoints {
    s3 = var.localstack_endpoint
  }
}

module "s3" {
  source = "../../modules/s3"

  bucket_name = var.bucket_name
  # LocalStack never reports a lifecycle configuration as applied: the provider polls for ~3
  # minutes and then fails the whole apply. Disabled here, enabled in production.
  enable_lifecycle = false
  # Local iteration should be able to destroy and recreate freely.
  force_destroy = true

  tags = {
    Project     = "async-pdf-processing"
    Environment = "local"
  }
}

output "bucket_name" {
  description = "Bucket the application must use (S3_BUCKET)."
  value       = module.s3.bucket_name
}
