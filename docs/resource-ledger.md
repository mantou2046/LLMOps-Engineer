# 本地 LLMOps 环境资源账本（W2 D11）

> 目的：回答周验收的最后一问 ——「**本地跑这套环境到底消耗多少资源（CPU / 内存 / 磁盘）**」。
> 口径：2026-10-15 实测，`kind` 单节点 + 宿主 Docker 上的 Langfuse/Ollama/Postgres。
> 采集方式：`kubectl top` / `kubectl describe node` / `docker system df` / `df -h`。

## 1. 集群节点容量与用量

| 项 | 容量 | 空载实测（17 Pod） | 占比 |
| --- | --- | --- | --- |
| CPU | 8 vCPU | **233m** | 2% |
| 内存 | 8.1 GiB | **1632 MiB** | 20% |
| Pod 上限 | 110 | 17 | 15% |
| 节点临时存储 | ~1006 GiB | — | — |

**调度承诺（Allocated resources）**：

| 资源 | Requests | Limits |
| --- | --- | --- |
| CPU | 1600m (20%) | 3100m (38%) |
| 内存 | 1604Mi (20%) | 2900Mi (36%) |

> 读法：**节点 CPU 空载只用了 2%，瓶颈不在 CPU 而在内存与磁盘**。17 个 Pod 吃掉 1.6 GiB
> （其中 apiserver 316Mi + controller-manager 110Mi + ingress 138Mi 占了大头 —— 控制面本身就很重）。

## 2. 各命名空间 Pod 分布

| 命名空间 | Pod 数 | 说明 |
| --- | --- | --- |
| kube-system | 9 | 控制面 + coredns + metrics-server + kindnet + kube-proxy |
| default | 3 | W1 的 llmops-api(2) + postgres(1) |
| prod | 2 | 本次 D11 的 Chart 部署 |
| dev | 1 | 本次 D11 的 Chart 部署（HPA min=1） |
| ingress-nginx | 1 | Ingress 控制器 |
| local-path-storage | 1 | kind 默认 StorageClass 供应器 |
| **合计** | **17** | |

## 3. 应用侧资源（Chart 的 requests / limits）

| 环境 | 副本 | requests.cpu | limits.cpu | requests.mem | limits.mem |
| --- | --- | --- | --- | --- | --- |
| dev | 1（HPA 1→5） | 50m | 500m | 128Mi | 256Mi |
| prod | 2（HPA 2→6） | 100m | 300m | 192Mi | 384Mi |

**HPA 压测峰值实测**（dev，12 并发 × 120s 打 `/work`）：

| 时刻 | dev Pod 数 | 节点 CPU |
| --- | --- | --- |
| t=10s | 1 | 514m |
| t=20s | 4 | 514m |
| t=30s | 4 | **1783m（峰值）** |
| t=40s | 5（到顶） | 548m |
| 停压 +5min | **1（缩回）** | 267m |

> ⚠️ 峰值只有 1.8 核（8 核的 22%），因为每个 Pod 的 `limits.cpu=500m` 是硬顶，
> 5 副本合计上限 2.5 核 —— **HPA 扩副本并不能突破单 Pod 的 CPU 上限**。
> 这也解释了为什么 12 并发压 0.2s 的任务时大量请求排队（p50 999ms ≫ 200ms）。

## 4. Docker 磁盘占用（宿主）

| 类型 | 合计 | 可回收 |
| --- | --- | --- |
| Images | 18.37 GB | 1.39 GB |
| Containers | 371.6 MB | 0 |
| Local Volumes | 4.74 GB | 0 |
| Build Cache | 232.1 MB | ~0 |
| **Docker 内部合计** | **≈ 23.7 GB** | |

**镜像明细**：

| 镜像 | 大小 | 说明 |
| --- | --- | --- |
| `llmops-api:0.2.0` / `0.3.0` | 136 MB | W1/W2 早期版本 |
| `llmops-api:0.5.0` / `0.5.1` / `0.5.2` | 149 MB | 本次 D11 构建 |
| `ollama/ollama` | ~9.4 GB | 本地推理（D10） |
| Langfuse 6 服务 | ~数 GB | postgres/clickhouse/redis/minio/web/worker |

## 5. 数据盘与宿主机磁盘

| 项 | 大小 | 备注 |
| --- | --- | --- |
| `docker_data.vhdx` | **33.9 GB** | WSL2 虚拟磁盘（只涨不缩） |
| Docker 内部实际 | ≈ 23.7 GB | 与 vhdx 差 ~10 GB = **未 TRIM 的已删数据** |
| C: 可用 | 42 GB | 82% 已用 |
| E: 可用 | 99 GB | 76% 已用 |

> ⚠️ vhdx 与内部用量差 ~10 GB 是 WSL2 ext4 不做 TRIM 所致（10-15 已踩坑：`diskpart compact` 只能回收 2G）。

## 6. 一句话结论

> 这套「K8s 生产化 + 观测 + 本地推理」的完整环境，**空载约占 1.6 GiB 内存 + 2% CPU，
> 磁盘约 24 GB（Docker 内部）／34 GB（vhdx）**。CPU 不是瓶颈，**内存和磁盘才是** ——
> 本地单机跑全套的代价主要花在「控制面 + 观测栈」的固定开销上，而不是业务负载。
