locals {
  package_dir = "${path.root}/.lambda-packages"
}

data "archive_file" "function" {
  for_each    = var.functions
  type        = "zip"
  source_dir  = each.value.source_dir
  output_path = "${local.package_dir}/${each.key}.zip"
}

resource "aws_lambda_function" "this" {
  for_each = var.functions

  function_name    = "${var.name_prefix}-${each.key}"
  role             = var.role_arn
  handler          = each.value.handler
  runtime          = each.value.runtime
  filename         = data.archive_file.function[each.key].output_path
  source_code_hash = data.archive_file.function[each.key].output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory_size
  architectures    = each.value.architectures
  layers           = each.value.layers
  tags             = var.tags

  dynamic "ephemeral_storage" {
    for_each = each.value.ephemeral_storage_size == null ? [] : [each.value.ephemeral_storage_size]

    content {
      size = ephemeral_storage.value
    }
  }

  environment {
    variables = each.value.environment
  }
}
