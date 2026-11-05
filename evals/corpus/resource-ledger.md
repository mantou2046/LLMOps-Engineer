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

> ⚠️ 下表为 **2026-10-16 清理后**实测；10-15 原值（Images 18.37 GB / 可回收 1.39 GB / Volumes 4.74 GB / Build Cache 232.1 MB）括号内保留于 §4.1 对照。

| 类型 | 合计 | 可回收 |
| --- | --- | --- |
| Images | **17.7 GB** | **776.6 MB** |
| Containers | 473.4 MB | 0 |
| Local Volumes | 5.649 GB | 0 |
| Build Cache | 351.6 MB | 36.14 MB |
| **Docker 内部合计** | **≈ 24.2 GB** | |

**镜像明细**：

| 镜像 | 大小 | 说明 |
| --- | --- | --- |
| `llmops-api:0.2.0` | 136 MB | W1 清单 `infra/k8s/app.yaml` **仍在跑**（default/llmops-api 2 副本）→ 保留 |
| `llmops-api:0.5.2` | 149 MB | 当前 Helm dev/prod 版本 → 保留 |
| `ollama/ollama` | ~9.4 GB | 本地推理（D10） |
| Langfuse 6 服务 | ~数 GB | postgres/clickhouse/redis/minio/web/worker |

### 4.1 为什么清理了 4 个旧 tag 却没省多少空间（10-16 实测认知）

删除的 4 个 tag：宿主 `0.3.0` / `0.5.0` / `0.5.1`，节点内另加 `0.1.0`。

**核心事实：5 个 tag 共享 149.2 MB 图层，各自独有层只有 ~130 KB。**
`docker system df -v` 的 SHARED/UNIQUE 列说明了一切 —— 它们共用 `python:3.12-alpine`
底座 + venv 层，删掉 4 个 tag 只回收 ≈0.5 MB。

**但可回收总量反而从 1.39 GB 涨到 1.513 GB**，因为删掉共享引用后，
`python:3.12-alpine`（83.6 MB，容器数 0）等底座镜像进入了可回收范围。
这也解释了为什么 `docker system df` 的 Images SIZE 略有上升（18.37 → 18.44 GB）——
总大小统计方式与 tag 数量无关，别被这个数字误导。

> **结论：旧 tag 清理的收益主要是「列表清爽 + 语义清晰」，不是省磁盘。**
> 真正能回收的大头在别处（`metrics-server` 111 MB、Build Cache 36 MB、python 底座 83.6 MB），
> 而最大的那个 —— `hpa-example` 736 MB —— 已单独处理，见 §4.3。

### 4.3 清 hpa-example 736 MB：先 untag 一次没删掉的坑（10-16 实测）

第一次 `docker rmi registry.k8s.io/hpa-example:latest` 只输出 `Untagged:`、**没有 `Deleted:`**，
`docker system df` 毫无变化。根因不是"图层被引用"，而是**同一个镜像 ID 上有两个 tag**：

```
registry.k8s.io/hpa-example:latest                                    93c2a5cc37c3  736MB
swr.cn-north-4.myhuaweicloud.com/ddn-k8s/registry.k8s.io/hpa-example 93c2a5cc37c3  736MB  ← 同一个 ID
```

即当时是从**华为云 K8s 镜像仓库**拉下来的，`registry.k8s.io` 只是个后加的别名。
**判据：`docker images -a` 里看到同名仓库 + 镜像 ID 相同 = 有多别名**，必须逐个 untag
直到出现 `Deleted:` 才算真删。补删华为云那条 tag 后才 `Deleted: sha256:93c2a5cc…`。

**实际收益**：Images 18.44 GB → **17.7 GB**，可回收 1.513 GB → **776.6 MB**（回收 ≈740 MB，与预估 736 MB 吻合）。

> ⚠️ 保留项：kind 节点内有 `hpa-example` 自己的 166 MB 副本（ID `4ca4c13a6d7cc`，与宿主那个不是同一镜像），
> 所以 `infra/k8s/hpa-demo/php-apache.yaml`（`imagePullPolicy: IfNotPresent`）**仍可直接 apply**，
> 宿主删掉不影响 HPA 演示。

### 4.2 三个 0.5.x 为什么在 13 分钟内连续出现（D11 迭代链）

| Tag | 时间 | 修了什么 |
| --- | --- | --- |
| 0.5.0 | 18:49 | PDB + Pod 反亲和（chart 0.5.0，生产可用性收口） |
| 0.5.1 | 18:58 | `main.py` 缺 `logging.basicConfig()` → `log.info` 被 root logger 吞 |
| 0.5.2 | 19:02 | `observability.py` 加 `blocked_instrumentation_scopes=["fastapi"]` → 探针 span 淹没业务 trace（200 条里 199 条是噪声） |

即「全链路复跑时边跑边发现 bug 边重建」，**不是重复构建，是正常调试链**。

## 5. 数据盘与宿主机磁盘

| 项 | 大小 | 备注 |
| --- | --- | --- |
| `docker_data.vhdx` | **33.9 GB** | WSL2 虚拟磁盘（只涨不缩） |
| Docker 内部实际 | ≈ 24.2 GB | 与 vhdx 差 ~10 GB = **未 TRIM 的已删数据** |
| C: 可用 | 42 GB | 82% 已用 |
| E: 可用 | 99 GB | 76% 已用 |

> ⚠️ vhdx 与内部用量差 ~10 GB 是 WSL2 ext4 不做 TRIM 所致（10-15 已踩坑：`diskpart compact` 只能回收 2G）。

## 6. 一句话结论

> 这套「K8s 生产化 + 观测 + 本地推理」的完整环境，**空载约占 1.6 GiB 内存 + 2% CPU，
> 磁盘约 24 GB（Docker 内部）／34 GB（vhdx）**。CPU 不是瓶颈，**内存和磁盘才是** ——
> 本地单机跑全套的代价主要花在「控制面 + 观测栈」的固定开销上，而不是业务负载。
>
> ⚠️ 10-16 补记：曾误以为「多版本镜像在浪费磁盘」并清掉 4 个旧 tag，
> 实测只回收 ≈0.5 MB（见 §4.1）。**同底座镜像堆叠几乎零成本**，
> 需要省空间时应盯 `hpa-example`（已清，回收 740 MB，见 §4.3）/ Build Cache /
> 观测栈，而不是业务镜像 tag 数。
