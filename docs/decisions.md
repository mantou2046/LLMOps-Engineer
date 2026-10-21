# 决策日志

> 规则：**每个关键选型写一条，写清「为什么选它、放弃了什么」**。  
> 面试时这一页比 README 更能证明你是在做工程，而不是在跟着教程敲。  
> 格式：`## YYYY-MM-DD · 决策标题` → 背景 / 选项 / 选择 / 理由 / 代价 / 复核条件

---

## 2026-10-07 · 作品集选题：资金/财务制度知识库问答

**背景**  
八周学习需要一个贯穿始终的作品集项目，而不是八个独立练习。选题要同时满足三件事：技术栈能覆盖 LLMOps 全链路、数据源我能自造（不依赖任何内部资料）、且能体现我的差异化。

**选项**

1. 通用文档问答（用公开数据集，如论文 / 维基）—— 最省事，但和几百个同类项目没区别
2. 纯技术演示（模型对比 / 推理性能压测）—— 技术含量高，但没有业务场景
3. **资金 / 财务制度知识库问答** —— 数据要自造，但业务域是我的一线经验所在

**选择**：方案 3

**理由**

- **差异化**：市面转 LLMOps 的人大半是纯工程背景，**没有业务域**。资金 / 财务制度这个领域我有 用友 BIP / 银企直联 / 银行回单 的交付经验，能问出别人问不出的难例（如「这笔付款该走哪条审批链」「条款冲突时以哪个为准」）——**难例质量直接决定评估体系的说服力**。
- **技术覆盖**：制度问答天然需要引用溯源 → 强制做 RAG + 评估；条款级精度要求 → 强制做检索调优；合规敏感 → 强制做护栏。需求是自然长出来的，不用硬凑。

**代价**

- 数据必须**全部自造**，前期要多花时间编一套同构的样例制度文档
- 不能拿真实案例讲，说服力略打折（但换来的是零合规风险）

**复核条件**  
若 W6 建 golden set 时发现自造文档覆盖不了足够的难例类型（如条款冲突、跨文档引用），则补充文档而不是放宽评估标准。

---

## 2026-10-07 · 数据源方案：全部自造同构样例

**背景**  
作品集需要一个**能公开演示**的数据源，而资金 / 财务制度的原始资料来自具体单位。

**选择**：数据层**全部自造同构样例**，真实资料永不入库。  
（仓库可见性与提交门禁是另一件事，见下一条决策。）

**理由**

- **自造数据不是妥协，反而更好用**：能**主动设计难例**（条款冲突、跨文档引用、应该拒答的问题），而真实数据往往「刚好能用」却覆盖不到边界 —— **难例质量直接决定评估体系的说服力**
- 零合规风险，可以放心演示

**代价**：需要额外写一套数据生成脚本；演示时不能说「这是某公司的真实制度」，说服力略打折

**复核条件**：数据源范围变更后，重新判断一次「这份数据能不能公开」；提交层面的脱敏由下一条决策的门禁兜底

---

## 2026-10-07 · 仓库设为公开，并加一道提交前脱敏门禁

**背景**  
作品集要公开才有求职价值，但数据源涉及财务制度。需要在「可见性」和「泄露风险」之间定一个策略。

**选项**

1. 私有仓库，W8 再决定是否转公开
2. 公开仓库，靠自觉 + `.gitignore`
3. **公开仓库 + 提交前自动脱敏检查**

**选择**：方案 3

**理由**

- **私有等于作品集不存在**：简历上写不了链接，面试演示要当场加 collaborator。对求职目的来说，方案 1 不是「风险更低」，而是「目标失效」。
- **「先私有做完再转公开」是错的**：转公开时**整个提交历史一起公开**。八周里只要手滑 commit 过一次 `.env`、一份真实文档、一条内网地址，转公开那一刻它就在公网上了，而且**删不掉**（已 fork / 已缓存）。从第一天 public 反而**强迫从一开始就守规矩**。
- **实际约束**：W5 要上 Actions、W6 要把 eval 接进 CI，而 eval 很吃时间。公开仓库 Actions **无限免费**，私有仓库 Free 账户只有 **2000 分钟/月**，很可能撑不到 W6。
- **靠流程而非靠意志**：单靠自觉，八周里迟早手滑一次。加一道自动门禁，把「记得检查」变成「不用记得」。

**门禁设计（三层）**

1. **内置规则** —— 私钥、各类 API Key / Token、JWT、内网 IP 段、带口令的连接串、疑似凭据键值
2. **自定义词表** —— `scripts/sensitive_terms.txt`（单位名 / 内网域名 / 真实表名字段名）
3. **CI 兜底** —— `.github/workflows/ci.yml` 在 PR 上跑同一脚本，防本地没装钩子的人绕过

**两个刻意的设计**

- **脚本只输出「文件:行号 + 命中类型」，绝不回显原文** —— 否则检查工具本身就成了泄露渠道
- **真实词表 gitignore，只提交模板** —— 否则「防泄露清单」本身就成了泄露源

**代价**

- 需要维护一份词表，且**词表要自己记得更新**（新增内部术语时要补）
- 会有误报，需要人工判断（所以钩子保留 `--no-verify` 逃生口）

**复核条件**  
若八周内出现一次误报导致被迫跳过检查，说明规则太严，应改为「只报新增行」而不是放开规则。

---

## 2026-10-08 · 容器化基础：多阶段构建 + 非 root + 固定 tag

**背景**  
W1 D2 要把服务容器化。容器化本身不难，难的是「生产可用的容器化」——面试官不会问「你会不会写 Dockerfile」，而会问「你的镜像为什么这么大 / 为什么以 root 跑」。所以在第一天就把这几条钉死，后面所有服务都照这个标准走。

**选项**

1. 单阶段 + 完整基础镜像（`python:3.12`）—— 最省事，镜像约 1GB，root 运行
2. 单阶段 + slim 镜像 —— 体积降下来，但编译依赖仍留在最终镜像里
3. **多阶段（builder + runtime）+ slim + 非 root + 固定小版本 tag**

**选择**：方案 3

**理由**

- **BP6 多阶段**：编译期需要 `build-essential` / `gcc`（装 wheel 用），运行期**一个都不需要**。多阶段让最终镜像只带 venv，编译器留在上一阶段 —— 这同时解决了**体积**和**攻击面**两个问题（少一个 gcc 就少一批 CVE）。
- **BP7 非 root**：Docker 默认以 root 跑容器内进程。一旦应用被越权，root 权限是容器逃逸的跳板。建 `appuser`（UID 1001）成本极低，收益是实质性的安全隔离。
- **BP2 固定 tag**：`latest` 是动态的，今天和下月构建出的镜像可能不同，破坏**可重现性**。用 `python:3.12.7-slim-bookworm` —— 连 Debian 代号都锁。
- **为什么 Python 3.12 而不是本机的 3.13/3.14**：3.12 的第三方生态（尤其是后续要用的 RAGAS、Langfuse SDK）兼容性最稳。**本机版本和容器内版本不一致是正常的**，容器化的意义之一就是隔离宿主差异。

**代价**

- Dockerfile 复杂度上升，新手读起来要多花几分钟
- 基础镜像锁死后，安全补丁需要手动 bump（这是「可重现」的必然代价）
- 加了 `Dockerfile.naive` 作为反面教材 —— 多维护一个文件，换来的是**体积对比有实测数据**而不是拍脑袋说「省了 70%」

**复核条件**  
若 W5 接 CI 后发现镜像构建时间过长（多阶段要建两次环境），再评估是否用 BuildKit cache mount 加速，而不是退回单阶段。

---

## 2026-10-08 · 用 compose 而非 K8s 起步（本地开发环境）

**背景**  
D2 要起一个 Postgres + pgvector。工具选择上，compose 和 K8s 都能做。

**选择**：**本地开发用 compose，W2 上云/上集群时再迁到 K8s**

**理由**

