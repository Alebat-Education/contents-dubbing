output "state_machine_arns" {
  description = "State machine ARNs by logical name."
  value       = { for key, machine in aws_sfn_state_machine.this : key => machine.arn }
}
