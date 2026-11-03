# =============================================================================
# W5 D6 · Terraform 本地 IaC —— 全新「环境即代码」环境
# -----------------------------------------------------------------------------
# 范围（任务卡 2026-10-14 降权，达标即可）：
#   1. kind 集群（llmops-iaac）—— 全新独立集群，不接管现有手工 llmops 集群
#   2. Postgres/pgvector 数据库（StatefulSet + PVC + Service + ConfigMap + Secret）
# 不覆盖：应用层（归 helm/CI）、网关、ingress、mlflow —— 那些是部署层，不是环境层。
#
# 【为什么另起全新集群，不接管现有 llmops】
#   · Terraform 的价值是「从声明式文件一键重建」，不是「接管已有环境」
#   · 现有 llmops 是 W1 起 kubectl apply 手工作坊的，import 会脏（PVC 无 tf 标签、字段被当漂移改回）
#   · 现有 postgres 有真实数据，destroy 会真删数据盘，接管 = 把在用数据挂进 IaC 管理，风险配不上收益
#   · 验收「destroy 后 apply 能重建」= 演示从声明式重建新环境，不是删旧集群
#
# 【provider 选型】
#   · kind = tehcyx/kind（kyma-incubator/kind 已 archived 2022，tehcyx 是活跃维护 fork，2026-10-22 发 v0.13.0）
#   · kubernetes = hashicorp/kubernetes（官方，接 kind 集群的 kubeconfig）
# =============================================================================

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

provider "kind" {}

# -----------------------------------------------------------------------------
# 1. kind 集群：全新独立集群 llmops-iaac
#    kubeconfig_path 让 kubeconfig 落到本目录，供 kubernetes provider 使用
# -----------------------------------------------------------------------------
resource "kind_cluster" "llmops_iaac" {
  name            = "llmops-iaac"
  kubeconfig_path = abspath("${path.module}/kubeconfig")
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    # 只绑 127.0.0.1，不暴露到局域网
    networking {
      api_server_address = "127.0.0.1"
    }

    # 节点 containerd 走宿主同款镜像加速（国内直连 Docker Hub 不通，
    # Pod 会 ImagePullBackOff——见手册坑清单「节点拉镜像走 mirror」）
    containerd_config_patches = [
      <<-EOT
        [plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
          endpoint = ["https://docker.1ms.run", "https://docker.m.daocloud.io", "https://hub.rat.dev"]
      EOT
    ]

    node {
      role = "control-plane"
    }
  }
}

# -----------------------------------------------------------------------------
# 2. 把本地 pgvector 镜像 load 进 kind 集群
#    ⚠️ Docker 29 已知 bug（kind issue #3795/#4066）：containerd image store 下
#    `docker save` 导出的 OCI index 引用了未导出的 blob，节点 `ctr images import`
#    报 `content digest not found`。Docker 29 未修。
#    → 不用 kind_load：让 StatefulSet 直接从 registry 拉镜像（节点走宿主网络）。
#      首次拉 631MB 稍慢，拉完永久进节点（deploy/destroy/apply 循环不再重拉）。
# -----------------------------------------------------------------------------
# resource "kind_load" "pgvector" {
#   image        = "pgvector/pgvector:pg16"
#   cluster_name = kind_cluster.llmops_iaac.name
# }

# -----------------------------------------------------------------------------
# 3. kubernetes provider：接上面 kind 集群的 kubeconfig
# -----------------------------------------------------------------------------
provider "kubernetes" {
  config_path = kind_cluster.llmops_iaac.kubeconfig_path
}

# -----------------------------------------------------------------------------
# 4. Postgres/pgvector 数据库（声明式，复用 infra/k8s/postgres.yaml 口径）
#    但做了本地 IaC 简化：
#    · 存储用 hostPath 最简版（任务卡说「存储最简 hostPath」）
#    · 密钥用 .tfvars 占位（任务卡说「密钥用 .tfvars 占位即可」）
# -----------------------------------------------------------------------------

# 4a. namespace —— 独立命名空间隔离 IaC 环境，不污染 default
resource "kubernetes_namespace_v1" "iaac" {
  metadata {
    name = "iaac"
  }

  depends_on = [kind_cluster.llmops_iaac]
}

