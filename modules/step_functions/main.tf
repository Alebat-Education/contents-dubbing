resource "aws_cloudwatch_log_group" "states" {
  for_each          = var.state_machines
  name              = "/aws/vendedlogs/states/${var.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_sfn_state_machine" "this" {
  for_each = var.state_machines

  name       = "${var.name_prefix}-${each.key}"
  role_arn   = var.role_arn
  type       = "STANDARD"
  definition = each.value.definition
  tags       = var.tags

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"
    log_destination        = "${aws_cloudwatch_log_group.states[each.key].arn}:*"
  }

  tracing_configuration {
    enabled = true
  }
}
