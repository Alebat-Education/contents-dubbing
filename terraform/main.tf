data "aws_caller_identity" "current" {}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

module "s3" {
  source = "../modules/s3"

  name_prefix   = local.name_prefix
  bucket_name   = local.data_bucket_name
  force_destroy = var.force_destroy_bucket
  tags          = local.tags
}

module "polly_s3" {
  source = "../modules/s3"

  providers = {
    aws = aws.polly
  }

  name_prefix   = "${local.name_prefix}-polly"
  bucket_name   = local.polly_output_bucket_name
  force_destroy = var.force_destroy_bucket
  folder_prefixes = [
    "audio_output/polly/",
  ]
  tags = local.tags
}

resource "aws_s3_object" "transcribe_vocabulary" {
  bucket       = module.s3.bucket_name
  key          = "config/transcribe/medical_vocabulary_es.tsv"
  source       = "${path.module}/../assets/transcribe/medical_vocabulary_es.tsv"
  etag         = filemd5("${path.module}/../assets/transcribe/medical_vocabulary_es.tsv")
  content_type = "text/tab-separated-values; charset=utf-8"
}

resource "aws_transcribe_vocabulary" "medical_es" {
  vocabulary_name     = "${local.name_prefix}-medical-es"
  language_code       = var.transcribe_language_code
  vocabulary_file_uri = "s3://${module.s3.bucket_name}/${aws_s3_object.transcribe_vocabulary.key}"
  tags                = local.tags
}

resource "terraform_data" "translate_terminology" {
  input = {
    name      = local.translate_terminology_name
    region    = var.aws_region
    file_path = local.translate_terminology_file
    file_hash = filebase64sha256(local.translate_terminology_file)
  }

  triggers_replace = [
    local.translate_terminology_name,
    var.aws_region,
    filebase64sha256(local.translate_terminology_file),
  ]

  provisioner "local-exec" {
    command = "aws translate import-terminology --region ${self.input.region} --name ${self.input.name} --merge-strategy OVERWRITE --data-file fileb://${self.input.file_path} --terminology-data Format=CSV"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "aws translate delete-terminology --region ${self.input.region} --name ${self.input.name} || true"
  }
}

module "iam" {
  source = "../modules/iam"

  name_prefix                                      = local.name_prefix
  account_id                                       = data.aws_caller_identity.current.account_id
  aws_region                                       = var.aws_region
  data_bucket_name                                 = module.s3.bucket_name
  data_bucket_arn                                  = module.s3.bucket_arn
  polly_bucket_arn                                 = module.polly_s3.bucket_arn
  bedrock_foundation_model_arn_pattern_for_profile = local.bedrock_foundation_model_arn_pattern_for_profile
  bedrock_model_arn                                = local.bedrock_model_arn
  tags                                             = local.tags
}

module "mediaconvert" {
  source = "../modules/mediaconvert"

  name_prefix = local.name_prefix
  tags        = local.tags
}

resource "aws_lambda_layer_version" "ffmpeg" {
  s3_bucket                = module.s3.bucket_name
  s3_key                   = aws_s3_object.ffmpeg_layer.key
  s3_object_version        = aws_s3_object.ffmpeg_layer.version_id
  layer_name               = "${local.name_prefix}-ffmpeg"
  description              = "Static FFmpeg and ffprobe binaries for audio assembly."
  source_code_hash         = local.ffmpeg_layer_zip_hash
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]
}

resource "aws_s3_object" "ffmpeg_layer" {
  bucket       = module.s3.bucket_name
  key          = local.ffmpeg_layer_s3_key
  source       = local.ffmpeg_layer_zip_path
  source_hash  = filemd5(local.ffmpeg_layer_zip_path)
  content_type = "application/zip"
}

module "lambda" {
  source = "../modules/lambda"

  name_prefix = local.name_prefix
  role_arn    = module.iam.lambda_role_arn
  functions   = local.lambda_functions
  tags        = local.tags
}

module "step_functions" {
  source = "../modules/step_functions"

  name_prefix        = local.name_prefix
  role_arn           = module.iam.step_functions_role_arn
  state_machines     = { for key, definition in local.state_machine_definitions : key => { definition = definition } }
  log_retention_days = var.log_retention_days
  tags               = local.tags
}
