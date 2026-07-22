terraform {
  required_version = ">= 1.6.0"

  # The bucket and Region are supplied at init time from the ignored backend.hcl file.
  # Backend blocks cannot reference Terraform variables, locals, or resources.
  backend "s3" {
    key          = "terraform/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }

    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40.0"
    }

    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0"
    }
  }
}