# 4b. ConfigMap —— 非敏感项（DB 名、用户）
resource "kubernetes_config_map_v1" "postgres" {
  metadata {
    name      = "postgres-config"
    namespace = kubernetes_namespace_v1.iaac.metadata[0].name
  }

  data = {
    POSTGRES_DB   = var.postgres_db
    POSTGRES_USER = var.postgres_user
  }

  depends_on = [kubernetes_namespace_v1.iaac]
}

# 4c. Secret —— 密码从 .tfvars 占位注入，不写死在 main.tf
resource "kubernetes_secret_v1" "postgres" {
  metadata {
    name      = "postgres-secret"
    namespace = kubernetes_namespace_v1.iaac.metadata[0].name
  }

  data = {
    POSTGRES_PASSWORD = var.postgres_password
  }

  depends_on = [kubernetes_namespace_v1.iaac]
}

# 4d. ConfigMap —— 初始化 SQL（建 pgvector 扩展）
resource "kubernetes_config_map_v1" "postgres_initdb" {
  metadata {
    name      = "postgres-initdb"
    namespace = kubernetes_namespace_v1.iaac.metadata[0].name
  }

  data = {
    "01-extensions.sql" = <<-EOT
      CREATE EXTENSION IF NOT EXISTS vector;
    EOT
  }

  depends_on = [kubernetes_namespace_v1.iaac]
}

# 4e. Headless Service —— StatefulSet 稳定网络标识
resource "kubernetes_service_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = kubernetes_namespace_v1.iaac.metadata[0].name
    labels = {
      app = "postgres"
    }
  }

  spec {
    cluster_ip = "None" # headless：DNS 直接返回 Pod IP
    selector = {
      app = "postgres"
    }
    port {
      name        = "postgres"
      port        = 5432
      target_port = 5432
    }
  }

  depends_on = [kubernetes_namespace_v1.iaac]
}

# 4f. StatefulSet + PVC —— 核心数据库
resource "kubernetes_stateful_set_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = kubernetes_namespace_v1.iaac.metadata[0].name
    labels = {
      app = "postgres"
    }
  }

  spec {
    service_name = kubernetes_service_v1.postgres.metadata[0].name
    replicas     = 1
    selector {
      match_labels = {
        app = "postgres"
      }
    }
    template {
      metadata {
        labels = {
          app = "postgres"
        }
      }
      spec {
        container {
          name  = "postgres"
          image = "pgvector/pgvector:pg16"
          # 镜像已 kind_load 进集群，IfNotPresent 用本地
          image_pull_policy = "IfNotPresent"

          port {
            name           = "postgres"
            container_port = 5432
          }

          env {
            name  = "POSTGRES_DB"
            value_from {
              config_map_key_ref {
                name = kubernetes_config_map_v1.postgres.metadata[0].name
                key  = "POSTGRES_DB"
              }
            }
          }
          env {
            name  = "POSTGRES_USER"
            value_from {
              config_map_key_ref {
                name = kubernetes_config_map_v1.postgres.metadata[0].name
                key  = "POSTGRES_USER"
              }
            }
          }
          env {
            name  = "POSTGRES_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "POSTGRES_PASSWORD"
              }
            }
          }
          # 数据目录必须在挂载点下，否则数据写在容器层 → 删 Pod 就没了
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }

          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "initdb"
            mount_path = "/docker-entrypoint-initdb.d"
          }

          # 存活/就绪探测：pg 官方镜像自带 pg_isready
          readiness_probe {
            exec {
              command = ["sh", "-c", "pg_isready -U $POSTGRES_USER -d $POSTGRES_DB -h 127.0.0.1"]
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          liveness_probe {
            exec {
              command = ["sh", "-c", "pg_isready -U $POSTGRES_USER -d $POSTGRES_DB -h 127.0.0.1"]
            }
            initial_delay_seconds = 15
            period_seconds        = 10
          }
        }

        volume {
          name = "initdb"
          config_map {
            name = kubernetes_config_map_v1.postgres_initdb.metadata[0].name
          }
        }
      }
    }
    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "standard"
        resources {
          requests = {
            storage = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [kubernetes_service_v1.postgres]
}