- **关注点分离**：D2 的学习目标是「容器化 + 镜像优化」，不是「编排」。此刻引入 K8s 会让「数据库起不来」和「YAML 写错了」两类问题混在一起，排错成本翻倍。W1 D3–D5 专门学 K8s，那时再引入。
- **compose 是单机多容器的正确工具**：它解决的是「本地一键起一套依赖」，这正是开发环境的需求。用 K8s 做这件事属于杀鸡用牛刀。
- **迁移是自然的**：compose 里的配置（镜像 tag、环境变量外置、健康检查、卷持久化）在 K8s 里**语义一一对应**（Deployment / ConfigMap+Secret / probes / PVC）。D4 讲 ConfigMap/Secret/PVC 时，可以直接拿 compose.yml 做对照讲。

**代价**

- 要维护两套编排（compose + K8s 清单），但两者用途不同，不算重复
- compose 的 `depends_on: condition: service_healthy` 在 K8s 里没有直接等价物（要用 initContainer），迁移时需要改写

**复核条件**  
若本地容器数量超过 5 个、或需要多节点模拟，则改用 kind/minikube 做本地环境。

---

## 2026-10-08 · 数据库初始化用 initdb 脚本，而非在应用里建表

**背景**  
pgvector 需要 `CREATE EXTENSION vector` 才能用。这个动作放哪里？

**选项**

1. 应用启动时检查并创建扩展
2. `docker-entrypoint-initdb.d/` 里的初始化 SQL
3. 用 Alembic 等迁移工具

**选择**：**方案 2（当前阶段）**，W3 起迁移到方案 3

**理由**

- **扩展是「数据库级」的东西，不是「应用级」**。让应用（一个普通权限用户）去 `CREATE EXTENSION` 需要超级用户权限，这在生产里是权限模型上的倒退。
- **方案 2 零成本**：官方 postgres 镜像原生支持，挂个目录即可，不需要引入任何依赖。
- **为什么 W3 要换到迁移工具**：initdb 脚本**只在数据卷为空时执行一次**。一旦开始改表结构（W3 做实验追踪、W6 加检索表），必须要有版本化迁移，否则「同事拉下代码但表结构是旧的」会成为常态。

**代价**

- 现在改 schema 要 `down -v` 重建（丢数据），阶段内可接受
- 写了明确的注释说明这个限制，避免以后误以为改 SQL 就能生效

**复核条件**  
进入 W3 第一次改表结构时，必须换成迁移工具，不能继续靠 initdb。

---

## 2026-10-08 · 脱敏门禁加「占位符豁免」，而不是放宽规则

**背景**  
加 Dockerfile / compose / 验证脚本时，门禁报出 4 处命中，**全是误报**：

| 位置                        | 内容                                                   | 性质         |
| ------------------------- | ---------------------------------------------------- | ---------- |
| `infra/compose.yml`       | `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}`  | 对凭据的**引用** |
| `infra/compose.yml`       | `PGADMIN_DEFAULT_PASSWORD: ${PGADMIN_PASSWORD:?...}` | 同上         |
| `infra/check_pgvector.py` | `"postgresql://{u}:{p}@{h}:{port}/{db}"`             | python 模板串 |
| `infra/check_pgvector.py` | docstring 里的用法示例                                     | 文档         |

**问题**：门禁一动就报警，人会养成 `--no-verify` 的习惯 —— **那时门禁等于不存在**。  
这是安全工具的经典失效模式：不是「不够严」，而是「太吵以至于被绕过」。

**选项**

1. 关掉相关规则 —— 检出能力永久损失，不可接受
2. 每次误报手动 `--no-verify` —— 等于放弃门禁
3. **加一层「占位符识别」**：形式上不可能是真凭据的写法放行，其余照拦

**选择**：方案 3

**理由**

- **真凭据有可判别的形态**：它是随机字符串（`hunter2Xk9pQz`），**不会**长成 `${VAR}` / `{u}` / `<your-password>` / `changeme`。  
  「形式判据」能把两者干净地分开，而不是靠降低强度。
- **放行名单刻意写窄**：只认 `${`、`{{`、`<`、`{var}`、`your-*`/`test-*` 前缀、纯掩码（`...`/`xxx`）、少数通用词（`password`/`localhost`）。  
  **没有**把 `llmops` 这类「像真用户名的词」加进白名单 —— 那样会削弱保护。
- **改了文档而不是改规则**：最后 1 处残留命中在用法示例里（形如 `postgresql://<user>:<password>@<host>` 这种**真实感过强**的示例）。用户名这类词在真凭据里也很常见，  
  若为它加豁免就是真正的降级。**正确做法是让示例写成明确的占位形式**（`<user>:<password>@<host>`），  
  即「消除误报的根源」而不是「让规则闭嘴」。

**验证（必须有对照，否则不敢说没降级）**

| 组         | 用例                                                              | 结果                |
| --------- | --------------------------------------------------------------- | ----------------- |
| A · 占位符放行 | `${VAR}` / `{u}` / `<x>` / `changeme` / `user` / `xxx` … 共 13 例 | 13/13 放行 ✅        |
| B · 真凭据拦截 | `hunter2Xk9pQz` / `P@ssw0rd2026local` / `sk-abc…` … 共 5 例       | **5/5 拦截，0 漏报** ✅ |
| C · 端到端   | 4 例占位全放行；4 例真凭据（连接串 / 键值 / API Key / 内网 IP）全拦                   | ✅                 |

全树扫描从「4 处误报」变为「0 处命中」。

**代价**

- 豁免名单**需要维护**：若将来某种新写法被误报，应优先改写法，实在不行才扩名单
- 存在理论上的绕过面（攻击者可把真凭据伪装成 `{...}` 形式）—— 但**门禁防的是「手滑」，不是「恶意隐藏」**，  
  这个攻击模型不在设计目标内

**复核条件**  
若再出现一次「为绕过误报而 `--no-verify`」，说明豁免判断仍有缺口 → 补判据，而不是关规则。

---

## DEC-005 · 基础镜像选 alpine（musl）而不是 slim-bookworm（glibc）

**日期**：2026-10-08 · W1 D2

**背景**  
D2 验收硬指标之一是「最终镜像 < 200 MB」。用 `python:3.12.7-slim-bookworm` 做基础层时，  
多阶段已优化到 **247 MB**，剩余体积拆解如下：

| 组成                                | 体积       |
| --------------------------------- | -------- |
| `python:3.12.7-slim-bookworm` 基础层 | 176.5 MB |
| venv（已删 pip/setuptools/wheel）     | ~71 MB   |
| curl（HEALTHCHECK 用）               | ~4 MB    |

**问题**：光基础层就占 176.5 MB，**留给应用的预算不足 24 MB —— 物理上不可能达标**。  
这不是优化技巧问题，是选型问题。

**选项**

1. 接受 247 MB，把验收指标改成 < 300 MB —— **修改指标来迁就实现，是最坏的一种自我欺骗**
2. 用 distroless / scratch 自建 —— 体积更小（~60 MB），但要自己解决 libc、CA 证书、时区，且没有 shell 无法 `docker exec` 排查
3. **换 `python:3.12-alpine`（79.1 MB）** —— 保留完整 shell 与包管理器，调试体验不变

**选择**：方案 3

**理由**

- 净省约 **97 MB 基础层体积**，实测最终 **129.9 MB**，余量 70 MB（不是「擦线过」，留了后续加依赖的空间）
- 仍带 `sh` 和 `apk`，出问题时 `docker exec -it <c> sh` 能进去查 —— distroless 做不到这点，  
  对一个**还在快速迭代的学习项目**来说，可调试性 > 极限体积
- **前提是验证过的，不是赌的**：alpine 用 musl libc，带 C 扩展的包若无 musllinux wheel 就必须现场编译，  
  为编译装进去的 `gcc` + `musl-dev` 会把省下的体积吃回去。所以动手前先跑了一次真实安装：
  ```bash
  docker run --rm python:3.12-alpine sh -c '\
    python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt'
  ```
  全部成功，无 sdist 编译。构建日志可佐证命中的是 musl wheel：  
  `uvloop-0.23.0-cp312-cp312-musllinux_1_2_x86_64.whl`、  
  `watchfiles-1.3.0-cp310-abi3-musllinux_1_1_x86_64.whl`、  
  `websockets-17.2-cp312-cp312-musllinux_1_2_x86_64.whl`

