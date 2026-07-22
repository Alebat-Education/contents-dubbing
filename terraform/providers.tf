provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

provider "aws" {
  alias  = "polly"
  region = var.polly_region

  default_tags {
    tags = local.tags
  }
}
