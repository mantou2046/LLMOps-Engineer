# MLflow 服务端镜像（Postgres 后端版）
#
# 为什么需要自建：官方 ghcr.io/mlflow/mlflow:v3.17.1 里**没有 psycopg2/psycopg**
# （2026-10-22 实测：import psycopg2 → ModuleNotFoundError），而
# --backend-store-uri postgresql:// 需要它。官方镜像只够跑 SQLite 后端。
#
# 构建 + 载入 kind：
#   cd /e/Projects/LLMOps-Engineer
#   docker build -t llmops-mlflow:v3.17.1-pg infra/k8s/mlflow
#   kind load docker-image llmops-mlflow:v3.17.1-pg --name llmops

FROM ghcr.io/mlflow/mlflow:v3.17.1

# psycopg2-binary 是预编译 wheel，不需要 pg_config / gcc
RUN pip install --no-cache-dir psycopg2-binary==2.9.10

# 健康检查用官方镜像自带的 curl 不够稳，交给 k8s 的 httpGet 探针即可
