output "lambda_role_arn" {
  description = "IAM role ARN used by transformer Lambdas."
  value       = aws_iam_role.lambda.arn
}

output "step_functions_role_arn" {
  description = "IAM role ARN used by Step Functions."
  value       = aws_iam_role.step_functions.arn
}

output "mediaconvert_role_arn" {
  description = "IAM service role ARN used by MediaConvert jobs."
  value       = aws_iam_role.mediaconvert.arn
}