**连带修改（选型变了，周边必须跟着变）**

| 项            | slim-bookworm                             | alpine                                                     | 原因                           |
| ------------ | ----------------------------------------- | ---------------------------------------------------------- | ---------------------------- |
| 包管理器         | `apt-get` + `rm -rf /var/lib/apt/lists/*` | `apk` / 换源改 `/etc/apk/repositories`                        | ——                           |
| 建用户          | `groupadd`/`useradd` + 长选项                | `addgroup`/`adduser` + **短选项**（busybox 不认 `--uid`/`--gid`） | busybox 版工具集                 |
| HEALTHCHECK  | `curl -fsS`                               | **`wget -q -T 3 --spider`**                                | **alpine 无 curl**            |
| builder 装编译器 | 可装（不影响最终镜像）                               | **刻意不装**                                                   | 依赖全是 wheel，装了纯浪费；哪天需要编译会立刻报错 |

⚠️ **runtime 不装 curl 是个有意识的取舍**：为「让 curl 可用」而 `apk add curl` 会连带  
`ca-certificates` 等约 4~8 MB，与体积目标矛盾。用 busybox 自带 wget 零成本解决。

**代价 / 风险**

- **musl 与 glibc 的行为差异**：某些依赖在 musl 下有细微差别（如 DNS 解析、`getaddrinfo`、locale 支持）。  
  本项目当前只用到网络 + JSON，无影响
- **未来可能被迫回退**：若引入只在 glibc 下发预编译包的依赖（典型如某些版本的 `oracledb`、  
  部分科学计算栈），alpine 上要么编译失败要么需要额外适配  
  → **回退路径明确**：把两个 stage 的 `FROM` 换回 `python:3.12-slim-bookworm`，  
  恢复 apt 与 `groupadd`/`useradd` 写法即可，改动集中在单个文件

**复核条件**

1. 引入任何新的带 C 扩展的依赖时，先确认有 musllinux wheel；没有则评估是否需要回退
2. 若构建时间因 alpine 下的现场编译而显著变长，说明 wheel 覆盖出现缺口 → 重新评估

**顺带记录 · 一个被这次选型暴露的通用教训**

优化过程中我曾把「删 pip/setuptools」写进 **runtime 段**，镜像**从 270 MB 涨到 284 MB**。  
原因：`COPY --from=builder /opt/venv` 那一层已含 pip，后面的 `RUN rm` 只加了个 overlayfs  
**删除标记（whiteout）**，底层数据仍在。挪回 **builder 段**（装完即删）后降到 247 MB。

> **「把清理放到后面」是错的直觉 —— 清理要放在「数据进入镜像之前」。**  
> 判断方法很简单：问自己「这条 rm 是在哪一层的**下方**还有没有副本」。

---

## DEC-006 · 本地集群选 kind 而不是 minikube（2026-10-09 · W1 D3）

**背景**：D3 要一个本地 K8s 集群，候选是 `kind` / `minikube` / `k3s`。

**决策**：用 **kind v0.33.0**，集群名 `llmops`。

**理由**

1. **复用已有环境**：D2 已装 Docker Desktop 4.94.0 + WSL2 集成 —— kind 只需要 Docker，  
   不用再引入 hypervisor。
2. **起停快、可反复推倒**：实测 `Ready after 12s`。D3/D4/D5 要反复 load 镜像、  
   改 YAML、删 Pod 验证持久化 —— `kind delete cluster` 干净利落，一天里重建多次不心疼。
3. **贴近 CI**：K8s 官方自己就用 kind 跑测试，同一套命令以后能直接搬进 CI。

**代价（已知）**

- NodePort **不能直接从宿主机访问**（节点 IP 在 Docker 网络里）→ 本地验证统一走  
  `kubectl port-forward`。D4 要外部访问时再评估 Ingress（kind 需配 `extraPortMappings`）。
- 需要 WSL 集成开启；⚠️ `wsl --shutdown` 会**连带停掉 Docker 的 WSL 后端**。

**复核条件**：需要多节点拓扑、或想一键装 Ingress/Dashboard 等 addon 时，重新评估 minikube。

---

## DEC-007 · 镜像用具体 tag + `IfNotPresent`，不用 `latest` + `Always`（2026-10-09 · W1 D3）

**背景**：kind 的节点是**独立容器**，看不到宿主机的 `docker images`，镜像必须  
`kind load docker-image` 显式载入节点。

**决策**：`infra/k8s/app.yaml` 里写 `image: llmops-api:0.1.0` + `imagePullPolicy: IfNotPresent`。

**理由**

- 若用 `latest` + 默认的 `Always`，kubelet 会**绕过本地镜像**去 Docker Hub 拉一个  
  不存在的 `llmops-api` → 直接 `ImagePullBackOff`，而镜像明明就在节点里。  
  这是 kind 场景下最经典的误判。
- 具体 tag 同时满足 D2 已确立的「不用 latest」原则（构建可重现）。

**复核条件**：接入真实镜像仓库（私有 registry + `imagePullSecrets`）时，  
`Always` 配合不可变 tag（digest）会重新变得合理。


## DEC-009 · kind 载入 multi-arch 镜像用 `ctr ... import --platform`，不用 `kind load`（2026-10-09 · W1 D4）

**背景**：`kind load docker-image pgvector/pgvector:pg16` 报  
`ctr: content digest sha256:a45475a5bc78...: not found`；  
换 `kind load image-archive` 同样报错。而本仓库自建的 `llmops-api:0.1.0`  
用同一条命令却能正常载入 → **是镜像的问题，不是 kind 坏了**。

**根因（两条，缺一不可）**

1. **代理泄漏**：节点从**建集群那一刻**继承了宿主环境变量  
   `HTTPS_PROXY=http://127.0.0.1:13566`。而节点**内部**的 `127.0.0.1`  
   指向节点自己、不是宿主 → 一切回源动作必然  
   `proxyconnect tcp: dial tcp 127.0.0.1:13566: connect: connection refused`。
2. **外平台层**：`docker save` 出的 tar 有 **23 个 blob / 16 个 manifest 层**，  
   多出的 7 个是**其它架构的层**（pgvector 是 multi-arch 镜像）。  
   `ctr` 默认按 `--all-platforms` 解，遇到解不开的外平台层就报 digest not found。

**决策**：改用「`docker save` → `docker cp` → 节点内 `ctr import --platform`」。

```bash
docker save pgvector/pgvector:pg16 -o pgv.tar
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'   # ⚠️ Git Bash 会改写路径
docker cp "E:\Projects\LLMOps-Engineer\pgv.tar" llmops-control-plane:/pgv.tar
docker exec llmops-control-plane ctr -n k8s.io images import \
    --platform linux/amd64 /pgv.tar
```

**理由**

- `--platform linux/amd64` **只解当前架构的层**，绕开外平台层。
- 这条路径**完全不依赖节点联网**，顺带规避了代理泄漏问题。
- ⚠️ `docker cp` 传 POSIX 路径（`/tmp/pgv.tar`）会报  
  `GetFileAttributesEx e:\tmp: The system cannot find the file specified`  
  —— Git Bash 的路径转换所致，**必须传真实 Windows 路径**。

**副作用 / 代价**：比 `kind load` 啰嗦（3 条命令 vs 1 条），所以  
**只对 multi-arch 镜像用这条兜底路径**；自建的单架构镜像继续用 `kind load`。

**复核条件**：建集群时清掉 `HTTPS_PROXY` 等代理变量、且镜像只推 amd64  
单架构（或改用 `--platform` 构建）时，`kind load` 会重新变得够用。

---

## DEC-010 · K8s Secret 清单里只放 `<PLACEHOLDER:...>`，真值由 `set -a; . ./.env` 注入（2026-10-09 · W1 D4）

**背景**：D4 要把连接串与 API Key 移进 K8s Secret。但本仓库是**公开的**，  
且 `scripts/check_sensitive.py` 有一道提交前门禁会拦「疑似凭据键值」。

