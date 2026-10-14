# Langfuse 自托管（观测层）

W2 D9 产出物。给作品集服务加上 **trace / token / 成本** 的可见性。

## 这套东西解决什么

D8 的 `/chat` 能回答「模型说了什么」，但回答不了这些问题：

- 刚才那次请求，**上游到底花了多少 token、多少钱**？
- 用户抱怨「慢」——**慢在哪一段**：建连、排队、还是生成？
- 上线了一个新 prompt 版本，**质量是变好了还是变差了**？
- 一次失败的重试，**最后一次成功前消耗了几个失败请求**（白烧了多少额度）？

Langfuse 就是把这些变成「可查的面板」。它是 LLMOps 里**观测层**的落点 ——
和 K8s 的探针/日志是一回事，只是观测的对象从「容器活没活」变成「一次 LLM 调用花了多少」。

## 架构（6 个服务，别被吓到）

```
                 ┌──────────────────────── 浏览器 ────────────────────────┐
                 │                       http://localhost:3000            │
                 └────────────────────────────┬───────────────────────────┘
                                              ▼
   项目服务 /chat ──SDK上报──▶  langfuse-web (UI + API, :3000)
                                     │  ▲
                                     │  │ 异步任务
                                     ▼  │
              ┌──────────────┬───────────────┬──────────────┬─────────────┐
              ▼              ▼               ▼              ▼             ▼
        postgres(5433)   clickhouse      redis(6380)    minio(9090)  langfuse-worker
        元数据/账号       trace/观测      队列/缓存      对象存储      后台任务
```

为什么是这些组件（v3 之后的架构，和早期版本不同）：

| 组件 | 存什么 | 为什么不用别的 |
|---|---|---|
| **Postgres** | 用户、项目、API key、配置、数据集 | 强一致的小数据，关系型最合适 |
| **ClickHouse** | 事件明细（v4：`events_core`/`events_full`；旧版：`observations`/`traces`） | **量大、只追加、按时间范围查** —— 正是列存 OLAP 的强项；v2 曾用 Postgres 存 trace，量大后扛不住才拆出来 |
| **Redis** | 任务队列（BullMQ）、缓存 | 事件入库是异步的，需要可靠的队列 |
| **MinIO** | 事件与媒体的大块存档 | 大 payload 不该塞进关系库；S3 兼容便于换云存储 |

> ⚠️ **v4 存储真相（实测）**：`langfuse:4` 把明细写进 ClickHouse 的 **`events_full`**，
> 而 `observations` / `traces` / `analytics_*` 等旧表**保持 0 行**；
> 面板/查询走 **`GET /api/public/v2/observations`**，由事件表投影而来。
> 直接用旧表名或旧 API 路径核对会误判成「没数据」——这是本次最大的坑。

> **关键认知**：Langfuse 自身的「存储分层」本身就是一套可讲的架构决策 ——
> 什么数据放关系型、什么放列存、什么放对象存储，各有各的理由。
> 这在面试里比「我会用 Langfuse」值钱得多。

## 启动

**前置**：Docker Desktop 在运行（⚠️ 若拉镜像失败并报 `dial tcp 127.0.0.1:xxxx: actively refused`，
是**代理客户端**临时不可用，不是镜像站的问题——确认本机代理（如 `127.0.0.1:9527`）可用后重试即可）。

```bash
# 1. 准备 env（第一次）
cp infra/langfuse/.env.example infra/langfuse/.env

# 2. 生成三把**必填**密钥，填进 infra/langfuse/.env
openssl rand -hex 32    # → NEXTAUTH_SECRET
openssl rand -hex 32    # → SALT
openssl rand -hex 32    # → ENCRYPTION_KEY   （必须 64 位 hex）

# 3. 起（在仓库根执行）
docker compose -f infra/langfuse/compose.yml --env-file infra/langfuse/.env up -d

# 4. 看状态，等 langfuse-web 起来（约 1–3 分钟）
docker compose -f infra/langfuse/compose.yml ps
docker compose -f infra/langfuse/compose.yml logs -f langfuse-web   # 看到 "Ready" 即可

# 5. 取 Public Key / Secret Key —— 二选一：

#  方式 A（推荐，headless）：在 .env 里配好下面 5 项后**重建**，Langfuse 会自动建
#  组织 / 项目 / 管理员 / API key，并把 key 直接写在你手里（无需进 UI）：
#     LANGFUSE_INIT_ORG_ID=llmops-org            # ⚠️ 变量名必须是 *_ORG_ID，不是 *_ORGANIZATION_ID
#     LANGFUSE_INIT_ORG_NAME=llmops-org
#     LANGFUSE_INIT_PROJECT_ID=llmops-prod
#     LANGFUSE_INIT_PROJECT_NAME=llmops-prod
#     LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-<自己生成的随机串>
#     LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-<自己生成的随机串>
#     LANGFUSE_INIT_USER_EMAIL=admin@local.dev
#     LANGFUSE_INIT_USER_NAME=admin
#     LANGFUSE_INIT_USER_PASSWORD=<任意口令>
#   ⚠️ 这些值在**容器创建时**注入，改完必须 `up -d`（会重建）才生效；只 restart 不重建无效。

#  方式 B（手动）：浏览器 http://localhost:3000 → Sign up → 建 Organization + Project
#    → Project Settings 里复制 Public / Secret Key

# 6. 把 key 填回**仓库根** .env（不是这个目录的 .env），并显式开启观测：
#    LANGFUSE_PUBLIC_KEY=pk-lf-...
#    LANGFUSE_SECRET_KEY=sk-lf-...
#    LANGFUSE_HOST=http://localhost:3000
#    OBSERVABILITY=langfuse          # off(默认) / langfuse / log

# 7. 端到端自检（发一条测试 trace + 双通道确认落库）
python scripts/verify_langfuse_trace.py \
    --public-key "$LANGFUSE_PUBLIC_KEY" --secret-key "$LANGFUSE_SECRET_KEY" \
    --ch-user clickhouse --ch-password <CLICKHOUSE_PASSWORD>

# 停（数据保留在命名卷里）
docker compose -f infra/langfuse/compose.yml --env-file infra/langfuse/.env down
# 停并删数据（慎用）
docker compose -f infra/langfuse/compose.yml --env-file infra/langfuse/.env down -v
```

