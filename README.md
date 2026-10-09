# LLMOps Engineer 作品集 · 资金/财务制度知识库智能问答与评估平台

[![CI](https://github.com/mantou2046/LLMOps-Engineer/actions/workflows/ci.yml/badge.svg)](https://github.com/mantou2046/LLMOps-Engineer/actions/workflows/ci.yml)

> 一个**可复现、可评估、可观测**的 LLM 应用，用来回答资金 / 财务制度类问题。
> 目标不是「能跑」，而是**能证明每次改动让系统变好还是变坏**。

- **仓库**：https://github.com/mantou2046/LLMOps-Engineer （公开）
- **学习计划与周复盘**：`E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\`

---

## 问题陈述（3 句话，待细化）

1. 资金 / 财务制度分散在多个文档里，人工查找慢且容易漏，问答类工具又常常「答得像那么回事但引错条款」。
2. 本项目做一个带**引用溯源**的知识库问答服务，并且**给它的每个回答建立可量化的评估基线**。
3. 关键不是把模型接上，而是搭出一套 **检索 → 评估 → 观测 → 成本** 的工程闭环，让质量变化可测量、回归可拦截。

> ⚠️ **数据脱敏铁律**：本仓库**只放自造的、同构的样例文档**。任何来自真实单位（制度、手册、表结构、账号、内网地址）的内容一律不入库。提交前必查。

---

## 目录结构

```
LLMOps-Engineer/
├── app/            # FastAPI 服务层（LLM 调用封装、结构化输出、重试、流式）
├── infra/          # 部署与基础设施（Docker / K8s / Helm / Terraform）
├── evals/          # 评估体系（golden set、RAGAS 四指标、CI 门禁脚本）
├── data/           # 样例数据（自造脱敏文档 + 切块产物）
├── notebooks/      # 探索性实验（chunk 策略对比、检索调优）
└── docs/
    ├── decisions.md   # 决策日志（每个关键选型写清「为什么」）
    └── env-setup.md   # 本机环境与已知坑
```

---

## 关键数字（README 必须写清的 5 个指标）

> 这些是面试官唯一会看的东西。每周更新一次，**必须能在本仓库里复现**。

| # | 指标 | 改动前 | 改动后 | 测量方式 |
| --- | --- | --- | --- | --- |
| 1 | faithfulness | — | — | RAGAS，`evals/` |
| 2 | context recall | — | — | RAGAS，`evals/` |
| 3 | P95 延迟 / TTFT | — | — | Grafana + 网关层埋点 |
| 4 | 每千次查询成本 | — | — | Langfuse + 云账单 |
| 5 | 语义缓存命中率 | — | — | LiteLLM 指标 |

---

## 镜像体积对比（W1 D2 产出）

> 面试常问「你怎么优化镜像」。答案不能是「用了多阶段构建」，而要有**实测数字**。
> 本节数字由 `bash scripts/verify_d2.sh` 现场测出，可复现。

**对比口径**：同一份代码，两个 Dockerfile —— `app/Dockerfile`（多阶段 + alpine + 非 root）vs `app/Dockerfile.naive`（单阶段 + 完整镜像 + root，**仅作反面教材**）。

| 镜像 | 基础镜像 | 构建方式 | 运行用户 | 体积 |
| --- | --- | --- | --- | --- |
| `llmops-api:naive` | `python:3.12`（完整版） | 单阶段，`COPY . .` | root | **1655.1 MB** |
| `llmops-api:0.1.0` | `python:3.12-alpine` | 多阶段，精确 COPY | `appuser` | **129.9 MB** |
| **减少** | | | | **92.2%（省 1525.2 MB）** |

> 基础层参考值（`docker image inspect` 实测）：`python:3.12-alpine` 79.1 MB / `python:3.12.7-slim-bookworm` 176.5 MB / `python:3.12` 完整版 ~900 MB。
> 最终镜像相对 alpine 基础层只净增 **50.7 MB**，即「venv 66.9 MB 去掉 pip/setuptools/wheel 之后」+ 应用代码。

**为什么最终选了 alpine 而不是 slim**（这是本轮优化里最有价值的一段）

一开始用 `python:3.12.7-slim-bookworm` 做基础层，优化到 **247 MB** 就压不动了 ——
因为 **slim 基础层本身就占 176.5 MB，物理上不可能满足「< 200MB」**。于是换 `python:3.12-alpine`：

| | slim-bookworm | alpine |
| --- | --- | --- |
| 基础层 | 176.5 MB | **79.1 MB** |
| 最终镜像 | 247 MB | **129.9 MB** |
| 是否达标 < 200MB | ❌ | ✅（余量 70 MB） |

换 alpine 的唯一风险是 **musl libc 而非 glibc** —— 带 C 扩展的包如果没有 musllinux wheel，
就得在镜像里装 `gcc` + `musl-dev` 现场编译，把省下的体积全吃回去。
**所以动手前先验证，而不是构建失败了再发现**：

```bash
# 在 alpine 里跑一次真实安装，不装任何编译器
docker run --rm python:3.12-alpine sh -c '
  pip install --no-cache-dir -r requirements.txt && echo WHEELS_OK'
```

结果全部命中预编译 wheel，构建日志可直接佐证：

```
uvloop-0.23.0-cp312-cp312-musllinux_1_2_x86_64.whl
watchfiles-1.3.0-cp310-abi3-musllinux_1_1_x86_64.whl
websockets-17.2-cp312-cp312-musllinux_1_2_x86_64.whl
```

⚠️ 因此 `app/Dockerfile` 的 builder 段**故意不装 gcc**。哪天某个依赖开始只发 sdist，
构建会立刻**报错**而不是悄悄变慢 —— 这是理想的失败方式。
⚠️ 同理 runtime 段**不装 curl**：alpine 自带 busybox 的 `wget`，HEALTHCHECK 用
`wget -q -T 3 --spider` 即可；为「让 curl 可用」去 `apk add curl` 会带回
`ca-certificates` 等约 4~8 MB，与体积目标背道而驰。

**跑法**

```bash
cp .env.example .env      # 填 POSTGRES_PASSWORD
bash scripts/verify_d2.sh # 一条命令：建两个镜像 + 比体积 + 验非 root + 验 pgvector
```

结束后善后（**保留数据卷**）：

```bash
docker compose --env-file .env -f infra/compose.yml down
```

> ⚠️ **手动敲 compose 命令时务必带 `--env-file .env`**（脚本内部已封装）。
> 否则 compose 找不到 `.env`，会报一长串
> `required variable POSTGRES_PASSWORD is missing a value` —— 看起来像配置坏了，
> 其实只是**环境文件没被加载**。`docker compose` 默认只从当前工作目录找 `.env`，
> 换个目录执行就失效。

### 多阶段省在哪（原理）

镜像体积来自**层**。单阶段把所有东西都堆在最终镜像里：

| 内容 | 单阶段 | 多阶段 |
| --- | --- | --- |
| 基础镜像 | `python:3.12` 完整版（含编译器、文档、apt 缓存） | `python:3.12-alpine`（79.1 MB） |
| 构建工具链 | `gcc` / `build-essential` 留在镜像里 | builder 段**根本不装**（依赖全是预编译 wheel） |
| pip 缓存 | 常在镜像里 | `--no-cache-dir` 不留 |
| pip / setuptools / wheel | 留在镜像里（约 15 MB） | 在 **builder 段**装完即删 |
| apk/apt 列表 | 常在镜像里 | 与安装同层 `rm -rf` |
| 源代码 | `COPY . .` 可能带 `.git` / `data/` | 精确 COPY + `.dockerignore` |

> ⚠️ **一个反直觉点**：清理动作（`rm -rf /var/lib/apt/lists/*`、删 pip）必须和「产生它的那条命令」写在**同一条 RUN** 里。
> 拆成两条 RUN 的话，删除只是在上一层之上盖一个**删除标记（whiteout）**，底下那层的数据**仍然在镜像里** —— overlayfs 是叠加的，不是覆盖。
> 这就是「层」这个概念的实际影响。

> 📌 **本项目真实踩坑（比上面的通用提醒更值得记）**：
> 优化到 270 MB 时，我把「删 pip / setuptools」写进了 **runtime 段**，心想「反正最后删掉就小了」。
> 结果镜像**涨到 284 MB** —— 因为 `COPY --from=builder /opt/venv` 那一层已经把 pip 拷进来了，
> 后面的 `RUN rm` 只加了一个删除标记，底层那 ~15 MB 依然在。
> 把它挪回 **builder 段**（装完就删，这样 COPY 过去的 venv 天生就是精简的）之后降到 247 MB。
> **「把删除放到后面」是错的直觉 —— 删除要放到「数据进入镜像之前」。**

### 层缓存顺序（BP4）

```dockerfile
COPY requirements.txt ./          # ① 依赖声明：低频变动
RUN pip install -r requirements.txt  # ② 装依赖：只有 ① 变了才重跑
COPY app/main.py ./               # ③ 源码：高频变动
```

反过来写（先 `COPY . .` 再装依赖）的话，**改一行代码就会让依赖层缓存全部失效**，每次构建都要重装几十个包。

---

## 快速开始

> 依赖与网络配置见 [`docs/env-setup.md`](docs/env-setup.md)（含代理端口、pip 源、HF 镜像的**实测结论**）。

```bash
cp .env.example .env          # 填入自己的密钥
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
```

### 容器化跑法（推荐把依赖关进容器）

> ⚠️ 本机原来没装 Docker，安装见 [`infra/DOCKER-SETUP-Win11.md`](infra/DOCKER-SETUP-Win11.md)（含实测下载地址与代理配置）。

```bash
cp .env.example .env                          # 至少填 POSTGRES_PASSWORD

# 构建多阶段镜像
docker build -f app/Dockerfile -t llmops-api:0.1.0 .

# 起 Postgres + pgvector
docker compose -f infra/compose.yml up -d
docker compose -f infra/compose.yml ps        # 等 postgres (healthy)

# 一条命令跑完 W1 D2 的三条验收
bash scripts/verify_d2.sh
```

| 验收项 | 标准 |
| --- | --- |
| 镜像体积 | 多阶段 < 200MB，且显著小于朴素单阶段 |
| 非 root | 容器内 `whoami` ≠ `root` |
| pgvector | 能写入向量并完成最近邻检索 |

---

## 本地 K8s 跑法（W1 D3 / D4 / D5 产出）

> 用 [kind](https://kind.sigs.k8s.io/) 在 Docker 里起一个单节点集群。
> **为什么选 kind 而不是 minikube**：见 [`docs/decisions.md`](docs/decisions.md) D6。

```bash
# 一条命令跑完 D3 的三条验收（建集群 → load 镜像 → apply → 校验 → 自愈演示）
bash scripts/verify_d3.sh

# 一条命令跑完 D4 的三条验收（配置外置 → 持久化 → Ingress）
bash scripts/verify_d4.sh

# 一条命令跑完 D5 的验收（三种探针 + 三种排错演练）
bash scripts/verify_d5.sh
```

| 验收项（D3） | 标准 | 实测 |
| --- | --- | --- |
| Pod 全部 Running | `kubectl get pods` | ✅ 2/2，**13 项全绿** |
| 服务能访问 | `port-forward` + `/health` 返回 `status:ok` | ✅ |
| Service Endpoints | 非空（证明 selector 与 labels 匹配上） | ✅ 2 个 endpoint |

| 验收项（D4） | 标准 | 实测 |
| --- | --- | --- |
| 配置外置 | ConfigMap + Secret 容器内可读 | ✅ **19 项全绿** |
| ⭐ **数据持久化** | 删 Pod 重建后数据还在 | ✅ `INSERT` → `delete pod` → 重建 → `SELECT` count=1 |
| **Ingress** | 宿主 80 端口能按 Host 路由到服务 | ✅ `Host: llmops.local` → **200**；错误 Host → **404**（负向验证） |

| 验收项（D5） | 标准 | 实测 |
| --- | --- | --- |
| 三种探针配置 | startup / liveness / readiness 路径与参数正确 | ✅ **29 项全绿** |
| ⭐ **startupProbe 有效性** | 同样 45s 慢启动，有 startup 应零重启 | ✅ 反面（无 startup）**重启 4~5 次起不来** vs 正面 **0 次 `1/1 Running`** |
| ⭐ **readiness 摘流量** | 失败 → 摘出 EndpointSlice 但**不重启** | ✅ `0/1 Running` + 端点数 **2→1→2** + **RESTARTS=0** |
| 排错判据 | 三种故障各有可断言的特征 | ✅ `ErrImagePull` / `OOMKilled`+**137** / `Running` 但 `READY=0/1` |

### 三种探针怎么配的（D5）

**参数按「失败代价」推算，不是照抄**：

| 探针 | 路径 | period × threshold | 容忍窗口 | 失败后果 |
| --- | --- | --- | --- | --- |
| `startupProbe` | `/health` | 2s × 30 | **60s** | 杀容器（仅启动期） |
| `livenessProbe` | `/health` | 10s × 3 | **30s** | **重启容器** |
| `readinessProbe` | `/ready` | 3s × 2 | **6s** | **摘出 EndpointSlice**（不重启） |

**核心权衡**：readiness 的误判代价只是「短暂少一个副本接流量」→ 可以激进（6s）；
liveness 的误判代价是「重启容器 + 可能进 CrashLoop 恶性循环」→ 必须保守（30s）。
⚠️ **若 startup 的窗口 ≤ liveness 的窗口，startup 形同虚设** —— `verify_d5.sh` 里有断言检查这个不等式。

**为什么 liveness 打 `/health` 而 readiness 打 `/ready`**：liveness 若查数据库，
数据库抖动会导致容器被反复重启，而**重启解决不了数据库问题** —— 把「外部依赖故障」
放大成「服务全面不可用」。外部依赖的检查属于 readiness（此时摘流量是正确的）。

**两个「默认关闭」的演示旋钮**（`app/main.py`，D13）：

```bash
# ① 模拟慢启动（默认 0 = 行为不变），用于演示 startupProbe
kubectl set env deployment/llmops-api STARTUP_DELAY_SECONDS=45

# ② 注入「就绪失败」（默认不激活），演示摘流量但不重启
kubectl exec deploy/llmops-api -- touch /tmp/not-ready
kubectl get pods                      # → READY 0/1，STATUS 仍是 Running
kubectl exec deploy/llmops-api -- rm -f /tmp/not-ready
```

⚠️ **`READY 0/1` + `STATUS Running` 是最易误判的一种故障**：容器没崩、`RESTARTS=0`、
`logs` 也正常，但服务就是不接流量。定位靠 `kubectl get endpoints`（端点变少/为空）
或 `describe pod` 里的 `Readiness probe failed`。

📖 探针原理与参数推算详见 [`docs/decisions.md`](docs/decisions.md) D12–D14。

### 清单文件

```
infra/k8s/
├── app.yaml                 # Deployment(2 副本，三探针) + Service(ClusterIP)  [D3/D5]
├── config.yaml              # ConfigMap llmops-config + Secret llmops-secrets  [D4]
├── postgres.yaml            # headless Service + StatefulSet + initdb ConfigMap [D4]
├── ingress.yaml             # Ingress 规则（host: llmops.local）              [D4]
├── kind-ingress-config.yaml # 带 extraPortMappings 的集群配置                 [D4]
└── ingress-nginx/
    └── deploy-upstream.yaml # ingress-nginx 控制器清单（已打 2 处补丁，见下）   [D4]
```

### Ingress 一键安装（⚠️ 会删库重建集群）

```bash
bash scripts/setup_ingress_kind.sh            # 有二次确认
bash scripts/setup_ingress_kind.sh --yes      # 跳过确认
```

`extraPortMappings` 只在**建集群时**生效，所以装 Ingress 必须 `kind delete cluster` + 重建。
脚本会先让你确认，并处理下面三个坑（全部实测踩过）：

1. **清单下载** —— 沙箱 env 代理拦 `raw.githubusercontent.com` → 走注册表代理 `127.0.0.1:9527`，
   清单**落盘进仓库**（`infra/k8s/ingress-nginx/deploy-upstream.yaml`）。
2. **清单两处补丁** —— Service `LoadBalancer`→`NodePort`(30080/30443)；**去掉 `@sha256:` digest 钉死**。
3. **节点 containerd 代理** —— kind 把宿主 `HTTP(S)_PROXY` 注入节点，而节点内部的
   `127.0.0.1` 指向节点自己 → 任何回源都 `connection refused`。脚本会加 systemd drop-in
   清掉它（`Environment=` 清空 + `NO_PROXY=*` 兜底）。

### ⚠️ kind 的三个经典坑（都实测踩过）

1. **镜像必须显式 load**。kind 的「节点」是**独立容器**，看不到宿主机的 `docker images`。
   不 load 就一定拉不到 —— 而且如果镜像用 `latest` + `imagePullPolicy: Always`，
   kubelet 还会**绕过本地镜像**去 Docker Hub 拉一个不存在的 tag → 直接 `ImagePullBackOff`，
   而镜像明明就在节点里。**所以用具体 tag + `IfNotPresent`**（见 `docs/decisions.md` D7）。

2. **multi-arch 镜像载入失败**。`kind load docker-image pgvector/pgvector:pg16` 会报
   `ctr: content digest ...: not found`。绕法：
   ```bash
   docker save pgvector/pgvector:pg16 -o pgv.tar
   docker cp "E:\Projects\LLMOps-Engineer\pgv.tar" llmops-control-plane:/pgv.tar
   docker exec llmops-control-plane ctr -n k8s.io images import \
       --platform linux/amd64 /pgv.tar
   ```
   完整归因（代理泄漏 + 外平台层）见 `docs/decisions.md` D9。

3. **digest 引用 + 节点代理 = 永远拉不到**。清单里写 `image: xxx@sha256:...` 时，
   kubelet 会拿 **digest 去远端校验 manifest**（不查本地），而节点代理指向 `127.0.0.1`
   → `ErrImagePull`，**且 `imagePullPolicy` 设什么都没用**。修法见 D11
   （去 digest + 清节点 containerd 代理）。

### 清理

```bash
kind delete cluster --name llmops
```

---

## ⚠️ Docker 数据盘已迁到 E 盘（2026-10-09 完成）

**关键认知：项目源码在 E 盘 ≠ 数据在 E 盘。**

`E:\Projects\LLMOps-Engineer` 本身只占 **618 KB**（纯源码）。但 kind 的「节点」
本质是 **Docker 容器** —— 它的文件系统、每个镜像层、postgres 的 PVC 数据卷，
**全部写在 Docker 的数据盘里**。

| | 位置 | 状态 |
| --- | --- | --- |
| 迁移前 | `C:\Users\lile2\AppData\Local\Docker\wsl\disk\docker_data.vhdx` | 11.5 GB，C 盘只剩 37G |
| **迁移后** | **`E:\Projects\DockerData\DockerDesktopWSL\`** | **11.55 GB，C 盘回到 48G** |

📄 **迁移步骤见 [`docs/docker-data-move-to-E.md`](docs/docker-data-move-to-E.md)**
（目标位置 **`E:\Projects\DockerData`** —— 与代码仓库平级，见下方「为什么」）。

⚠️ `docker rmi` **不会让 vhdx 变小** —— 必须迁移或压缩才能真回收。
💡 **迁移后 kind 集群原样恢复，无需重建**（实测）；`verify_d4.sh` 19 项全绿，postgres 数据完好。
✅ C 盘旧 vhdx 由 Docker Desktop **自动清理**（`AppData\Local\Docker` 12 GB → 9.1 MB）。

**为什么数据盘不放在本项目目录里**：Docker 数据盘是**全局的**，装着所有项目共用的
镜像 / 容器 / 卷，不属于任何单个仓库。放进去会让一个 618 KB 的代码仓库看起来有 12 GB，
备份和同步时也会连带搬运。所以 `E:\Projects\` 下区分两类目录 ——
代码仓库（进 Git、常备份）与大数据目录（`DockerData` / `AIModel` / `FileStorages`，平级而非嵌套）。

---

## 启用提交前脱敏检查（克隆后必做一次）

本仓库是**公开**的，所以有一道提交前门禁，防止手滑把凭据或内部信息推上去。

```bash
git config core.hooksPath .githooks
cp scripts/sensitive_terms.example.txt scripts/sensitive_terms.txt   # 填自己的敏感词
python scripts/check_sensitive.py --all                              # 手动全树扫一遍
```

检查内容分三层：

| 层 | 内容 | 触发时机 |
| --- | --- | --- |
| 内置规则 | 私钥、各类 API Key / Token、JWT、**内网 IP 段**、带口令的连接串、疑似凭据键值 | — |
| 自定义词表 | `scripts/sensitive_terms.txt` 里的单位名 / 内网域名 / 真实表名字段名 | — |
| 本地钩子 | `.githooks/pre-commit` 扫**暂存区**，命中即阻止提交 | `git commit` |
| CI 兜底 | `.github/workflows/ci.yml` 扫**全树**，防止本地没装钩子的人绕过 | push 到 main / PR |

> ⚠️ 脚本**只报「文件:行号 + 命中类型」，绝不回显命中的原文** —— 否则检查工具本身就成了泄露渠道。
> ⚠️ `scripts/sensitive_terms.txt` 被 gitignore，**真实词表只留本地**。
> ✅ 两道门禁均已实测生效（2026-10-07）：本地钩子在提交时扫描 7 个暂存文件；CI 在首次 push 时扫描全树 14 个文件并通过。

---

## 进度

对照学习计划：`E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\`

| 周 | 日期 | 主题 | 状态 |
| --- | --- | --- | --- |
| W1 | 10/7 – 10/11 | 起手与环境、Docker、K8s 起手 | 进行中（D1–D4 已完成，D5 待做） |
| W2 | 10/12 – 10/18 | K8s 生产化、上云、服务化 + 观测 | 未开始 |
| W3 | 10/19 – 10/25 | 实验追踪与模型管理 | 未开始 |
| W4 | 10/26 – 11/1 | 编排、部署与网关 | 未开始 |
| W5 | 11/2 – 11/8 | 监控、工程规范与 IaC | 未开始 |
| W6 | 11/9 – 11/15 | RAG 与评估（进度落后时优先保） | 未开始 |
| W7 | 11/16 – 11/22 | 护栏、成本、微调与 Agent | 未开始 |
| W8 | 11/23 – 11/30 | 作品集打包与求职启动 | 未开始 |