**决策**：`infra/k8s/config.yaml` 里的 Secret **只写占位符**  
（`"<PLACEHOLDER:set-from-.env>"`），真值通过命令行从 `.env` 注入：

```bash
set -a; . ./.env; set +a          # .env 已在 .gitignore
kubectl create secret generic llmops-secrets \
  --from-literal=POSTGRES_PASSWORD \
  --from-literal=LLM_PRIMARY_API_KEY \
  --from-literal=LITELLM_MASTER_KEY \
  --dry-run=client -o yaml | kubectl apply -f -
```

**理由**

- **`stringData` 写什么，`get -o jsonpath` 就能还原什么** —— Secret 只是 base64  
  不是加密（D4 已动手演示过）。所以清单文件进了 Git，等于凭据进了 Git。
- `--dry-run=client -o yaml | kubectl apply -f -` 是**幂等**的：重复执行不会报  
  `AlreadyExists`，也不会把已有 Secret 删掉重建。
- 占位符用 `<PLACEHOLDER:...>` 而不是 `REPLACE_ME`，是**为了让门禁一眼放过**：  
  前者在扫描器眼里明显不是值，后者会命中「疑似凭据键值」规则。
- ⚠️ **注释里也不要写 `键=$变量` 形式** —— 那是踩过的坑：`check_sensitive.py`  
  按 `键=值` 形态判疑似凭据，连注释里的**示例命令**都会命中并拦住提交。

**代价**：克隆仓库后**必须先建 Secret**，直接 `kubectl apply -f infra/k8s/config.yaml`  
会得到一堆无效占位符。所以 `scripts/verify_d4.sh` 里第 1 步只 `apply` ConfigMap 部分  
和**已经带占位符的 Secret**（仅用于演示结构），真实场景走上面的注入命令。

**复核条件**：接入外部密钥管理器（Vault / External Secrets Operator / Sealed Secrets）  
时，明文占位符可以彻底从仓库里消失 —— 那时这条决策升级为「用 Sealed Secrets」。

---

## DEC-011 · kind 上装 ingress-nginx：本地清单 + 去 digest 钉死 + 清节点 containerd 代理（2026-10-09 · W1 D4）

**背景**：D4 第 2 步要装 ingress-nginx 配一条 Ingress 规则。

**四步操作（已全部固化进 `scripts/setup_ingress_kind.sh`）**

1. **必须删库重建集群**，用 `infra/k8s/kind-ingress-config.yaml`  
   （`extraPortMappings` 宿主 80/443 → 节点 30080/30443，**只在建集群时生效**）。
2. **清单下载走注册表代理**：沙箱 env 的 `HTTPS_PROXY`（本次 1250）只放行白名单，  
   拉 `raw.githubusercontent.com` 报 `Bad Gateway`。改用  
   `curl -x http://127.0.0.1:9527` 拉通，**清单落盘到仓库**（`infra/k8s/ingress-nginx/deploy-upstream.yaml`），  
   避免每次部署都依赖网络。
3. **清单打两处补丁**：
   - controller Service `LoadBalancer` → **`NodePort` 并显式 pin `nodePort: 30080/30443`**  
     （kind 没有云 LB 实现，不 pin 就接不上 `extraPortMappings`）。
   - **去掉所有镜像的 `@sha256:` digest 钉死，只留 tag**。
4. **清掉节点 containerd 继承的代理**（见下，这是最坑的一步）。

**根因（与 D9 同源，但更隐蔽）**

kind 建集群时会把宿主的 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` **注入节点容器**，  
进而被 **containerd 进程继承**。节点内部的 `127.0.0.1` 指向**节点自己**，不是宿主 →  
一切回源必挂 `proxyconnect tcp: dial tcp 127.0.0.1:1250: connect: connection refused`。

⚠️ **为什么 DEC-009 的 `ctr import` 这次不够**：镜像明明已导入节点，但

- 清单用 **`@sha256:` digest 引用** 时，kubelet 会拿 digest 去远端**校验 manifest**（不是查本地）；
- 即使改成 tag 引用，containerd 仍会先尝试回源确认 tag。

所以 digest 引用 + 代理这两个条件**叠加**时，**无论 `imagePullPolicy` 设什么都不管用**。

**✅ 修法（持久生效，不是临时绕过）**

```bash
# 给节点 containerd 加 systemd drop-in，清掉继承的代理
docker exec llmops-control-plane sh -c '
mkdir -p /etc/systemd/system/containerd.service.d
cat > /etc/systemd/system/containerd.service.d/99-no-proxy.conf << "EOF"
[Service]
Environment=
Environment="NO_PROXY=*"
Environment="no_proxy=*"
EOF
systemctl daemon-reload && systemctl restart containerd'
```

**理由**

- `Environment=`（空值）**清空** systemd 从父环境继承的全部变量。
- `NO_PROXY=*` 兜底：Go 的 proxy 逻辑里 `NO_PROXY` 优先级高于 `HTTP(S)_PROXY`。
- 镜像已用 `ctr import` 导入本地，**根本不需要回源**，所以关代理没有副作用。
- ⚠️ 这个 drop-in **不在节点镜像里**，`kind delete cluster` 后需重跑（脚本已含）。

**实测结果**：`verify_d4.sh` → **19 项全绿**，含  
`curl -H 'Host: llmops.local' http://localhost/health` → **HTTP 200** + 返回体正确；  
**负向验证**错误 Host → **404**（证明是 Ingress 在做 Host 路由，而非碰巧撞到 Service）。

**复核条件**：环境不设全局代理、或换用 `cloud-provider-kind`（kind 内置 LB 实现，  
可省掉 NodePort 补丁）时，第 3、4 步可以简化。

---

## DEC-012 · 三种探针的分工：readiness 比 liveness 灵敏一个数量级（2026-10-09 · W1 D5）

**背景**：D5 第 1 步要给 Deployment 加 liveness / readiness / startup 三种探针。  
探针参数写多少，是最容易随手拍、也最容易埋雷的地方。

**决策**：三组参数**刻意不等**，按「失败代价」排序取不同灵敏度。

| 探针             | 路径        | period | failureThreshold | 容忍窗口    | 失败后果             |
| -------------- | --------- | ------ | ---------------- | ------- | ---------------- |
| startupProbe   | `/health` | 2s     | 30               | **60s** | 杀容器（启动期）         |
| livenessProbe  | `/health` | 10s    | 3                | **30s** | 重启容器             |
| readinessProbe | `/ready`  | 3s     | 2                | **6s**  | 摘出 EndpointSlice |

**理由（核心权衡）**

- **readiness 要灵敏（6s）**：误判的代价只是「短暂少一个副本接流量」，代价低 → 可以激进。  
  摘流量是**可逆、廉价**的操作。
- **liveness 要保守（30s）**：误判的代价是**重启容器**（冷启动 + 可能进 CrashLoop 恶性循环），  
  代价高 → 必须容忍瞬时抖动。
- **startup 要最宽容（60s）**：它的作用就是「在启动期替 liveness 挡住一切」，  
  比 liveness 的 30s 更宽才能有效。⚠️ **若 startup 窗口 ≤ liveness 窗口，startup 形同虚设**  
  —— 所以 `verify_d5.sh` 里专门有一条断言检查这个不等式。

**另一个决策：liveness 不写 `initialDelaySeconds`**

有 startupProbe 兜住启动期后，liveness 从容器一启动就可以探（因为 startup 成功前它根本不执行）。  
写 `initialDelaySeconds` 只会拖慢「发现真死锁」的速度。官方文档也明确：

> "Liveness probes do not wait for readiness probes to succeed. If you want to wait before executing a liveness probe, you can either define `initialDelaySeconds` or use a startup probe."

**关键区分：liveness 打 `/health`（不查外部依赖），readiness 打 `/ready`（W2 改查数据库）**

liveness 若查数据库：数据库抖动 → liveness 失败 → 重启容器 → **但重启解决不了数据库问题**，  
反而把所有副本轮流重启，把「外部依赖故障」放大成「服务全面不可用」。  
外部依赖的检查属于 readiness（此时摘流量是对的 —— 确实处理不了请求）。

