terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# 全部指向本地 aws-emulator：假凭据经环境变量注入（模拟器生成的凭据文件），
# 跳过一切真 AWS 校验；insecure=true 是因为模拟器用自签名证书。
provider "aws" {
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true
  insecure                    = true

  endpoints {
    s3       = "https://127.0.0.1:9297"
    dynamodb = "https://127.0.0.1:9297"
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "tf-llmops-artifacts"
}

resource "aws_s3_object" "metrics" {
  bucket  = aws_s3_bucket.artifacts.id
  key     = "mlflow/tf-run/metrics.txt"
  content = "source=terraform fmt=1.0 hit1=0.82\n"
}

resource "aws_dynamodb_table" "eval" {
  name         = "tf-eval-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_dynamodb_table_item" "row" {
  table_name = aws_dynamodb_table.eval.name
  hash_key   = aws_dynamodb_table.eval.hash_key
  item = jsonencode({
    id   = { S = "tf-001" }
    hit1 = { N = "0.82" }
  })
}

output "bucket" { value = aws_s3_bucket.artifacts.id }
output "table" { value = aws_dynamodb_table.eval.name }
