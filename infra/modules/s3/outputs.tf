output "bucket_name" {
  description = "Name of the bucket the application writes source PDFs to (S3_BUCKET)."
  value       = aws_s3_bucket.main.bucket
}

output "bucket_arn" {
  description = "ARN of the bucket, for IAM policies."
  value       = aws_s3_bucket.main.arn
}
