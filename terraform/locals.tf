locals {
  name_prefix = "${var.project_name}-${var.environment}"

  data_bucket_name = coalesce(
    var.data_bucket_name,
    "${local.name_prefix}-${random_id.bucket_suffix.hex}"
  )

  polly_output_bucket_name = coalesce(
    var.polly_output_bucket_name,
    "${local.name_prefix}-polly-${replace(var.polly_region, "-", "")}-${random_id.bucket_suffix.hex}"
  )

  bedrock_model_id_parts              = split(".", var.bedrock_model_id)
  bedrock_is_inference_profile_id     = !startswith(var.bedrock_model_id, "arn:") && contains(["eu", "us", "apac", "global"], local.bedrock_model_id_parts[0])
  bedrock_profile_foundation_model_id = local.bedrock_is_inference_profile_id ? join(".", slice(local.bedrock_model_id_parts, 1, length(local.bedrock_model_id_parts))) : var.bedrock_model_id
  bedrock_model_arn = coalesce(
    var.bedrock_model_arn,
    startswith(var.bedrock_model_id, "arn:")
    ? var.bedrock_model_id
    : local.bedrock_is_inference_profile_id
    ? "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}"
    : "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}"
  )
  bedrock_is_inference_profile                     = strcontains(local.bedrock_model_arn, ":inference-profile/")
  bedrock_foundation_model_arn_pattern_for_profile = local.bedrock_is_inference_profile ? "arn:aws:bedrock:*::foundation-model/${local.bedrock_profile_foundation_model_id}" : null

  translate_terminology_name = "${local.name_prefix}-medical-es-en"
  translate_terminology_file = abspath("${path.module}/../assets/translate/medical_terminology.csv")

  ffmpeg_layer_zip_path = abspath("${path.module}/../layers/ffmpeg/ffmpeg-layer.zip")
  ffmpeg_layer_zip_hash = fileexists(local.ffmpeg_layer_zip_path) ? filebase64sha256(local.ffmpeg_layer_zip_path) : null
  ffmpeg_layer_s3_key   = "config/layers/ffmpeg/ffmpeg-layer.zip"

  tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.tags
  )

  lambda_functions = {
    parse_transcribe_output = {
      source_dir  = "${path.module}/../lambdas/parse_transcribe_output"
      timeout     = 90
      memory_size = 512
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    render_vtt = {
      source_dir  = "${path.module}/../lambdas/render_vtt"
      timeout     = 60
      memory_size = 256
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    build_bedrock_prompts = {
      source_dir  = "${path.module}/../lambdas/build_bedrock_prompts"
      timeout     = 180
      memory_size = 512
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    merge_bedrock_output = {
      source_dir  = "${path.module}/../lambdas/merge_bedrock_output"
      timeout     = 300
      memory_size = 512
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    prepare_polly_tasks = {
      source_dir  = "${path.module}/../lambdas/prepare_polly_tasks"
      timeout     = 180
      memory_size = 512
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    polly_proxy = {
      source_dir  = "${path.module}/../lambdas/polly_proxy"
      timeout     = 30
      memory_size = 256
      environment = {
        DATA_BUCKET         = module.s3.bucket_name
        POLLY_ENGINE        = var.polly_engine
        POLLY_OUTPUT_BUCKET = module.polly_s3.bucket_name
        POLLY_REGION        = var.polly_region
      }
    }

    write_polly_manifest = {
      source_dir  = "${path.module}/../lambdas/write_polly_manifest"
      timeout     = 300
      memory_size = 512
      environment = {
        DATA_BUCKET = module.s3.bucket_name
      }
    }

    assemble_dubbed_audio = {
      source_dir             = "${path.module}/../lambdas/assemble_dubbed_audio"
      timeout                = var.assemble_audio_timeout_seconds
      memory_size            = var.assemble_audio_memory_size
      architectures          = ["x86_64"]
      layers                 = [aws_lambda_layer_version.ffmpeg.arn]
      ephemeral_storage_size = var.assemble_audio_ephemeral_storage_mb
      environment = {
        DATA_BUCKET = module.s3.bucket_name
        FFMPEG_PATH = "/opt/bin/ffmpeg"
      }
    }

    build_mediaconvert_job = {
      source_dir  = "${path.module}/../lambdas/build_mediaconvert_job"
      timeout     = 30
      memory_size = 256
      environment = {
        DATA_BUCKET            = module.s3.bucket_name
        MEDIACONVERT_ROLE_ARN  = module.iam.mediaconvert_role_arn
        MEDIACONVERT_QUEUE_ARN = module.mediaconvert.queue_arn
      }
    }
  }

  state_machine_definitions = {
    transcription = templatefile("${path.module}/../statemachines/transcription.asl.json.tftpl", {
      name_prefix                        = local.name_prefix
      data_bucket_name                   = module.s3.bucket_name
      language_code                      = var.transcribe_language_code
      media_format                       = var.transcribe_media_format
      max_speaker_labels                 = var.max_speaker_labels
      vocabulary_name                    = aws_transcribe_vocabulary.medical_es.vocabulary_name
      parse_transcribe_output_lambda_arn = module.lambda.function_arns.parse_transcribe_output
    })

    translate_amazon = templatefile("${path.module}/../statemachines/translate_amazon.asl.json.tftpl", {
      data_bucket_name               = module.s3.bucket_name
      terminology_name               = terraform_data.translate_terminology.input.name
      translate_source_language_code = split("-", var.transcribe_language_code)[0]
      translate_max_concurrency      = var.translate_max_concurrency
      render_vtt_lambda_arn          = module.lambda.function_arns.render_vtt
    })

    translate_bedrock = templatefile("${path.module}/../statemachines/translate_bedrock.asl.json.tftpl", {
      data_bucket_name                 = module.s3.bucket_name
      bedrock_model_id                 = var.bedrock_model_id
      bedrock_max_concurrency          = var.bedrock_max_concurrency
      build_bedrock_prompts_lambda_arn = module.lambda.function_arns.build_bedrock_prompts
      merge_bedrock_output_lambda_arn  = module.lambda.function_arns.merge_bedrock_output
    })

    dubbing_polly = templatefile("${path.module}/../statemachines/dubbing_polly.asl.json.tftpl", {
      data_bucket_name                = module.s3.bucket_name
      polly_output_bucket_name        = module.polly_s3.bucket_name
      polly_engine                    = var.polly_engine
      polly_voice_map_json            = jsonencode(var.polly_voice_map)
      polly_max_concurrency           = var.polly_max_concurrency
      polly_proxy_lambda_arn          = module.lambda.function_arns.polly_proxy
      prepare_polly_tasks_lambda_arn  = module.lambda.function_arns.prepare_polly_tasks
      write_polly_manifest_lambda_arn = module.lambda.function_arns.write_polly_manifest
    })

    packaging_mediaconvert = templatefile("${path.module}/../statemachines/packaging_mediaconvert.asl.json.tftpl", {
      data_bucket_name                  = module.s3.bucket_name
      assemble_dubbed_audio_lambda_arn  = module.lambda.function_arns.assemble_dubbed_audio
      build_mediaconvert_job_lambda_arn = module.lambda.function_arns.build_mediaconvert_job
      mediaconvert_role_arn             = module.iam.mediaconvert_role_arn
      mediaconvert_queue_arn            = module.mediaconvert.queue_arn
    })
  }
}