**复核条件**：应用启动变快（< 5s）后可考虑去掉 startupProbe；接入真实依赖后  
readiness 的 `failureThreshold` 可能要调大（避免依赖抖动导致频繁摘流量）。

---

## DEC-013 · 给应用加两个「默认关闭」的演示旋钮，而不是改测试专用镜像（2026-10-09 · W1 D5）

**背景**：D5 要验证三种探针**真的生效**。但原始应用启动 < 2s、且没有任何「会失败」的状态  
—— 探针永远不会失败，也就无法证明它有用。需要一个手段来制造故障。

**候选方案**

| 方案                              | 问题                                      |
| ------------------------------- | --------------------------------------- |
| A. 新建一个「故意会失败」的测试镜像             | 生产镜像与测试镜像分叉，测的不是同一个东西                   |
| B. 用 `kubectl exec` 进去 kill 主进程 | 只能演示 liveness，演示不了 readiness 的「摘流量但不重启」 |
| C. 改应用，加**默认关闭**的开关             | ✅ 采用                                    |

**决策**：给 `app/main.py` 加两个旋钮，**默认值保证生产行为完全不变**。

1. `STARTUP_DELAY_SECONDS`（默认 `0`）—— 模拟慢启动，让 startupProbe 有东西可保护。
2. `NOT_READY_FILE`（默认 `/tmp/not-ready`）—— 该文件存在时 `/ready` 返回 **503**。

**理由**

- **测的就是生产镜像本身**：开关关闭时行为与 v0.1.0 逐字节一致（已实测：默认导入耗时 < 0.1s）。
- **readiness 的演示必须能「不重启容器」**：方案 B 做不到，而 `/ready` 返回 503  
  正是 readiness 语义的**直接注入点** —— 改一个文件即可，容器毫不受影响。
- **默认值安全**：不设环境变量时两个旋钮都不激活，不存在「忘了关导致生产带延迟」的风险。

**⚠️ 设计细节：`/health` 刻意不看 `NOT_READY_FILE`**

如果 `/health` 也读这个文件，那 liveness 会跟着失败 → 容器被杀 ——  
那就把「暂时不该接流量」错误地升级成了「重启」。**两个端点必须反映不同语义**，  
这个刻意的不对称本身就是教学材料。

**实测结果**（`scripts/verify_d5.sh` 全绿）

| 场景                        | 结果                                                |
| ------------------------- | ------------------------------------------------- |
| 默认（不设变量）                  | `/health` 200、`/ready` 200，行为与 v0.1.0 一致          |
| 标记文件存在                    | `/ready` **200 → 503 → 200**；`/health` **全程 200** |
| `STARTUP_DELAY_SECONDS=6` | 端口延迟约 6s 才可访问                                     |

**复核条件**：W2 接入 pgvector 后，`/ready` 改为真实查数据库连通性，  
`NOT_READY_FILE` 这个旋钮可保留（做故障演练），也可删（用「停数据库」来演示）。

---

## DEC-014 · 排错演练用独立 Deployment，不污染主应用（2026-10-09 · W1 D5）

**背景**：D5 第 2 步要练「错镜像 tag / 内存 limit 过小」的排错。  
最直接的做法是改主应用 `llmops-api` 的清单，但那样会破坏已验证的状态。

**决策**：每次演练 `kubectl create deployment <d5-xxx>` 建**独立** Deployment，  
演练完 `kubectl delete`，主应用全程不动。

**三种演练的判据（实测值）**

| 故障            | 注入方式                       | 关键判据                                                        | 实测                                                                             |
| ------------- | -------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 镜像 tag 错      | `llmops-api:v9.9.9`        | `STATUS=ImagePullBackOff`                                   | ✅ events 里 `failed to resolve reference "docker.io/library/llmops-api:v9.9.9"` |
| 内存过小          | limit `32Mi`（实测占用 ~40MB）   | `lastState.reason=OOMKilled` + **退出码 137**                  | ✅ `137 = 128 + 9(SIGKILL)`                                                     |
| readiness 路径错 | `/readiness`（正确是 `/ready`） | `STATUS=Running` 但 `READY=0/1`、`RESTARTS=0`、EndpointSlice 空 | ✅ 事件 `statuscode: 404`                                                         |

**⚠️ 本地 kind 环境的一个坑（记录备查）**：`llmops-api` 是本地构建的镜像，  
只存在于 kind 节点里。tag 写错时，kubelet 会**跑去 Docker Hub** 找  
（错误信息是 `docker.io/library/llmops-api:v9.9.9`），  
**看起来像网络问题，实际是「本地根本没有这个 tag」** —— 别被错误信息带偏。

**复核条件**：接上真实镜像仓库（Harbor / ECR）后，tag 错的表现会变成 401/403（权限），  
届时判据需相应调整。

---

## DEC-015 · 用 Helm 做应用发布，而不是裸 YAML（2026-10-12 · W2 D6）

**背景**：W1 的应用清单是**一份裸 YAML**（`infra/k8s/app.yaml`），deploy / Service /  
ConfigMap / Secret 全在里面，值写死。要到 W2 起支持 dev / prod 双环境时，  
裸 YAML 只有两条路：复制成两份各改各的（改一处要记得改另一处），  
或者用 `envsubst` 之类在 apply 前替换（没有版本、没有回滚记录）。  
`helm rollback` 这类「一条命令退回上一版」的能力，裸 YAML 完全没有 ——  
W1 D5 的排错演练也印证了：出问题只能手动把旧文件重新 `apply` 一遍。

**决策**：W2 起所有 K8s 应用用 **Helm Chart** 管理，模板进 `templates/`，  
环境差异全部下沉到 `values.yaml`，并按环境拆 `values-dev.yaml` / `values-prod.yaml`。

**理由**

1. **一个应用 = 十几个资源的组合**（Deployment + Service + ConfigMap + Secret +  
   ServiceAccount…）。裸 YAML 跨环境只能复制粘贴改值，无版本、无回滚；  
   Helm 把它们打包成一个 Chart（模板 + values），**换一套 values 即出一套环境**。
2. **Release 自带版本历史**。每次 install / upgrade 生成递增 revision，  
   翻车一条 `helm rollback <rev>` 回退，且回滚本身也记为一个新 revision（可追溯）。
3. **与 W1 衔接零返工**。D4 的 Ingress / ConfigMap / Secret 结构原样搬进 Chart 模板，  
   `verify_*.sh` 的验收口径不变（探针路径、端口、环境变量语义全部保留）。


**实测证据（W2 D6 当天演练）**

| 环节             | 做法                                                        | 结果                                                                                        |
| -------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 裸 YAML → Chart | 改造 `helm create` 官方骨架，只改 3 个文件                            | `helm lint` **0 failed**；`helm template` 渲染 **6 个资源**                                     |
| 双环境实装          | `llmops-dev`(ns dev) + `llmops-prod`(ns prod)，各带一套 values | 双双 **REVISION 1 deployed**；`kubectl exec printenv` 验出 dev=`0.2.0-dev`/prod=`0.2.0`，环境隔离成立 |
| 故意发坏版本         | `--set readinessProbe.httpGet.path=/bad-path --wait`      | helm 退出码 1，REVISION 2 = **failed**                                                        |
| **回滚**         | `helm rollback llmops-dev 1`                              | `Rollback was a success!`，history 成 `1 deployed → 2 failed → 3 Rollback to 1`             |

**⭐ 最值得记的一条**：坏版本期间**服务零中断** —— Endpoints 始终只有旧 Pod，  
`/health` 连打 3 次全 200、`RESTARTS` 始终 0，两个 ReplicaSet 并存（旧 1 Ready / 新 0 Ready）。  
**readinessProbe 把「发布失败」挡在流量入口外，而不是等用户报障** ——  
这是 DEC-012「readiness 比 liveness 灵敏」的实战兑现。

**两条踩坑（备查）**

