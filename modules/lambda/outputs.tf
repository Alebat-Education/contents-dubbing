output "function_arns" {
  description = "Lambda function ARNs by key."
  value       = { for key, fn in aws_lambda_function.this : key => fn.arn }
}

output "function_names" {
  description = "Lambda function names by key."
  value       = { for key, fn in aws_lambda_function.this : key => fn.function_name }
}
