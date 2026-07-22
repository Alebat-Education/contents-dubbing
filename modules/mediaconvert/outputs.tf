output "queue_arn" {
  description = "MediaConvert queue ARN."
  value       = aws_media_convert_queue.this.arn
}