## 端口（全部只绑 127.0.0.1，不对外）

| 服务 | 宿主端口 | 说明 |
|---|---|---|
| langfuse-web | **3000** | UI + API，浏览器入口 |
| langfuse-worker | 3030 | 仅本机（不需要浏览器直连） |
| **postgres** | **5433** | ⚠️ **刻意偏移** —— 项目库占了 5432 |
| clickhouse | 8123 (HTTP) / 9000 (native) | |
| redis | **6380** | ⚠️ 刻意偏移，避开将来别处起的 6379 |
| minio | 9090 (API) / 9091 (console) | |

**为什么独立的 compose 文件**（不并进 `infra/compose.yml`）：官方 Langfuse 的变量名
（`POSTGRES_PASSWORD` / `REDIS_AUTH` / `CLICKHOUSE_PASSWORD` …）与项目库**同名**，
共用一个 `.env` 会互相串值。拆开 = 各有各的 env 文件，物理隔离。

## 踩坑记录

1. **postgres 端口冲突是必然的，不是偶然** —— 官方 compose 默认 `5432`，
   只要本机已经有一个 Postgres 就撞。改**宿主侧**端口（`5433:5432`）即可，
   容器内保持 5432 所以 `DATABASE_URL` 不用动。
2. **变量名撞车比端口冲突更隐蔽** —— 端口冲突会直接报错，变量串值不会：
   容器都起得来，只是连到了错的库。这也是要独立 env 文件的原因。
3. **`ENCRYPTION_KEY` 必须是 64 位 hex** —— 写成随便一串字符会在启动时被拒。
4. **media 上传的 endpoint 必须是浏览器可达的地址** ——
   worker 用容器内网名（`http://minio:9000`）没问题，但浏览器不行，
   所以 `langfuse-web` 单独覆盖成 `http://localhost:9090`（compose 里已处理）。
5. **首次启动慢是正常的** —— ClickHouse 建表 + 迁移，约 1–3 分钟。
   `logs -f langfuse-web` 里看到 `Ready` 才算好。

### ⚠️ v4 专属的坑（2026-10-14 实测，`langfuse:4` 镜像）

6. **`deploy.resources.limits.memory: 1G` 会真的 OOM** ——
   Compose v2 在非 swarm 模式**也会**把 `deploy.limits` 当 cgroup 上限应用。
   `langfuse-web`（Next.js）在 1G 下直接 node OOM 崩溃（日志见 `v8::OOMErrorHandler`），
   `restart: always` 导致崩溃循环、探活恒 000。**已把 web / clickhouse 提到 2G。**
7. **v4 的公共查询 API 移到了 `/api/public/v2/` 前缀** ——
   `GET /api/public/traces`、`/api/public/observations` 一律 **404**，并返回文案
   “not available on deployments running in Langfuse v4 events_only mode”。
   ⚠️ **别被这句话误导**：它不是「API 被禁用」，只是**旧 v1 路径的兼容报错**。
   正确路径是 **`GET /api/public/v2/observations`**（UI 面板的数据源，实测 200 有数）。
8. **trace 明细落在 `events_full`，不是 `observations`** ——
   v4 用事件表族（`events_core` / `events_full`）做存储，`observations` / `traces` /
   `analytics_*` 这些旧表在本部署下**始终为 0**。程序化核对要去 `events_full` 查
   （`metadata_names` / `metadata_values` 是**平行数组**，不是 map —— 别用 `metadata['k']`）。
9. **`LANGFUSE_INIT_*` 的变量名要对齐** —— `.env.example` 里是 `LANGFUSE_INIT_ORG_ID`
   （**不是** `LANGFUSE_INIT_ORGANIZATION_ID`）。名字写错时 Langfuse 只打印一行 warn
   （“LANGFUSE_INIT_ORG_ID is not set … will be ignored”）然后**静默跳过整个初始化**，
   表现是「登录进去发现没有 org / 没有 key」。
10. **预设 key 不做格式校验** —— `LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY` 在源码里
    只是 `z.string().optional()`，**不校验前缀和长度**。所以可以自己 `openssl rand`
    生成 `pk-lf-…` / `sk-lf-…` 填进去，Langfuse 原样采用 —— headless 拿 key 的关键。

## 与八周计划的关系

| 周次 | 用 Langfuse 做什么 |
|---|---|
| **W2 D9** | 起服务 + 接 SDK，每次 `/chat` 打 trace（input/output/模型/耗时/token/成本） |
| W3 | 实验追踪：同一 prompt 的不同版本，用 trace 对比 |
| W6 | 评估：把 RAGAS 的打分写回 trace，做「质量可追溯」 |
| W7 | 护栏与成本：按 trace 统计 token 花费，做成本优化 |

> ⚠️ 本地磁盘有限：D11 做压测时记得**调低采样率**，别让 trace 把盘写满。