- 官方骨架的 `deployment.yaml` 写的是 `containerPort: {{ .Values.service.port }}`，  
  即**假设「容器端口 == Service 端口」**。本应用是 `Service 80 → 容器 8000`，  
  不拆出独立 `.Values.containerPort` 的话，流量会转到没人监听的 80 端口。
- Helm 的**列表值是整体替换**而非合并：`secret.requiredKeys` 这类清单，  
  若在 `values-secret.yaml` 里也写一份，会**覆盖**掉 `values-prod.yaml` 的清单，安全闸门形同虚设。

**复核条件**：若某应用**长期单环境**且资源 ≤3 个，退回裸 YAML（或 kustomize）更轻，  
不必硬套 Helm —— 上 Helm 的收益来自「多环境 × 多资源 × 需要回滚」。

---

## DEC-016 · 生产工作负载一律用专属 ServiceAccount + 最小权限 Role（2026-10-13 · W2 D7）

**背景**：DEC-007 要给应用配 ServiceAccount + Role。先问一句「它到底要调哪些 K8s API」——  
答案是**一个都不用**：`llmops-api` 是普通 HTTP 服务，不 list Pod、不写 ConfigMap。

那为什么还要折腾？因为**不给权限 ≠ 没有权限**：K8s 会自动把 ServiceAccount token  
挂进每个 Pod 的 `/var/run/secrets/kubernetes.io/serviceaccount/token`。  
应用一旦被 RCE（依赖漏洞、命令注入、SSRF 打到元数据），攻击者拿到的就是这枚 token。

**决策**

1. 每个工作负载用**自己的** ServiceAccount，不用 `default` —— 权限可按工作负载单独回收。
2. 用 **Role + RoleBinding**（不是 ClusterRole），作用域锁死在本命名空间。
3. 能不用 API 的，把 `automountServiceAccountToken` 显式关掉，**连 token 都不挂**。  
   （⚠️ 这里刻意不写成 `键: 值` 的形式 —— 仓库的脱敏门禁按「键=值」扫凭据，  
   会把任何 `SomeKey: someValue` 的行都拦下来，包括这个正经的 K8s 字段。）
4. 确实要读的，用 `resourceNames` 收窄到**具体对象**，而不是给整个资源类型。

本 Chart 的 `templates/rbac.yaml` 按 2 + 4 实现（为了把机制演示完整）：  
只给「读自己那一个 ConfigMap」+「读本命名空间 Pod」，其余一概不给。

**实测证据（W2 D7）**

`kubectl auth can-i --as=system:serviceaccount:dev:llmops-dev-llmops-chart -n dev`：

| 动作                               | 结果    | 说明                   |
| -------------------------------- | ----- | -------------------- |
| 读**自己那一个** ConfigMap             | ✅ yes | 由 `resourceNames` 放行 |
| 读**同命名空间另一个** ConfigMap          | ❌ no  | 同一条规则，仅因对象名不同即被拒     |
| 列出全部 ConfigMap / Secret          | ❌ no  | 没给 `list`            |
| 读 / 列出 Secret                    | ❌ no  | 完全没这条规则              |
| 读 / 列出 Pod                       | ✅ yes | 显式给的最小读权限            |
| 删除 Pod / exec 进 Pod              | ❌ no  |                      |
| 读 Deployment                     | ❌ no  |                      |
| **跨命名空间**读 ConfigMap / Pod（prod） | ❌ no  | Role 的命名空间边界         |

**容器内实证**（用 Pod 自己挂载的 token 直接打 API Server，比 `auth can-i` 更有说服力）：

| 请求                                                                     | 状态码     |
| ---------------------------------------------------------------------- | ------- |
| `GET /api/v1/namespaces/dev/configmaps/llmops-dev-llmops-chart-config` | **200** |
| `GET /api/v1/namespaces/dev/configmaps/kube-root-ca.crt`               | **403** |
| `GET /api/v1/namespaces/dev/configmaps`（list）                          | 403     |
| `GET /api/v1/namespaces/dev/secrets`                                   | 403     |
| `GET /api/v1/namespaces/dev/pods`                                      | 200     |
| `GET /apis/apps/v1/namespaces/dev/deployments`                         | 403     |
| `GET /api/v1/namespaces`                                               | 403     |

**三条容易写错的坑（实测）**

1. **`resourceNames` 与 `list`/`watch` 互斥**。它只对 `get`/`update`/`patch`/`delete` 生效；  
   规则里一旦加上 `list` 或 `watch`，`resourceNames` 会被**静默忽略**，  
   规则退化成「能读本命名空间**所有**该类型资源」—— 看着更严，实际更松，且不报错。
2. **「零 Role」≠「零权限」**。`auth can-i --list` 显示，即使一条规则都不给，  
   SA 仍自带一组集群级只读权限（`/api/*`、`/apis/*`、`/healthz`、`/version` 等），  
   来自内置的 `system:discovery` / `system:public-info-viewer`。  
   所以「最小权限」的准确说法是**收敛到内置基线**，不是收敛到零。
3. **RoleBinding 的 subject 要绑 SA，不是绑命名空间**。绑成 `Group` 或  
   `serviceaccounts` 全量，等于给该命名空间所有工作负载都发了同一套权限。

**复核条件**：若该应用将来真的需要调 API（如做成 operator、或要写 ConfigMap 做热更新），  
**按实际用到的资源逐个放开**，不要预留「以后可能用得上」的权限 ——  
预授权正是最小权限失效的头号原因。

---

## DEC-017 · HPA 的 CPU 目标值只有在 requests 贴近真实用量时才有意义（2026-10-13 · W2 D7）

**背景**：DEC-007 给 dev / prod 都开了 HPA（dev 1–5 @50%，prod 2–6 @60%）。  
压测时副本数一路顶到 `maxReplicas` 就不再动了 —— 但 CPU 利用率明明远高于目标值，  
按理应该继续扩才对。

**决策**：**HPA 的 `targetCPUUtilizationPercentage` 是「实际用量 ÷ requests」的百分比，  
不是「节点 CPU 的百分比」**。所以上 HPA 之前必须先校准 `requests.cpu`；  
requests 定得比真实用量低一个数量级时，HPA 会长期贴在 `maxReplicas`，等于没有扩缩能力。

**实测证据（W2 D7 压测）**

| 观测项                                  | 数值                                       |
| ------------------------------------ | ---------------------------------------- |
| `requests.cpu`（dev）                  | **50m**                                  |
| 压测中每个 Pod 实际 CPU（`kubectl top pods`） | **90m – 135m**                           |
| 单 Pod 利用率                            | 180% – 270%（= 实际 ÷ 50m）                  |
| HPA 目标                               | 50%（= 绝对阈值 25m）                          |
| 结果                                   | 2 → 4 → 5（贴 `maxReplicas`），CPU 仍停在 200%+ |

即使扩到 5 副本，**总 requests 也只有 250m**，而实际需求约 500m+ ——  
分母太小，利用率永远降不回 50%，HPA 只能一直加副本直到撞上限。

**顺带实测到的两条 HPA 行为**（官方文档只说结论，这里是现场数据）

- **扩容快、缩容慢**的由来：`scaleUp.stabilizationWindowSeconds` 默认 **0**，  
  `scaleDown` 默认 **300**。本次停压后 CPU 在 100 秒内就掉到 6%，  
  但副本数**死守 5 直到第 360 秒**，随后 5 → 2 → 1 分两步落回。
- 缩容取的是**过去 300 秒内建议值的最大值**（防抖动），  
  所以「CPU 已经很低」和「HPA 可以缩容」之间必然有 5 分钟左右的延迟。

**复核条件**：拿到真实用量后把 `requests.cpu` 调到接近 P95（本应用约 100m），  
再重跑一次压测确认副本数能停在中间档位而不是撞上限；  
DEC-011 做资源账本时一并把 limits 也校准一遍。

---

## DEC-018 · 上游重试用指数退避 + 抖动，且重试窗口只覆盖「首 token 之前」（2026-10-14 · W2 D8）

