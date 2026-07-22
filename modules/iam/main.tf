locals {
  lambda_function_arn_pattern = "arn:aws:lambda:${var.aws_region}:${var.account_id}:function:${var.name_prefix}-*"
  state_machine_arn_pattern   = "arn:aws:states:${var.aws_region}:${var.account_id}:stateMachine:${var.name_prefix}-*"
  execution_arn_pattern       = "arn:aws:states:${var.aws_region}:${var.account_id}:execution:${var.name_prefix}-*:*"
  transcribe_job_arn_pattern  = "arn:aws:transcribe:${var.aws_region}:${var.account_id}:transcription-job/${var.name_prefix}-*"
  transcribe_vocab_arn        = "arn:aws:transcribe:${var.aws_region}:${var.account_id}:vocabulary/${var.name_prefix}-medical-es"
  s3_object_arns = [
    "${var.data_bucket_arn}/input/*",
    "${var.data_bucket_arn}/transcripts/*",
    "${var.data_bucket_arn}/translations/*",
    "${var.data_bucket_arn}/audio_output/*",
    "${var.data_bucket_arn}/video_output/*",
    "${var.data_bucket_arn}/manifests/*",
    "${var.data_bucket_arn}/config/*",
  ]
  polly_s3_object_arns = [
    "${var.polly_bucket_arn}/audio_output/polly/*",
  ]
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid = "WriteLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.name_prefix}-*:*"]
  }

  statement {
    sid = "ListProjectBucket"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]
    resources = [
      var.data_bucket_arn,
      var.polly_bucket_arn,
    ]
  }

  statement {
    sid = "ReadWriteProjectPrefixes"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = concat(local.s3_object_arns, local.polly_s3_object_arns)
  }

  statement {
    sid = "PollySynthesis"
    actions = [
      "polly:GetSpeechSynthesisTask",
      "polly:StartSpeechSynthesisTask",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name_prefix}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "step_functions" {
  name               = "${var.name_prefix}-states"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "step_functions" {
  statement {
    sid       = "InvokeTransformerLambdas"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.lambda_function_arn_pattern]
  }

  statement {
    sid       = "StartDistributedMapExecutions"
    actions   = ["states:StartExecution"]
    resources = [local.state_machine_arn_pattern]
  }

  statement {
    sid = "ManageDistributedMapExecutions"
    actions = [
      "states:DescribeExecution",
      "states:StopExecution",
    ]
    resources = [local.execution_arn_pattern]
  }

  statement {
    sid = "TranscribeJobs"
    actions = [
      "transcribe:GetTranscriptionJob",
      "transcribe:StartTranscriptionJob",
    ]
    resources = [
      local.transcribe_job_arn_pattern,
      local.transcribe_vocab_arn,
    ]
  }

  statement {
    sid       = "TranslateTextWithTerminology"
    actions   = ["translate:TranslateText"]
    resources = ["*"]
  }

  statement {
    sid       = "InvokeBedrockModel"
    actions   = ["bedrock:InvokeModel"]
    resources = [var.bedrock_model_arn]
  }

  dynamic "statement" {
    for_each = var.bedrock_foundation_model_arn_pattern_for_profile == null ? [] : [var.bedrock_foundation_model_arn_pattern_for_profile]

    content {
      sid       = "InvokeBedrockFoundationModelViaProfile"
      actions   = ["bedrock:InvokeModel"]
      resources = [statement.value]

      condition {
        test     = "StringLike"
        variable = "bedrock:InferenceProfileArn"
        values   = [var.bedrock_model_arn]
      }
    }
  }

  statement {
    sid = "MediaConvertJobs"
    actions = [
      "mediaconvert:CreateJob",
      "mediaconvert:GetJob",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassMediaConvertRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.mediaconvert.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["mediaconvert.amazonaws.com"]
    }
  }

  statement {
    sid = "ReadWriteWorkflowArtifacts"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:ListMultipartUploadParts",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = concat([var.data_bucket_arn], local.s3_object_arns)
  }

  statement {
    sid = "WriteStepFunctionsLogs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "step_functions" {
  name   = "${var.name_prefix}-states"
  role   = aws_iam_role.step_functions.id
  policy = data.aws_iam_policy_document.step_functions.json
}

data "aws_iam_policy_document" "mediaconvert_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["mediaconvert.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mediaconvert" {
  name               = "${var.name_prefix}-mediaconvert"
  assume_role_policy = data.aws_iam_policy_document.mediaconvert_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "mediaconvert" {
  statement {
    sid = "ListBucket"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]
    resources = [var.data_bucket_arn]
  }

  statement {
    sid = "ReadMediaInputs"
    actions = [
      "s3:GetObject",
    ]
    resources = [
      "${var.data_bucket_arn}/input/*",
      "${var.data_bucket_arn}/audio_output/polly/*",
      "${var.data_bucket_arn}/audio_output/assembled/*",
    ]
  }

  statement {
    sid = "WritePackagedVideo"
    actions = [
      "s3:PutObject",
    ]
    resources = ["${var.data_bucket_arn}/video_output/mediaconvert/*"]
  }
}

resource "aws_iam_role_policy" "mediaconvert" {
  name   = "${var.name_prefix}-mediaconvert"
  role   = aws_iam_role.mediaconvert.id
  policy = data.aws_iam_policy_document.mediaconvert.json
}
