# LLMOps Engineer 作品集 · 资金/财务制度知识库智能问答与评估平台

[![CI](https://github.com/mantou2046/LLMOps-Engineer/actions/workflows/ci.yml/badge.svg)](https://github.com/mantou2046/LLMOps-Engineer/actions/workflows/ci.yml)

> 一个**可复现、可评估、可观测**的 LLM 应用，用来回答资金 / 财务制度类问题。
> 目标不是「能跑」，而是**能证明每次改动让系统变好还是变坏**。

- **仓库**：https://github.com/mantou2046/LLMOps-Engineer （公开）
- **学习计划与周复盘**：`E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\`

---

## 1 · 问题

资金 / 财务制度分散在多个文档里，人工查找慢且容易漏，问答类工具又常常「答得像那么回事但引错条款」。本项目做一个带**引用溯源**的知识库问答服务，并且**给它的每个回答建立可量化的评估基线**。重点是搭出一套 **检索 → 评估 → 观测 → 成本** 的工程闭环，让质量变化可测量、回归可拦截。

系统跑在单机、无 GPU（Graphics Processing Unit，图形处理器）的本地 `kind` 集群上，是工程样本，不是生产服务。样例文档全部自造、同构，不含任何真实单位的数据。

## 2 · 方案

请求从进来到出去走五个环节，每个环节都有对应的评估或观测落点。

| 环节 | 做什么 | 代码 / 报告 |
| --- | --- | --- |
| 输入护栏 | 规则基线挡注入，模型判定补规则漏掉的措辞 | `app/input_guard.py`、`evals/ACCEPTANCE_W7.md` |
| 检索 | 递归切块 400/50，向量 + BM25（Best Matching 25，词频相关性排序）两路，RRF（Reciprocal Rank Fusion，倒数排序融合）取 top-5 | `scripts/w6d2_hybrid_bench.py`、`app/` |
| 分流与缓存 | 规则判档选便宜档 / 主力档，精确缓存复用上次答案，重试只留网关一层 | `app/routing.py`、`app/cost.py`、`infra/litellm/` |
| 输出护栏 | 字段白名单、格式错最多修一次、固定兜底、个人信息遮盖 | `app/output_guard.py`、`evals/OUTPUT_GUARD_BENCH.md` |
| 评估与观测 | 50 题 golden set + RAGAS（Retrieval-Augmented Generation Assessment，检索增强生成评估）四指标 + CI（Continuous Integration，持续集成）里的非 LLM 门禁；指标、trace、成本各自留证 | `evals/`、`infra/grafana/`、`docs/ARCHITECTURE.md` |

架构图按 C4 只画上下文层与容器层，四条数据流（检索 / 评估 / 观测 / 成本）与 8 条决策标注见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；54 条选型的「为什么不是另一个」见 [`docs/DECISION_INDEX.md`](docs/DECISION_INDEX.md) 与 [`docs/decisions.md`](docs/decisions.md)。

## 3 · 取舍理由

几条最容易被问到的判断，都记了不选另一个做法的理由。

| 决定 | 没选什么 | 依据 |
| --- | --- | --- |
| 混合检索先上 RRF，验收看 hit@1 / MRR | 单路向量、只看 hit@5 | 单路向量 hit@1 只有 7/10；hit@5 太松，看不出融合价值（DEC-040） |
| RRF 已满分，rerank 默认不开 | 默认挂精排模型 | top-5 精排净增 2.2–2.4 s，换不到 hit@1 提升（DEC-042） |
| 先量噪声再谈提升，改动无效就不并入 | 只报正向数字 | W6 单变量归因结论是**证伪**：配对 Δ +0.0046 小于噪声标尺 +0.0074，faithfulness 反而降 0.1214（DEC-045、`evals/ATTRIBUTION.md`） |
| CI 门禁只用非 LLM 指标 | 把全量 RAGAS 塞进 CI | 上游限流会让 CI 红成与代码无关的假红，全量评估留本地周跑（DEC-046） |
| 精确缓存 + 规则分流，语义缓存暂缓 | 语义相似缓存 | 同条件四组只改这两件事拿到 −64.3%；语义缓存的三个前置本机仍缺（DEC-050） |
| 先 RAG 后微调，微调只当候选 | 直接上微调解决答不准 | 微调 100% 只证明格式行为可学，不等于业务效果（DEC-051、`evals/LORA_DEMO.md`） |

## 4 · 5 个数字

每个数字都标了报告路径与小节，可直接在仓库里核。口径提醒一并写出，别把演示单位当成真钱、把格式证据当成业务收益。

| # | 数字 | 出处（路径 + 小节） | 口径提醒 |
| --- | --- | --- | --- |
| 1 | 每千次成本点数 **1754.6 → 626.5（−64.3%）** | `evals/COST_BENCH.md` §2 四组对照表、D 行 | 成本点数是演示单位，不是真钱；30% 重复率是刻意设定 |
| 2 | 混合检索 **hit@1 7/10 → 10/10**、MRR **0.825 → 1.0** | `docs/ARCHITECTURE.md` 四条数据流表「融合」行；DEC-040 | 看严判据，别写成「融合全面更高」 |
| 3 | 50 题 RAGAS 四项 **0.9434 / 0.4768 / 0.8113 / 0.8591** | `evals/RAGAS_BASELINE.md` §全量复跑、汇总表 | CR（context recall）/ CP（context precision）绝对值依赖 `reference`；12 道拒答题 AR（answer relevancy）仅 0.0524，混进均值会掩盖差异 |
| 4 | 输出护栏 30 条量化：误拦 **0/8**、漏拦 **0**、单次判定 P50 **1.849 s** | `evals/OUTPUT_GUARD_BENCH.md` 量化评估表 | 判定器默认关闭；真实流量下的误拦率未量 |
| 5 | 决策对照表 **12/12 一致、证据覆盖 100%** | `docs/W7_D5_DECISION.md` §4 | 12 条场景是演示规模 |

三个别写错的口径：微调 100% 只证明格式行为可学、W6 归因结论是证伪、成本点数是演示单位。

## 5 · 如何复现

下面这些前置条件都要写在明处，有一步要靠「你脑子里记着」就算不合格。

| 前置 | 本机实测结论 |
| --- | --- |
| Python | 3.13 / 3.14 均可；换版本会让 pip 源结论反转，见 [`docs/env-setup.md`](docs/env-setup.md) §2 |
| 代理 | 环境变量里的端口每次会话可能变且可能是死端口，注册表那个（本机 9527）才稳。`git push` 要把 `http_proxy` / `https_proxy` / `HTTP_PROXY` / `HTTPS_PROXY` 四条都显式指过去 |
| 容器健康检查 | 起完依赖要等 `postgres` 变 `healthy` 再跑验收，否则连不上 |
| 宿主 / 容器地址 | 容器内连宿主用 `host.docker.internal`；本机服务一律写 `127.0.0.1`，写 `localhost` 会先解析 `::1` 而变慢 |
| mock 与真实上游 | `FAKE_UPSTREAM_BASE_URL` 与 `upstream_mode` 分开配；mock 下拒绝冒充并走兜底 |
| 端口占用 | 5432 / 6381 / 3001 / 19095 / 11434 等先查监听，别和已有容器撞 |
| 凭据 | `.env` 自己建、不提交；仓库只放自造样例，提交前过 `check_sensitive.py --staged` |

```bash
cd /e/Projects                    # 换成你的工作目录绝对路径
git clone https://github.com/mantou2046/LLMOps-Engineer.git
cd /e/Projects/LLMOps-Engineer    # 换成你的克隆目录绝对路径
cp .env.example .env               # 填自己的值
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
git config core.hooksPath .githooks
docker build -f app/Dockerfile -t llmops-api:0.1.0 .
docker compose -f infra/compose.yml up -d
docker compose -f infra/compose.yml ps        # 等 postgres (healthy)
pytest -q                                    # 179 项
bash scripts/verify_d2.sh
python scripts/check_sensitive.py --staged   # 提交前必跑
```

数字复核：

```bash
cd /e/Projects/LLMOps-Engineer
grep -nE '1754\.6|626\.5' evals/COST_BENCH.md
grep -nE '10/10|0\.825' docs/ARCHITECTURE.md
grep -nE '0\.9434|0\.4768' evals/RAGAS_BASELINE.md
grep -nE '1\.849|0/8' evals/OUTPUT_GUARD_BENCH.md
grep -nE '12/12' docs/W7_D5_DECISION.md
```

验收方法是换一个没看过项目的人、换一个干净目录，只给这份 README、不给口头补充。任何一步需要回来问，就是这里缺了前置条件；再让他回述第 1 段的问题和第 3 段的一处取舍，并逐条核上面 5 个数字能否在报告里找到同样的值。

> ⚠️ **数据脱敏铁律**：本仓库**只放自造的、同构的样例文档**。任何来自真实单位（制度、手册、表结构、账号、内网地址）的内容一律不入库。提交前必查。

---

以下为 W1–W7 的逐周明细与进度表，按周保留，不删。

## 目录结构

```
LLMOps-Engineer/
├── app/            # FastAPI 服务层（LLM 调用封装、结构化输出、重试、流式）
├── infra/          # 部署与基础设施（Docker / K8s / Helm / Terraform）
├── evals/          # 评估体系（golden set、RAGAS 四指标、CI 门禁脚本）
├── data/           # 样例数据（自造脱敏文档 + 切块产物）
├── notebooks/      # 探索性实验（chunk 策略对比、检索调优）
├── observatory/    # ⭐ 本地 K8s 观测台（可视化 + 可交互，见下）
└── docs/
    ├── ARCHITECTURE.md    # ⭐ 架构图终稿（C4 上下文层 + 容器层 + 四条数据流 + 决策标注）
    ├── DECISION_INDEX.md  # ⭐ 技术决策清单（54 条：选型 / 为什么不是另一个 / 证据 / 复核条件）
    ├── decisions.md       # 决策日志原文（每个关键选型写清「为什么」）
    └── env-setup.md       # 本机环境与已知坑
```

### K8s 观测台（`observatory/`）

一个连着你本地 kind 集群的网页控制台：**Pod 卡片墙实时刷新 +
7 个按钮直接操作真实集群**（删 Pod 看自愈、改坏探针看摘流量、
慢启动看 startupProbe、触发 OOM 看退出码 137……）。

```bash
# 双击 observatory/start.bat，或：
python observatory/server.py      # 然后打开 http://127.0.0.1:8899
```

**为什么要做它**：W1 的知识点（三种探针、自愈、就绪）如果只用
`verify_*.sh` 的「29 项全绿」来交付，你看不到任何东西，也无从判断
真假。这个工具把证据来源换成**集群自身的行为** —— 屏幕上卡片
从绿变红、RESTARTS 从 0 跳到 3，都是真实 kubectl 操作的结果，
你随时可以另开终端 `kubectl get pods -w` 交叉验证。

详见 [`observatory/README.md`](observatory/README.md)。

---

## 关键数字（W1 时点占位，已由文首第 4 段取代）

> W1 建仓时留的五个空槽（faithfulness / context recall / P95 / 成本 / 缓存命中率），当时还没有实测值。W8 D2 重写 README 后，5 个数字的实测值、报告出处与口径提醒统一写在文首「4 · 5 个数字」，不再分两处维护。本节保留标题说明沿革，避免读者把空槽误读成未完成项。

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

**不想敲命令看这些现象？用观测台（`observatory/`）：**

```bash
python observatory/server.py     # 打开 http://127.0.0.1:8899
```

上面那两个旋钮、以及「删 Pod 看自愈」「改坏探针对照 readness vs liveness」
「触发 OOM 看 137」都做成了按钮。点一下，**卡片当场变红/变绿**，
右侧同时显示执行了哪条 kubectl —— 想核实就照着敲一遍。
详见 [`observatory/README.md`](observatory/README.md)。

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

## 架构图（v4 定稿 · C4 两层）

> 终稿按 C4 model 只画前两层：**上下文层**（跟谁打交道）+ **容器层**（有哪些可独立部署的部分）。完整图、四条数据流（检索 / 评估 / 观测 / 成本）与 8 个决策标注见 **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**；每个选型的「为什么不是另一个 + 证据 + 复核条件」见 **[`docs/DECISION_INDEX.md`](docs/DECISION_INDEX.md)**。
>
> 静态版大图（不依赖 Mermaid 渲染，适合离线 / 简历附件）：**[`docs/images/llmops-architecture-v4.1.svg`](docs/images/llmops-architecture-v4.1.svg)** —— 与下方 Mermaid 同源，含 L1 / L2 两层、27 条走线与边界说明。**8 条决策标注不在图上重复**，编号 D1–D8 见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §2 表格（图上的 ①–⑥ 只是容器分区）。

![LLMOps 作品集架构图 v4.1](docs/images/llmops-architecture-v4.1.svg)

```mermaid
flowchart LR
  Q["提问者"] -->|"问题"| ING["ingress-nginx<br/>3 条 Host"]
  ING --> APP["应用 llmops-chart<br/>输入护栏 → 检索 → 分流 → 输出护栏"]
  APP -->|"检索 top-5"| PG[("postgres + pgvector<br/>330 块 · probes=3")]
  APP -->|"model=auto"| GW["LiteLLM :4001<br/>primary / cheap / backup"]
  GW --> CACHE[("redis 精确缓存<br/>TTL 600")]
  GW -->|"fallback 链"| UP["上游模型<br/>网关 / 本地 Ollama"]
  APP -.->|"指标"| OBS["Prometheus → Grafana<br/>8 块看板"]
  APP -.->|"trace + prompt 版本"| LF["Langfuse"]
  APP -.->|"实验汇总"| ML["MLflow<br/>实验 + alias registry"]
```

> **一句话**：把人问的一句话，变成一条**有引用、有档位、有成本、可回查**的回答；顺手记下「哪一步变慢 / 变贵 / 变错」。
>
> **为什么只画两层**：多一层就多一次解释成本。十二层回答「都做了什么」，两层回答「为什么这么搭」——后者才是选型能力的证据。层数从 v3 的十二层收到两层不是退步。

### 十二层读数（v3 · W4 收口，保留作逐层明细）

![LLMOps 作品集架构图 v3](docs/images/llmops-architecture-v3.svg)

> 十二层，分三段：**①–⑥ 运行基座**（入口 / 应用 / 配置 / 数据 / kind）· **⑦–⑨ 版本与可解释**（MLflow 实验追踪 / Langfuse 观测随行 / git→registry 发布指针）· **⑩–⑫ 编排与流量**（Prefect 调度 / LiteLLM 网关 / 应用侧规则分流与批量并发）。
>
> W4 的核心设计是**每层只管一件事**：编排器管调度、应用管分档、网关管模型与缓存——配置跟着层走，不跟着人走。所以网关别名背后的模型当天换了三次，**应用代码零改动**。
>
> 关键证据（可在本仓库复现）：flow 79.51 s（`train_model` 占 98.3%）· 判档 4.8–6.0 µs/次 · 故障转移 200 vs 去 fallback 的 5xx · 缓存 1161.6 → 10.3 → 8.9 ms · 并发 8× vs 串行 7.7×（token 一位不变）。

---

## 进度

> **最后更新：2026-11-12**（每次推进后同步本表，别让它变成装饰品）

对照学习计划：`E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\`（八周 10/7 → 11/30）

| 周 | 日期 | 主题 | 状态 | 关键产出 |
| --- | --- | --- | --- | --- |
| W1 | 10/7 – 10/11 | 起手与环境、Docker、K8s 起手 | ✅ **完成**（D1–D5） | 镜像 1655→129.9 MB（−92.2%）；kind 集群 + 三探针 29 项验收全绿；`observatory/` 观测台 |
| W2 | 10/12 – 10/18 | K8s 生产化、上云、服务化 + 观测 | ✅ **完成**（D6–D12） | Helm 发布、RBAC 最小权限、PDB + 反亲和、dev/prod 隔离、指数退避重试、Langfuse trace；**阶段一验收 10/18 达成** |
| W3 | 10/19 – 10/25 | 实验追踪与模型管理 | ✅ **完成**（D13–D19） | MLflow 四条 run（prompt v0/v1 对比，`refusal_rate` 0.0→0.2 两组复现、`format_ok` 恒 1.0）；模型按 alias 注册；prompt 版本化（git 真源）；MLflow 独立 chart 上 K8s；DEC-022~028 |
| W4 | 10/26 – 11/1 | 编排、部署与网关 | ✅ **完成**（D1–D7，**领先约 3 天**） | Prefect 第一个 flow + RAG 索引重建流水线（增量 + task 级重试，flow 79.51 s / train 占 98.3%）；部署三形态实测（在线 p50 15 ms / 离线 7.4 万行 0.64 s / 不做流式）；离线批量打分 `evals/batch_score.py`（8 并发 1.05 s vs 串行 8.08 s ≈ 7.7×，token 成本四组一位不变）；LiteLLM 网关三别名 + 故障转移（200 vs 去掉 fallbacks 的 5xx，缓存 1161.6→10.3→8.9 ms）；**应用侧规则分流器 `app/routing.py`**（判档 4.8–6.0 µs/次、离线 24 条 12/12 与标注 0 条不符、在线 18 条分流全对且缓存 6/6 命中）；**M3/M4 作业 12 题全答**；DEC-029~033 |
| W5 | 11/2 – 11/8 | 监控、工程规范与 IaC | ✅ **完成**（11/4 收口，提前 4 天；D1–D6 全绿） | **D1** Prometheus 指标采集打通：`app/metrics.py` 一条中间件装出「请求数 / 耗时 / 在飞数」（**错误数不单独做 counter**）+ `/metrics` 端点；Prometheus v3.15.1 容器抓取（5 s 间隔，单次 8.5 ms / 106 样本 / 10 KB）；五条 PromQL 验通（错误率 **0.2308 = 6/26**）；基数实验 30 个不同 404 路径仍只 **1 条**时序、1000 个 label 值 → **116 KB / 14 ms**；**两个失真实测**：流式请求客户端 1.641 s 而指标只记 0.424 s（只到 TTFB）、双 worker 连抓 6 次读到 5/5/5/7/7/5。**D2** Grafana 13.2.5 看板 + Evidently 0.7.23 漂移报告：`infra/grafana/` 三件套 provisioning 入版本库（datasource uid 写死 + 看板 JSON + `allowUiUpdates: false`），8 块面板（4 数字 + 4 曲线）；QPS 0.212→0.929（**4.4×**）、峰值 2.182、P95 `/chat` **4.75 ms**、抓取 6.05 ms；漂移三场景实测（control 2/6 列漂移 p=7.78e-04、coldstart p=2.81e-15、injected 3/6 = **50.0%**）；DEC-034 定「**看板只画能回答问题的图**」。**D3** LLM 专属指标：网关开 `callbacks: ["prometheus"]`（不开 `/metrics` 404）+ 关指标鉴权（默认 401，鉴权路径要 prisma/DB）+ 三个 `local-*` 档位与 `model_info` 显式定价；Prometheus 加 `litellm-gateway` job（`metrics_path` 须带斜杠）；可控假上游 `scripts/fake_upstream.py`（首包 300ms / usage 写死 100/50）+ `scripts/gateway_load.py` 五组流量 → **17 请求 = 12 次真实上游调用 + 2 次缓存命中 + 3 次打不通**（与上游 `/stats` 独立对账）；单次成本 cheap **2e-5** vs primary **2e-4**（正好 10 倍）、P95 TTFT **0.4875 s**（真实约 0.33 s，桶宽插值）、命中率 **11.6%**；**三个「数字会骗人」**：`cache_misses` 只对流式记（教科书公式算成 50%）、P95 被桶宽放大 84%、上游失败 20% 而错误率面板 **0%**（被重试吃掉）；看板 `llmops-llm.json` **10 块** |
| W6 | 11/9 – 11/15 | RAG 与评估（进度落后时优先保） | ✅ **完成**（11/5 开工、11/8 收口；周验收四条落地，D3 按双指标口径验收通过，DEC-047） | **D1** 三种切法 5 配置实跑选递归 B（400/50）：512 硬切块尾完整率仅 **7.2%**、按标点切到 **42.8%**；语料冻结到 `evals/corpus/`（commit `6ba1baf`，104,798 字符）；`rag_chunks` 从 29 行 `vector(384)` fake 迁成 **330 行 `vector(512)` 真向量**，六条灌库复验清单全勾——精确检索 hit@5 **10/10**、hit@1 7/10、MRR 0.825 与离线基线逐项对上；**ANN 坑实测**：IVFFlat 默认 `probes=1` 三次掉成 9/10、9/10、7/10（漏题随索引重建漂移），`probes=3` 三次稳定 10/10。**D2** BM25 + RRF 融合：hit@1 向量 7/10 → **RRF 10/10**、MRR 0.825 → **1.0**，生产库三路复验逐项对上；接 `bge-reranker-base` 量延迟，向量查询约 4ms、精排 top-5 净增 **2.2–2.4s**（约 530–620 倍）→ 默认不开，关键词判据下掉分属尺子错配，等 golden set 重评。**D3** 50 题 golden set 标完（`--strict` 0，答案逐字可核 23 / atoms 兜底 15），网页版点选答题的候选句全部来自冻结语料原文、人选的句子才成为标准答案；三个 AI 代理三轮独立复标未达旧线 0.8，**2026-11-09 用户拍板改判定口径，按预留双指标（平均 Jaccard 0.816 / 严格一致 70.2%）验收通过**（`evals/ACCEPTANCE.md` §口径变更与验收结论、DEC-047），⚠️ 不是原线达标、也不是真人双标。**D4** 接最小 RAG 链路（递归 B + RRF top-5 → 网关生成），用带 `reference` 的正式口径跑 RAGAS 四项，首份基线报告 `evals/RAGAS_BASELINE.md`；10 题小样四项基线 **0.9714 / 0.5817 / 0.9200 / 0.8988**（末项 9/10，`g006` 内部超时）；裁判从 AMD 那条 `deepseek-v4-flash`（429 冷却 65–86 秒 + 一次 504）换成网关 `auto` 路由（压测 15/15、平均 1.58 秒、不走 AMD）。`deepseek-v4.1-flash` 在 catalog 里 `no_key` 用不了。**D5** 单变量 A/B 归因（只改生成 prompt 第 3 条输出格式）：answer relevancy 配对 Δ **+0.0046**，小于理论不该变的 context recall 噪声标尺 **+0.0074**，逐题 5 涨 5 跌、σ 0.3718；faithfulness 反从 0.9214 掉到 **0.8000**（−0.1214，`g002` 被误判成拒答）→ 判定改动无效，不并入生产链路；报告 `evals/ATTRIBUTION.md`、脚本 `scripts/w6d5_attribution.py`。**D6** 先补 `source_chunk` 回填：按递归 B 切法把 38 道可答题的标准答案逐字核进 330 块（与 D1 逐字对账），12 道拒答题保持 `null`，`--strict` 退出 0、diff 只有一列；再上**零依赖非 LLM 门禁** `eval-gate` job：20 题确定性分层抽样子集、字符 bigram BM25、`IDBasedContextRecall@5` **0.75**（全量 38 题 **0.8421**）、阈值 **0.65**，改坏 query 模板塌到 **0.0** 且退出 1，`--self-test` + 3 条单测把「改坏 prompt 被 CI 拦下」自动化；`ruff` 全绿、`pytest` 29 passed。报告 `evals/SOURCE_CHUNK_BACKFILL.md` / `evals/CI_EVAL_GATE.md`。**D4 全量补跑（11-08，固定裁判 `glm-5.3-flash` 直连官方 `/v1`）**：50 题四项 50/50 全齐、NaN 0，均值 **0.9434 / 0.4768 / 0.8113 / 0.8591**；分组报分补齐，12 道拒答题 AR 仅 **0.0524**、CP **1.0000**，可答题 AR 0.6108，混进全量均值会掩盖差异；`g006` 与 `g042` 的缺分都补回。见 `evals/RAGAS_BASELINE.md` §全量复跑。**周复盘（11-08 提前写完）**：对比数字与三问成稿 [`docs/W6_WEEKLY_REVIEW.md`](docs/W6_WEEKLY_REVIEW.md)，结论证伪、无提升数字；D5 独立裁判交叉验证、拒答/boundary 配对扩展、rerank 复核与 probes 接线见各 `evals/` 报告。DEC-039~046 |
| W7 | 11/16 – 11/22 | 护栏、成本、微调与 Agent | 🟢 **提前完成**（11/09 开做 D1–D7，D1–D7 全部收口；**11/10 云端补跑清掉微调欠账、11/11 补真 LangGraph Agent 并完成三线复核，周里程碑按复核结果收口通过**） | **D1** 输入护栏：`app/input_guard.py` 规则基线 10 条攻击拦 9/10、正常请求误拦 0/5，另接 `prompts/input_guard/v1.yaml` 模型判定补住 A10（判 block / secret_extraction，`/chat` 400）；Docker mock 注入 400 / 正常 200。**D2** 输出护栏：`app/output_guard.py` 的 `answer` / `citations` 白名单 + 格式错最多修一次 + 固定兜底 + 个人信息遮盖；10 条样例 10/10、全量 73 项 pytest 通过，`llmops-api:w7d2` 容器结构化请求 HTTP 200 且 `degraded=true` / `format_repair_failed`、流式 HTTP 422；**真实检查器已接**：`prompts/output_toxicity/v1.yaml` + `prompts/output_grounding/v1.yaml` 与 `judge_toxicity` / `judge_grounding` 复用 D1 的 `llm.chat` 范式，由默认关闭的 `OUTPUT_GUARD_MODEL_JUDGE_ENABLED` 注入 `/chat`（结构化分支两个判定器 + 可选 `context`，非结构化分支只跑毒性），真实模型四条判定 4/4、三条组合 3/3，mock 下拒绝冒充并安全兜底。**量化评估**（[`evals/OUTPUT_GUARD_BENCH.md`](evals/OUTPUT_GUARD_BENCH.md)）：30 条标注样本，修掉模型重复输出 JSON 后解析失败 16/30 → 0，有效 28/30 里误拦 0 / 漏拦 0 / 依据判错 0；单次判定 P50 1.849s / P95 10.004s，一次结构化回答额外约 513 tokens；容器连真实上游完整链路已跑通（`upstream_mode=openai`，判定状态不再是 `not_checked`），全量 pytest 78 项。⚠️ 判定开关仍默认关闭：20s 超时下 2/30 会 fail-closed，串行两次判定明显拉长 `/chat`；真实流量分布下的误拦率未量。**D3** 成本控制：`app/cost.py` 精确缓存 + 规则分流 + Token 审计，`/chat` 两个默认关闭开关；20 条题集四组同条件压测每千次成本点数 **1754.6 → 626.5（−64.3%）**，只开分流 −61.5% / 只开缓存 −27.8%，命中率 28.4%、便宜档 69.7%，分流一致率 20/20；容器 `MISS → HIT` 且命中用量归零；18 项单测、[`evals/COST_BENCH.md`](evals/COST_BENCH.md)。⚠️ 语义缓存本机跑不起来（W4 D5 实测三缺），成本点数是演示单位、非模型成本单列未计入。**D4** 微调：本机纯 numpy 低秩适配实跑（参数 4096 → 512 = 12.5%、产物 12.8%，留出 MSE 0.000146 vs 0.000106，r=1 学不全停 190.111）+ 8 项单测；**2026-11-10 Kaggle T4 云端补跑真 LoRA 完成**：Qwen2.5-0.5B + r=8，可训练 540,672 / 494,032,768（0.1%）、训练 8.2 s / 峰值显存 2890 MB / adapter 2,175,168 B，留出 20 条格式符合率与准确率 **0% → 100%**；首轮 tiny-gpt2（隐藏维 2 的随机测试模型）0%→0% 学不动，Kaggle 预装 `torchao` 与 `peft` 冲突已留档。**D4 整项改判完成**，见 [`evals/LORA_DEMO.md`](evals/LORA_DEMO.md) §5。⚠️ 只有 40 条合成样本、4 类词语分类，100% 只证明「格式行为可被 LoRA 学会」，不是业务效果提升；云端 v2 原始结果留在 `evals/out/w7d4/w7d4_lora_result_v2_qwen.json`（本地产物不入库）。**D5** 决策对照表：[`docs/W7_D5_DECISION.md`](docs/W7_D5_DECISION.md) 按「给决策层讲」成稿，每条结论挂 10 条证据编号；`app/decision.py` 规则选择器 + 12 条人工标注场景 **12/12 一致、证据覆盖 100%、微调仅 1/12 且带「业务效果未量」声明**，22 项单测；全量 pytest 126 项；2026-11-10 云端补跑后 `E-FT-2` 改记为「小样本格式证据已量、业务效果未量」，默认顺序与「先 RAG，微调保持候选」的结论不变。**D6** Agent 编排：本机无 `langgraph`/`langfuse`，用零依赖标准库做最小可观测 Agent（三工具 + 全链路 trace + 最大步数 / 超时 / 异常工具三条守卫），7 场景 9/9 断言、S1 三步链路每步工具名 / 入参 / 结果 / 耗时 / 理由齐全，13 项单测，`calculator` 用 AST 白名单拒绝注入表达式；⚠️ 这是 11/09 时点的零依赖快照，原文保留；**2026-11-11 已补真 LangGraph 版**：`app/agent_graph.py` 用 `StateGraph` + `ToolNode` + `InMemorySaver` checkpointer 重写，每步工具调用是真 OTel span（`agent.run` 根 + 3 个 `agent.tool.*` 子 span，父子层级已验），8 场景 24/24 断言、22 项单测、checkpointer 三次断点续跑（每次换新编译的图读同一份状态），与零依赖版前三个场景结束原因 / 工具链 / 入参逐项一致；`langgraph 1.2.20` + `langfuse 3.15.0` 已装。**span 已真上报**自托管 Langfuse 面板（`--langfuse` 跑 S1 后回查 `/api/public/v2/observations` 确认同 traceId 下 `agent.run` + `agent.tool.calculator` 落地）；此前记的「服务未启动」是误判，真因是 `LANGFUSE_HOST=localhost` 走 IPv6 `::1` 且本地有 `HTTP_PROXY` 无 `no_proxy`，已在 `enable_langfuse_tracing()` 里归一化 host + `httpx.Client(trust_env=False)` 修掉。见 [`evals/AGENT_GRAPH.md`](evals/AGENT_GRAPH.md) §10。**D7** 周复盘：按 11/09 时点为成本 ✅ / Agent ✅ / 微调 ❌ → 周里程碑未验收，含三问、6 条欠账与 3 分钟讲稿；判定只反映 11/09 时点，**11/10 微调线真训练证据已补齐**；11/11 三线复核全 PASS，**周里程碑已按复核结果收口通过**（用户 11/11 拍板，11/22 不复跑二遍）。**11/11 D7 动手部分已做**：新增 [`scripts/w7d7_acceptance_check.py`](scripts/w7d7_acceptance_check.py) 重跑四线再按阈值判定（不再抄旧报告），成本降 64.3%、微调参数占比 12.5% 且秩不足对照成立、Agent 8 场景 + span 父子层级 + 续跑收口，**三线全 PASS**，正本 [`evals/ACCEPTANCE_W7.md`](evals/ACCEPTANCE_W7.md)；判定逻辑配 18 项单测（专测坏数据必须 FAIL），三项本机不可重跑的引用证据单列不混入 PASS。全量 pytest **179 项**（139 + D6 新增 22 + D7 判定 18 项）。七篇实践笔记与复现命令在知识库 |
| W8 | 11/23 – 11/30 | 作品集打包与求职启动 | 🟡 **提前开工**（11/11 D1、11/12 D2 提前完结；11/13 D3 完结，终版视频入库；D4–D7 未开始） | **D1** 架构图定稿 + 技术决策清单：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 按 C4 只画上下文层 + 容器层（四条数据流检索 / 评估 / 观测 / 成本逐条可走通、8 个决策标注挂证据）；[`docs/DECISION_INDEX.md`](docs/DECISION_INDEX.md) 把 54 条决策整理成「选型 / 为什么不是另一个 / 证据路径 / 复核条件」五列清单，逐条校验证据文件真实存在；W7 四条线原为缺口，**2026-11-11 已补成 DEC-048~052**（输入护栏 / 输出护栏 / 成本 / 微调 / Agent）；**11/12 新增 AWS Emulator 本地 IaC 对照验证层**（[`infra/aws-emulator/`](infra/aws-emulator/)）：PC 端 Docker 起 aws-emulator 1.0.322，与 VM 侧同版、同脚本跑对照：CLI S3 往返 `fc.exe` 报无差异、boto3 **9 通过 / 0 失败**、Terraform plan `4 to add` → apply `4 added` → 二次 plan `0 to add, 1 to change`（DynamoDB `on_demand_throughput` 保真漂移，与 VM 一致）→ destroy `4 destroyed` 后 head-bucket 404 / describe-table ResourceNotFoundException；产物为 `boto3_check.py`、`tf/main.tf` 与复现说明。**这一层把八周计划里「上云」要练的内容补齐了**：AWS API 语义、S3 / DynamoDB 资源模型、CLI / SDK 与 Terraform 资源编排、资源全生命周期与保真漂移，全部在本机 `127.0.0.1:9297` 用 AWS 官方模拟器跑通（不建云账号、不产生云账单）；简历措辞按事实写成「AWS Emulator 本地 IaC 验证」，不虚报真实云账号部署。**D2 README 重写（11/12 提前完结，原计划 11/24）**：按「问题 → 方案 → 取舍理由 → 5 个数字 → 如何复现」五段重写文首，5 个数字逐条标报告路径与小节并回仓核对（成本 −64.3% / RRF hit@1 7/10→10/10 / RAGAS 四项 / 输出护栏 30 条量化 / 决策对照表 12/12），复现段写全七类前置条件与命令链；W1–W7 逐周明细留在文末不删。`--staged` 脱敏 0 命中、全量 pytest 179 passed。⚠️ 验收项「换个人换目录从头跑」需真人，留 W8 收尾补，不自己代判。**D3 完结（11/12 开工、11/13 收口，原计划 11/25）**：[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) 写成一稿两用的 10 分钟价值叙事脚本（问题 → 原型 → 评估数字 → 落地路径 + 「CI 拦住坏改动」反例镜头），另裁 3 分钟版与 30 秒定位语；5 个数字与 README 第 4 段逐字对齐、口径边界照讲，门禁三条命令实跑退出码 0 / 1 / 0，`--sabotage` 不改文件。**终版视频** [`docs/demo/w8d3-demo-final.mp4`](docs/demo/w8d3-demo-final.mp4)（220 s / 1080p / H.264+AAC）：按真实画面重剪 14 个镜头，每句旁白、每条字幕都落在它描述的那张画面上；微软 TTS（`zh-CN-YunyangNeural`）逐句配音 + 逐字中文字幕（底部半透明带，不压终端数字与退出码）；架构图镜头用 [`docs/images/llmops-architecture-v4.1-左右组合.png`](docs/images/llmops-architecture-v4.1-左右组合.png) 定格，数字卡 / 问题卡静态帧末帧保持。核对表 30 项硬内容（数字 / 判据 / 路径 / 命令 / 退出码）逐字保留。⚠️ 上传（B 站 / YouTube / 云盘）与「视频能独立看懂」的真人回述验收**未做**（作者本人操作，AI 不代发 / 不代判） |

**当前在做**：W7（11/16–11/22）护栏、成本、微调与 Agent，11/09 提前开做。**D1 输入护栏已跑完**：规则 9/10 + 模型判定补住 A10，Docker mock 注入 400 / 正常 200；**D2 输出护栏已完成日验收**：规则基线 10/10、全量 pytest 78 项、容器结构化 200 且安全兜底、流式 422，毒性 / 资料依据接了真实模型判定（4/4 + 3/3），30 条量化评估与容器连真实上游链路见 [`evals/OUTPUT_GUARD_BENCH.md`](evals/OUTPUT_GUARD_BENCH.md)（误拦 0/10、漏拦 0/8、单次判定 P50 1.849s、一次结构化回答 +513 tokens）。两天的实践笔记都在知识库，含 PowerShell 与 Git Bash 复现命令。⚠️ 判定开关仍默认关闭：20s 超时下 2/30 会 fail-closed，串行两次判定拉长 `/chat`；真实流量分布的误拦率未量。**11/09 晚间续做 D3–D7**：D3 成本控制、D5 决策对照表按日验收线判完成；当时 D4 微调 / D6 Agent 均只到部分完成（本机 CPU-only 且缺 torch/peft、无 langgraph/langfuse），真 StateGraph 仍未跑，D6 保持零依赖最小 Agent、不冒充框架实现（以上为 11/09 时点口径，**11/11 已装依赖并补真 LangGraph 版**，见下方待办 ① 与 [`evals/AGENT_GRAPH.md`](evals/AGENT_GRAPH.md)）；**D7 周复盘提前编写，按 11/09 时点判周里程碑未验收**。**2026-11-10 云端补跑已完成**：Kaggle T4 上真 LoRA 跑通 Qwen2.5-0.5B（留出 20 条 0%→100%、8.2 s / 2890 MB），D4 改判完成，`evals/LORA_DEMO.md` §5、`docs/W7_D5_DECISION.md` 与 `app/decision.py` 的 `E-FT-2` / caveat 已同步为「小样本格式证据已量、业务效果未量」；周里程碑已于 11/11 按三线复核结果收口通过（用户拍板，11/22 不复跑二遍）。另提前编写了 W8 阅读清单与 W8 D1 / D2 操作手册（非终稿）。全量 pytest **179 项**（139 + D6 新增 22 + D7 判定 18 项）、本轮改动文件 ruff 全过（其余为 W6 系列存量脚本 E501）。本周待办：~~① 装 langgraph/langfuse 后重写 D6~~ → **2026-11-11 已闭环**：真 `StateGraph` / `ToolNode` / checkpointer + OTel span，8 场景 24/24 断言、22 项单测、断点续跑实跑通过；`langgraph 1.2.20` / `langfuse 3.15.0` 已装，span 已真上报自托管 Langfuse 面板（ 路径回查确认），详见 [`evals/AGENT_GRAPH.md`](evals/AGENT_GRAPH.md)。② D3 真实流量重复率与四项非模型成本单价、D2 真实分布误拦率仍未量（这两项是口径边界，不卡 D3 / D5 的日验收）。生产前另需：真实任务上的微调单变量 A/B 与评估集，属决定上微调之前的额外实验，不计入 W7 三天验收线；D3 / D4 / D5 已按各自任务卡验收线全部判完成。ℹ️ FreeLLMAPI「Allow LAN access」2026-11-09 用户拍板**保持开启、不用关**，已从待办移除。W8（11/23 起）按原计划做作品集打包，**11/11 已提前完成 D1**：架构图按 C4 收到两层（[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)）+ 54 条决策整理成五列清单（[`docs/DECISION_INDEX.md`](docs/DECISION_INDEX.md)），D2 起做简历与 3 分钟 demo。

**W6 已收口**（11/8 完成，11/9 D3 口径变更收口）：**11/9、11/10 两天全天完结**：递归 B（400/50）写成正式基线；混合检索生产复验 + rerank 延迟测量，结论默认不开。**11/11 / 11/12 提前跑过**：50 题 golden set 标完但复标验收未过（`evals/ACCEPTANCE.md`）；RAGAS 已于 11-08 用固定裁判 `glm-5.3-flash` 跑完 50 题全量 + 分组报分（四项 50/50 全齐、NaN 0，见 `evals/RAGAS_BASELINE.md` §全量复跑）。**11/13 已实跑**：单变量 A/B 归因证伪了「改 prompt 能提 relevancy」；11-08 晚间再用独立裁判 `glm-5.3-flash` 重评同一批 A/B 配对做交叉验证，四项 10/10 全齐（含此前全缺的 context precision），**「改动不采纳」维持原判**，详见 `evals/ATTRIBUTION.md` §独立裁判交叉验证。**11/14 已实跑**：`source_chunk` 回填 + 非 LLM eval 门禁进 CI，详见 `evals/CI_EVAL_GATE.md`。**11/10 复核补做**：按 DEC-042 用带 reference 的 context precision 重评 rerank（20 题分层抽样，基线 top-5 与 D4 逐字一致 20/20），CP 0.7843 → 0.8451 但配对 Δ +0.0608 落在 σ 0.2399 里、精排 P50 4545 ms，**默认不开维持**，详见 `evals/RERANK_RECHECK.md`；`app/retrieval.py` 把 `ivfflat.probes = 3` 收敛成应用侧统一连接入口 + 6 条集成测（DEC-041 补记）。**11/15 提前收口（11-08 晚间）**：周复盘与「改动前 / 后」对比数字成稿在 [`docs/W6_WEEKLY_REVIEW.md`](docs/W6_WEEKLY_REVIEW.md)（简历素材正本；两轮单变量 A/B 都判定不采纳，结论**证伪、无提升数字**）；周验收四条全部落地，D3 已于 2026-11-09 按预留双指标口径验收通过（Jaccard 0.816 / 严格一致 70.2%，DEC-047；⚠️ 旧线 0.8 未达、标注员为 AI 代理） → 按现行口径评估体系闭环；D5 的 A/B 10 题全是可答题（`single_hop` 8 / `multi_hop` 2），按现有题型切片已于 11-08 离线补算在 `evals/ATTRIBUTION.md` §按题型切片（零 LLM 调用，判定对题型不敏感）；12 道应拒答与 3 道 `boundary` 的**配对样本**已于 11-08 晚间补齐（`evals/REFUSAL_AB.md`：行为分零翻转、判定不变、按纪律留 1 个 CP 缺分）。其余 D4/D5 欠账已于 11-08 收口。
**W5 已收口**（2026-11-04，提前 4 天）：D1–D6 全绿，监控三件套 + 测试/CI + Terraform 本地 kind IaC，DEC-034~038。
**W4 已收口**（2026-10-28）：D1–D7 全部完成，周复盘三问与架构图 v3（十二层）已定稿。
决策日志共 **54 条**：编号 `DEC-005 ~ DEC-052` 有 **47 条**，另有 7 条 2026-10-07/08 的早期决策无编号（编号体系 10-09 才起，`DEC-001~004`、`DEC-008` 从未存在，不是丢了）。**W7 四条线的缺口已于 2026-11-11 补齐**：输入护栏 DEC-048 / 输出护栏 DEC-049 / 成本 DEC-050 / 微调 DEC-051 / Agent DEC-052。索引见 [`docs/DECISION_INDEX.md`](docs/DECISION_INDEX.md) 第七节，原文见 [`docs/decisions.md`](docs/decisions.md)。