**背景**：D8 要把 `/chat` 做成「可上集群的 API」。上游模型会抖 —— 502 / 连接重置 /  
读超时都可能，直接透传给用户就是把上游的抖动放大成我们的不可用。  
但重试不是「失败就再来」：不设上限会放大雪崩，不区分错误类型会白烧额度，  
在流式场景下重试还会让用户看到答案从头重来。

**决策**

1. **重试只在「首 token 之前」**。流式响应一旦开始吐字就不再重试 ——  
   半截答案没法续，重发等于客户端看到答案从头发一遍，比断流更难排查。
2. **指数退避 1s→2s→4s…，封顶 8s**，每次再叠 0–0.5s **随机抖动**（防多实例同时重试 = 惊群）。
3. **共 3 次尝试（含首发）后放弃**，耗尽转 **502**（不是 500 —— 语义是「我们没问题，上游有问题」，  
   监控告警该分流）。
4. **只重试 `retryable` 错误**：连接失败 / 读超时 / 408 / 409 / 429 / 5xx → 重试；  
   400 / 401 / 403 / 404 → 不重试（请求或凭据有问题，重来一遍结果一样，只是白烧额度）。
5. **关掉 SDK 自带的重试**（`max_retries=0`）。

**理由**

- **为什么是指数退避而不是固定间隔**：上游抖动通常是短暂拥塞。固定 1s 重试会在拥塞未缓解时  
  再打一次、再打一次，等于给已经过载的上游加压；指数退避让重试间隔随失败次数拉开，  
  给上游恢复留时间。抖动是为了避免「所有实例同一秒重试」形成新的尖峰。
- **为什么上限是 3 次而不是更多**：这是「用户体验」与「成功率」的折中。  
  3 次尝试在指数退避下最长约等 1+2 = 3s（不含抖动），用户还能接受；  
  再多就要十几秒，用户早走了，而且失败概率的边际收益急剧下降。
- **为什么重试次数要写进响应头**（`X-Upstream-Retries`）：排障时一眼就能区分  
  「这次慢是因为重试了」还是「上游本来就慢」，不用翻日志。
- **为什么关 SDK 重试**：openai SDK 默认自己会重试，若不关，「3 次重试」会变成  
  3×3 次，上游一次抖动能把客户端拖死十几秒。重试策略只能有一层，且要显式。

**实测证据（W2 D8 本地验收）**

| 场景                              | 注入方式                                   | 结果                                              |
| ------------------------------- | -------------------------------------- | ----------------------------------------------- |
| 自动重试（2 次失败后成功）                  | `MOCK_FAIL_N_TIMES=2` + `LLM_MAX_ATTEMPTS=3` | 200，`attempts=3`、`retries=2`，响应头 `X-Upstream-Retries: 2` |
| 重试耗尽                            | `MOCK_FAIL_N_TIMES=99` + `LLM_MAX_ATTEMPTS=2` | **502**，body 带 `{message, upstream, status}`      |
| 指数退避生效                          | `LLM_BACKOFF_MIN=0.05` 观测两次退避间隔         | 日志 `0.1s 后重试` → `0.3s 后重试`（递增）                 |
| 错误分类                            | 构造 401 / 400 / 500 / 429 / 连接失败 / 读超时  | 4xx 凭据/请求错 `retryable=False`，其余 `True`           |

**三条踩坑（备查）**

1. **流式下「首 token 超时」≠「总超时」**。首 token 超时是「建连后一直不来数据」，  
   总超时是「整个响应拖太久」；混成一个值会把长回答误杀（首 token 正常但生成慢）。  
   本实现里重试只对首 token 之前的故障生效，天然避开了这个歧义。
2. **重试要显式关掉 SDK 的那一层**，否则两层叠加（见理由末条）。
3. **结构化输出（JSON mode）不可靠**：模型仍可能吐出带 markdown 围栏的文本。  
   本实现不假设一定能 parse —— 解析失败时保留原文 + 标 `parse_failed=true`，  
   让调用方看到现场，而不是静默返回 null。

**复核条件**：接入真实上游并积累一段 429 / 5xx 的实际发生率后，重新校准  
`LLM_MAX_ATTEMPTS` 与退避上限；若发现长回答因「总超时」被误杀，则把  
「首 token 超时」与「总超时」拆成两个独立参数。

---

## DEC-019 · 本机无 GPU 的现实：本地推理服务选型（2026-10-15 · W2 D10）

**背景**：D10 要给 `/chat` 配一条「自托管」上游通道，与托管 API 组成双通道，并作为 primary 重试耗尽后的 fallback 兜底。第一选择是 vLLM（LLMOps 事实标准：OpenAI 兼容、continuous batching、生态成熟），但本机**没有 GPU**，只能走 CPU 路径。

**决策**

1. **放弃 vLLM 的 CPU 镜像**（`vllm/vllm-openai-cpu:latest`，v0.31.0）—— 它在**本机 Docker Desktop + WSL2 环境下无法完成引擎 warmup**，会永久卡死。
2. **改用 Ollama** 提供本地 OpenAI 兼容端点（`http://localhost:11434/v1`）。
3. **fallback 的配置语义不变**：仍是 `LLM_FALLBACK_ENABLED / MODEL / API_KEY / BASE_URL`，只是 `BASE_URL` 从 vLLM 的 `:8000/v1` 换成 Ollama 的 `:11434/v1` —— 协议层零改动。

**实测证据（vLLM 为何被放弃）**

| 排查维度 | 尝试 | 结果 |
| --- | --- | --- |
| 计算量 | `--max-model-len` 2048 → 1024 → **256** | 全卡；256 的 prefill 计算量可忽略仍卡 → **排除算力** |
| CPU 核数 | auto（仅 3 核）→ `VLLM_CPU_OMP_THREADS_BIND=0-7` | 仍卡 |
| 内存上限 | 8G → 12G | 仍卡 |
| 共享内存 | `shm_size: 1gb`（容器内实测 1.0G 可用） | 仍卡 |
| 权限 | `privileged: true`（消除 `numa_*` EPERM） | 仍卡 |
| 引擎版本 | V1（默认）→ `VLLM_USE_V1=0` | 仍卡 |
| OpenMP | `KMP_DUPLICATE_LIB_OK=TRUE` | 仍卡 |
| 启动模式 | `--enforce-eager`（跳过 torch.compile） | 仍卡 |

**共同症状**：日志稳定停在 `Warming up model for the compilation...`，之后每 60 秒一条 `No available shared memory broadcast block found in 60 seconds`，`/v1/models` 永不返回 200。

**理由**

- **为什么不继续深挖 vLLM**：D10 时间盒 1.5h，8 类配置已一致指向「镜像与 WSL2 运行时不兼容」，再往下是内核/驱动级排查，与「学 LLMOps 工程流程」的目标错位。**任务卡本身就预判了这点**（"本机无 GPU，vLLM 可能根本起不来 —— 起不来就直接跳过，别在这耗时间"）。
- **为什么 Ollama 合适**：同样暴露 OpenAI 兼容的 `/v1/chat/completions`，`app/llm.py` 协议层一行不用改；CPU 上秒级启动、模型管理内置。代价是没有 continuous batching 这类服务端优化，但对「跑通双通道 + 拿延迟对比」够用。
- **为什么值得记进日志**：这不是「环境坏了」，而是**会反复出现的选型约束** —— 无 GPU 机器上「能跑什么推理服务」有明确边界，下次不必重走这 8 类排查。

**实测数据（Ollama · qwen2.5:0.5b · 生成 64 tok）**

| 并发 | p50 | p90 | RPS | tok/s |
| --- | --- | --- | --- | --- |
| 1 | 3.10s | 3.61s | 0.33 | 14.1 |
| 4 | 3.45s | 4.93s | 0.99 | 40.8 |

**读法**：CPU 上多核并行能把吞吐拉近 3 倍，但**绝对值**仍是「单机玩具级」—— 这正说明为什么生产环境的自托管推理必须配 GPU。

**复核条件**：换到有 GPU 的机器后，vLLM 应作为首选重新评估，并重跑双通道压测确认服务端优化带来的吞吐差异。

---

