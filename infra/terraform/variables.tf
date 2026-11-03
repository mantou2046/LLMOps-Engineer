# =============================================================================
# W5 D6 · Terraform 变量定义
# -----------------------------------------------------------------------------
# 密钥用 .tfvars 占位（任务卡要求）：密码不写死在 main.tf
# 用法：复制 terraform.tfvars.example 为 terraform.tfvars 并填 POSTGRES_PASSWORD
# =============================================================================

variable "postgres_db" {
  description = "Postgres 数据库名（非敏感，进 ConfigMap）"
  type        = string
  default     = "llmops"
}

variable "postgres_user" {
  description = "Postgres 用户名（非敏感，进 ConfigMap）"
  type        = string
  default     = "llmops"
}

variable "postgres_password" {
  description = "Postgres 密码（敏感，进 Secret；用 terraform.tfvars 占位注入，勿写死在 main.tf）"
  type        = string
  sensitive   = true
}