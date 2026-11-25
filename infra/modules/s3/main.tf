# S3 bucket that stores the uploaded source PDFs.
#
# The same module is used locally (against LocalStack) and in production (against AWS) — only the
# provider in the env differs. That is what keeps "works locally" and "works in production" from
# drifting apart.

resource "aws_s3_bucket" "main" {
  bucket = var.bucket_name

  # Local dev sets this true so `terraform destroy` can clean up; production keeps it false so a
  # stray destroy cannot wipe customer uploads.
  force_destroy = var.force_destroy

  tags = var.tags

  lifecycle {
    precondition {
      condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63
      error_message = "bucket_name must be 3-63 characters (S3 naming rules)."
    }
  }
}

# Uploads are private: the app reads them through its own credentials and never serves a public
# URL. Presigned GETs are not used for source PDFs, only for the (currently unused) product images.
resource "aws_s3_bucket_public_access_block" "main" {
  bucket = aws_s3_bucket.main.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "main" {
  bucket = aws_s3_bucket.main.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Expire non-current versions so a versioned bucket does not grow without bound.
# LocalStack never confirms a lifecycle configuration (it polls, times out after ~3 minutes and
# fails the whole apply), so the local env disables it — the same trap the earlier research notes
# describe. Keeping it as a flag is cheaper than a forked module.
resource "aws_s3_bucket_lifecycle_configuration" "main" {
  count  = var.enable_lifecycle ? 1 : 0
  bucket = aws_s3_bucket.main.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}