## DEC-020 · 为什么生产环境要 PDB 与反亲和（2026-10-15 · W2 D11）

**背景**
D11 要给 prod 做可用性收口。两个常被并列提起、但**管的是两件不同的事**的机制：PodDisruptionBudget（PDB）与 PodAntiAffinity（反亲和）。

**选项**

1. 只加 PDB —— 挡住 drain 时的批量驱逐
2. 只加反亲和 —— 让副本分散到不同节点
3. **两个都加，各管一段**

**选择**：方案 3

**理由**

- 先分清两类中断，这是本决策的核心：
  - **自愿中断**（voluntary）：`kubectl drain`、节点升级、集群缩容 —— 人/控制器**主动**发起，走 Eviction API。**PDB 管这段**。
  - **非自愿中断**（involuntary）：节点宕机、内核 panic、OOMKilled —— **PDB 管不了**，只能靠「多副本 + 反亲和」把风险摊到多个节点。
- 所以两者**互补而非重复**：PDB 保「主动操作时不断服」，反亲和保「被动故障时不团灭」。
- ⚠️ PDB 的两条易错语义：① 只拦 Eviction API，**拦不住 `kubectl delete pod`**；② selector 必须与 Deployment 一致，否则保护的是一个空集合。

**实测证据（本地 kind 单节点）**

| 机制 | 操作 | 结果 |
| --- | --- | --- |
| PDB | 2 副本、`minAvailable:1` 时驱逐 pod1 | **HTTP 201**（成功） |
| PDB | 立刻驱逐 pod2 | **HTTP 429** `Cannot evict pod as it would violate the pod's disruption budget` |
| PDB | 把副本缩到 1 | `Allowed disruptions: 1 → **0**`（任何驱逐都会被拒） |
| 反亲和 | `type: hard` + 单节点 + 删 pod 触发重调度 | 新副本 **Pending**：`0/1 nodes are available: 1 node(s) didn't match pod anti-affinity rules` |
| 反亲和 | `type: soft`（prod 默认） | 正常调度（单节点上"尽量分散"退化为"挤一起"，不阻塞） |

**代价**

- `minAvailable` 与副本数相等时，drain 会**永久卡住** —— 这是「保护强度」与「可运维性」的直接取舍。
- 反亲和 `hard` 在节点数不足时会让副本直接 Pending —— **反亲和依赖节点多样性**，单节点集群上 `hard` 是自伤。
- 本机 kind 只有 1 个节点，反亲和的**收益无法真实验证**（只能验证"配置正确 + hard 会 Pending"）；多节点集群才看得出分散效果。

**复核条件**
上多节点集群后重测反亲和的真实分散效果；prod 副本数变化时复核 PDB 口径（`minAvailable` vs `maxUnavailable`）。

---

## DEC-021 · dev / prod 隔离的边界在哪（2026-10-15 · W2 D11）

**背景**
D6 起用「同一份 Chart + 两套 values」做双环境。需要明确：**隔离到底隔了什么、又没隔什么**，否则容易误以为「有两个 Namespace 就等于生产级隔离」。

**选项**

1. 两个独立 Chart —— 彻底，但模板重复、修一处要改两遍
2. **同一 Chart + 两套 values + 两个 Namespace**
3. 两个独立集群 —— 隔离最强，本地做不到

**选择**：方案 2

**理由**
同一 Chart 保证「模板只有一份真相」，values 差异承载环境差异。实测确认隔离由四层共同构成：

| 层 | 机制 | 实测 |
| --- | --- | --- |
| 命名空间 | `dev` / `prod` 两个 ns | `kubectl get ns` 两行独立 |
| 对象命名 | `fullname = <release>-<chart>` | `llmops-dev-llmops-chart` vs `llmops-prod-llmops-chart` |
| **selector** | `app.kubernetes.io/instance: <Release.Name>` | **这是互不干扰的关键** —— 拿 dev 的 selector 去 prod ns 找 Pod = 0 个 |
| 配置 | ConfigMap / env 由 values 覆盖 | dev：`DEBUG`/`mock`/`log`/`ENABLE_LOAD_ENDPOINT=1`；prod：`INFO`/`openai`/`langfuse`/`ENABLE_LOAD_ENDPOINT=0` |

**不隔离的部分（必须知道）**：集群级资源是共用的 —— CRD、Ingress 控制器、StorageClass、metrics-server、镜像仓库。所以「dev 改坏了 CRD」会波及 prod。

**代价**

- ⚠️ Helm 的 values 合并是**浅合并 + 递归合并 map**，但**列表整体替换** —— dev 的 `env` 只写一项会让另一项消失，所以两套 values 的 `env` 都必须写全。
- 两套环境要**各自 upgrade**：本次就实测到 prod 落后 dev 4 个小版本（chart 0.2.0 vs 0.4.0，env 缺 `LLM_MODE`/`OBSERVABILITY`），是「多环境"验证"」该抓的第一类问题。
- 一个 release 的 values 漂移不会自动同步到另一个 —— 没有"继承"机制。

**复核条件**
上真实生产时，Namespace 隔离不够（需独立集群或至少独立节点池）；多租户场景还要补 ResourceQuota / LimitRange 做硬隔离（本次已给 dev 配了 quota 兜底，见 `infra/k8s/dev-quota.yaml`）。

---

## DEC-025 · 模型注册用 alias 不用 stage（2026-10-21 · W3 D15）

**背景**：D15 要把「可部署单元」注册进 Model Registry 并管理版本切换。MLflow 有两种指针机制：stage（Staging / Production 生命周期）与 alias（可任意指向某版本的标签）；stage 自 MLflow 2.9 起已 deprecated。

**选择**：用 alias（`production` / `canary` / `champion`），不建 stage。

**理由**：alias 是裸指针——回滚 = 把指针拨回去，金丝雀 = 建第二个指针，全部零代码零部署；stage 把「一条单向流水线」写死在工具里，想表达回滚只能再建一套流水线或手工改 stage。

**实证**：

| 场景 | 证据 |
| --- | --- |
| 课程侧 | `taxi-duration`：`champion` 拨 v1→v2→v1，`load_model("models:/taxi-duration@champion")` 全程一行代码；预测 [15.43, 6.49, 15.75, 21.68, 11.02] ↔ [15.33, 6.50, 15.20, 21.47, 10.79] 跟着变、再拨回 |
| 作品集侧 | `qa-combo`：`production`→v1（prompt v0）/ `canary`→v2（prompt v1）；`combos.py` 同一行 `load_combo`，alias 一拨，system_prompt 从「只输出 JSON」变成「资金与财务问答」 |

**代价**：alias 没有状态机——工具不阻止把 `production` 拨到未 READY 的版本；多 alias 语义靠团队约定。对策：切 alias 走 review / CI，权限与职责管控到人，禁止手工拨。

---

## 待记录（后续每天补充）

- [x] 为什么用 Helm 而不是裸 YAML（W2）→ 见 DEC-015
- [x] 为什么生产环境要最小权限（W2 D7）→ 见 DEC-016
- [x] 重试为什么用指数退避、重试几次就放弃（W2 D8）→ 见 DEC-018
- [x] 本机无 GPU 时本地推理怎么选（W2 D10）→ 见 DEC-019
- [x] 为什么生产环境要 PDB 与反亲和（W2 D11）→ 见 DEC-020
- [x] dev / prod 隔离的边界在哪（W2 D11）→ 见 DEC-021
- [x] 模型注册用 alias 不用 stage（W3 D15）→ 见 DEC-025
- [ ] 模型注册用 alias 不用 stage（W3 D15 · vault 骨架 DEC-025 已起草，结论待乐哥补写后回写本文件并勾选）
- [ ] 模型版本与 prompt 版本必须一起管（W3 D15 · vault 骨架 DEC-026 已起草，结论待乐哥补写后回写本文件并勾选）
- [ ] prompt 为什么是代码不是配置（W3 · 待记）
- [ ] 为什么先 RAG 而不是先微调（W7 · 待记）
- [ ] 语义缓存阈值怎么定（W7 · 待记）
