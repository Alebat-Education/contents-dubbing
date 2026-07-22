output "bucket_name" {
  description = "Data bucket name."
  value       = aws_s3_bucket.data.bucket
}

output "bucket_arn" {
  description = "Data bucket ARN."
  value       = aws_s3_bucket.data.arn
}
