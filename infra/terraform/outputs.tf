# =============================================================================
# W5 D6 · Terraform 输出
# =============================================================================

output "cluster_name" {
  description = "kind 集群名（destroy/apply 重建的对象）"
  value       = kind_cluster.llmops_iaac.name
}

output "kubeconfig_path" {
  description = "kubeconfig 落盘路径（kubectl --kubeconfig 用它连 IaC 集群）"
  value       = kind_cluster.llmops_iaac.kubeconfig_path
}

output "postgres_namespace" {
  description = "Postgres 所在 namespace"
  value       = kubernetes_namespace_v1.iaac.metadata[0].name
}

output "postgres_service" {
  description = "Postgres headless service（集群内 DNS：postgres.iaac.svc.cluster.local:5432）"
  value       = kubernetes_service_v1.postgres.metadata[0].name
}