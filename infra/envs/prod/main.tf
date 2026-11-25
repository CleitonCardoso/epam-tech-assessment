# Production environment: real AWS.
#
#   cd infra/envs/prod
#   cp terraform.tfvars.example terraform.tfvars   # then edit
#   terraform init
#   terraform plan
#   terraform apply
#
# State note: this uses local state by default so it runs with no extra setup. A real deployment
# should use remote state with locking (see the commented backend block below) — with local state,
# two operators planning at once can corrupt it.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # backend "s3" {
  #   bucket         = "your-tfstate-bucket"
  #   key            = "async-pdf-processing/prod.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "your-tfstate-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

module "s3" {
  source = "../../modules/s3"

  bucket_name = var.bucket_name
  # Real S3 honours this; the local environment has to disable it.
  enable_lifecycle = true
  # Production uploads are not disposable.
  force_destroy = false

  tags = local.tags
}

module "app" {
  source = "../../modules/ec2-app"

  name            = var.project
  instance_type   = var.instance_type
  bucket_arn      = module.s3.bucket_arn
  key_name        = var.key_name
  ssh_cidr_blocks = var.ssh_cidr_blocks
  root_volume_gb  = var.root_volume_gb
  tags            = local.tags
}

output "bucket_name" {
  description = "Set S3_BUCKET to this in the host's .env."
  value       = module.s3.bucket_name
}

output "public_ip" {
  description = "Elastic IP — point your DNS A record here."
  value       = module.app.public_ip
}

output "instance_role_arn" {
  description = "Instance profile role used for S3 access; no static keys needed on the box."
  value       = module.app.instance_role_arn
}

output "next_steps" {
  description = "What to do after apply."
  value       = <<-EOT
    1. DNS: point an A record at ${module.app.public_ip}
    2. Copy compose.yaml, frontend/, infra/, scripts/ to the host's /opt/app
    3. Create .env there with: S3_BUCKET=${module.s3.bucket_name}
       AWS_REGION=${var.aws_region}, AI_MODE=live, GEMINI_API_KEY=...,
       and leave AWS_ENDPOINT/S3_ENDPOINT_URL empty so the SDK uses real AWS + the instance role
    4. On the host: docker compose up -d --build   (no provisioner needed: the bucket exists)
    5. Put TLS in front (ALB + ACM, or nginx + Let's Encrypt on the box)
  EOT
}
