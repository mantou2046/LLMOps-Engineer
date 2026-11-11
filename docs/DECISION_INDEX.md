# 技术决策清单

> **这是什么**：`docs/decisions.md` 的索引层。原始日志写「当时怎么想的」，这张表只回答面试会追问的三件事——**做了什么选择、为什么不是另一个、证据在哪**。
>
> **怎么用**：3 分钟讲架构时挑 8 条讲（下表标 ★ 的），其余留作追问时的底牌。每条都能指到本仓库里一份可复现的报告或一段代码。
>
> **口径**（先说清，免得被当成漏项）：
> - 决策日志共 **54 条**：编号 `DEC-005 ~ DEC-052` 有 **47 条**，另有 **7 条 2026-10-07/08 的早期决策没有编号**（编号体系是 10-09 才开始的，`DEC-001~004`、`DEC-008` 从未存在，不是丢了）。
> - **W7 四条线（护栏 / 成本 / 微调 / Agent）原先是真实缺口**，只留在 `evals/` 报告里；2026-11-11 已按 `decisions.md` 正式流程补成 **DEC-048 ~ DEC-052**（见第七节）。
> - 证据列只写**已经落库的东西**。没量过的项在复核条件里写明「未量」，不用形容词凑。

---

## 一、早期决策（2026-10-07 / 10-08 · 无编号）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| 早期-1 | 作品集选题：**资金 / 财务制度知识库问答**，不用通用文档问答 | 通用数据集和几百个同类项目没区别；业务域是我的差异化，且能自然长出 RAG + 评估 + 护栏 | `README.md` 问题陈述三句话 | 自造文档覆盖不了足够难例类型时，补文档而不放宽评估标准 |
| 早期-2 | 数据源**全部自造同构样例**，真实资料永不入库 | 真实制度有合规风险；自造反而能主动设计难例（条款冲突、跨文档引用、应拒答） | `data/` · `evals/corpus/`（104,798 字符冻结语料） | 数据源范围变更后重判「能不能公开」，提交层由脱敏门禁兜底 |
| 早期-3 | 仓库**公开** + 提交前脱敏门禁 | 私有等于作品集不存在；「先私有做完再转公开」会积压风险 | `scripts/check_sensitive.py`（占位符豁免，`0` 命中） | 真实资料要入库时，先过门禁再加白名单，白名单刻意写窄 |
| 早期-4 | 容器化：**多阶段构建 + 非 root（uid 1001）+ 固定 tag** | 单阶段镜像 1655.1 MB；root 跑容器等于把宿主机交出去 | `app/Dockerfile` · `README.md` 镜像体积对比（→ **129.9 MB，−92.2%**） | 换基础镜像或加系统依赖后重跑体积对比，别让「瘦」变成传说 |
| 早期-5 | 本地开发用 **compose** 起步，W2 再迁 K8s | 单机多容器 compose 就够；一上来 K8s 会把「学容器」变成「学排错」 | `infra/compose.yml` | 需要多副本 / 探针 / Ingress 时迁集群（W2 已发生） |
| 早期-6 | 数据库初始化用 **initdb 脚本**，不在应用里建表 | 建表是数据库级的关注点，应用启动时建表会和并发启动打架 | `infra/initdb/` | 需要版本化迁移时换迁移工具（W3 起已用 MLflow 侧 alembic） |
| 早期-7 | 脱敏门禁加**占位符豁免**，不放宽规则 | 真凭据有可判别形态；放宽规则等于门禁失效 | `scripts/check_sensitive.py` · `scripts/sensitive_terms.example.txt` | 新增凭据类型时补形态规则，不加白名单放行 |

## 二、W1–W2 · 地基与生产化（DEC-005 ~ DEC-021）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-005 | 基础镜像选 **alpine（musl）**，不用 slim-bookworm（glibc） | 净省约 97 MB 基础层；仍带 `sh` 与 `apk` 能装依赖 | `app/Dockerfile` · `README.md` §多阶段省在哪 | 出现只有 glibc wheel 的依赖时，退回 slim 并重记体积 |
| DEC-006 ★ | 本地集群选 **kind**，不用 minikube | 复用已有 Docker / WSL2，不引入 hypervisor；`Ready after 12s`；与 CI 同套命令 | `infra/k8s/kind-ingress-config.yaml` · `scripts/setup_ingress_kind.sh` | 需要多节点拓扑或一键装 addon 时重评 minikube |
| DEC-007 | 镜像用**具体 tag + `IfNotPresent`**，不用 `latest` + `Always` | `latest` 让「跑的是哪版」不可知，构建不可重现 | `infra/k8s/app.yaml` | 接真实 registry（私有 + `imagePullSecrets`）时，改用 digest 钉死 |
| DEC-009 | multi-arch 镜像用 **`ctr import --platform`**，不用 `kind load` | `kind load` 只解当前架构层；`ctr` 路径不依赖节点联网 | `scripts/load_image_to_kind.sh` | 建集群时清代理变量、镜像只推 amd64，则可回到 `kind load` |
| DEC-010 | Secret 清单里只放 **`<PLACEHOLDER:...>`**，真值由 `.env` 注入 | 密码入库即泄露；占位符比 `REPLACE_ME` 可判别、能进门禁豁免 | `infra/k8s/config.yaml` · `scripts/gen_values_secret.py` | 接 Vault / External Secrets 后可去掉占位符方案 |
| DEC-011 | kind 装 ingress-nginx：**本地清单 + 去 digest 钉死 + 清节点 containerd 代理** | 官方 manifest 依赖联网拉镜像，本机有代理会挂 | `infra/k8s/ingress-nginx/` · `scripts/setup_ingress_kind.sh`（三条 Host 均 200） | 环境不设全局代理，或换 `cloud-provider-kind` |
| DEC-012 | 三种探针**分工且参数刻意不等**：readiness 比 liveness 灵敏一个数量级 | 三组参数写成一样，等于用一种失败模式应付三种 | `infra/k8s/app.yaml` · `scripts/verify_d5.sh` | 启动变快（<5s）可去 startupProbe；接真实依赖后改为真查连通性 |
| DEC-013 | 加两个**默认关闭**的演示旋钮，不改测试专用镜像 | 测的必须是生产镜像本身；默认值保证生产行为不变 | `app/main.py`（`/ready` 与慢启动旋钮） | W2 接 pgvector 后 `/ready` 改真实查库（已发生） |
| DEC-014 | 排错演练用**独立 Deployment**，不污染主应用 | 演练要删 Pod、灌错镜像，动静大；主应用是演示资产 | `scripts/verify_d5.sh` | 接真实 registry 后，tag 错的表现变成 401/403，演练脚本要跟着改 |
| DEC-015 ★ | 应用发布用 **Helm**，不用裸 YAML | 一个应用 = 十几个资源；Release 自带 revision 历史，回滚一条命令 | `infra/k8s/llmops-chart/`（dev 1 / prod 2） | 长期单环境且资源 ≤3 个时，裸 YAML 或 kustomize 更轻 |
| DEC-016 | 生产工作负载一律**专属 ServiceAccount + 最小权限 Role** | 默认 SA 挂着多余令牌；权限跟着工作负载走才可审计 | `infra/k8s/llmops-chart/templates/` | 应用真需要调 API（operator / 热更新 ConfigMap）时再扩权限 |
| DEC-017 | HPA 的 CPU 目标**只在 requests 贴近真实用量时**才有意义 | `targetCPU` 是「用量 ÷ requests」；requests 虚高会让 HPA 永不扩容 | `infra/k8s/hpa-demo/` · `scripts/bench_hpa.py` | 拿到真实用量后把 `requests.cpu` 调到接近 P95 再定目标值 |
| DEC-018 | 上游重试用**指数退避 + 抖动**，窗口只覆盖「首 token 之前」 | 固定间隔会同步重试打爆上游；首 token 之后重试会把半句话重说一遍 | `app/llm.py`（`X-Upstream-Retries` 回显） | 积累真实 429 / 5xx 发生率后重新校准次数与上限 |
| DEC-019 | 本机无 GPU：本地推理选 **Ollama**，不深挖 vLLM | vLLM 的吞吐优势在 CPU 上测不出来，学的是参数不是结论 | `infra/ollama/` · `scripts/bench_ollama.py` | 换 GPU 机器后 vLLM 作为首选重评，并重跑双通道压测 |
| DEC-020 | 生产要 **PDB + 反亲和** | 先分清自愿 / 非自愿中断：PDB 管自愿，反亲和管理单点 | `infra/k8s/llmops-chart/`（PDB + topologySpread） | 单节点集群上 PDB 只是声明，多节点后重验驱逐行为 |
| DEC-021 | dev / prod 隔离到**命名空间 + 配额**，不止是换个名字 | 同 ns 双份资源会互相抢配额、NetworkPolicy 也分不开 | `infra/k8s/dev-quota.yaml` · `infra/k8s/llmops-chart/values*.yaml` | 上真实多租户后重判隔离边界（网络策略 / 镜像仓库分开） |

## 三、W3 · 实验追踪与版本（DEC-022 ~ DEC-028）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-022 | 成熟度自评 **L1 → 目标 L3**，L4 不做 | L4（全自动再训练）没有真实回流数据，做了也是演示 | `README.md` 进度表 · 库内 W3 复盘 | W3 追踪 + registry 落地、W6–W7 有回流后重评 |
| DEC-023 ★ | 实验记录**最小充分集五件套**：git_sha + 参数 + 指标 + 产物 + 评估集版本 | 少一件就只是日志：没有 eval_set_version 的指标不可比 | `scripts/track_prompt_run.py` · MLflow `qa-prompt-eval` 两 run 血缘四项对齐 | 换评估集或换模型后，血缘字段必须同步扩，不能只记指标 |
| DEC-024 ★ | **MLflow 记实验汇总，Langfuse 记请求链路**，靠 prompt_version / run_id 串 | 两者不互相替代：一个答「哪版更好」，一个答「这次为什么慢 / 贵 / 错」 | `app/observability.py` · `infra/langfuse/` · `scripts/verify_langfuse_trace.py` | 引入第三方评测平台时，先定它归哪一类，别造第三套真相 |
| DEC-025 | 模型注册用 **alias**（production / canary），不用废弃的 stage | stage 自 MLflow 2.9 废弃；alias 是一等公民且可多标签 | `scripts/register_combo.py`（`taxi-duration` v3/v4、`qa-combo` v1/v2 来回拨，代码零改） | 需要灰度比例控制时，在 alias 之上加流量权重，而非回到 stage |
| DEC-026 | 模型版本与 prompt 版本**绑成一个 Registry 版本** | 只升模型不记 prompt，等于把两个变量混成一个结论 | `scripts/register_combo.py`（prompt 从 run artifact 取 + sha256 校验后注册） | prompt 结构变化（新增变量）时重建 combo，不原地改版本 |
| DEC-027 ★ | prompt **以 git 为真源**，registry 只做发布指针 | 正文存 registry 会分叉；git 有 diff / review / 回滚 | `prompts/structured_json/v1.yaml`（正文按 `prompts/<name>/v<N>.yaml` 存放）· `scripts/register_prompts.py` · `app/prompts.py`（`X-Prompt-Version` 回显，hash 对账 `d7b79571` / `009c95a5`） | 出现非工程角色要改 prompt 时，再评估是否需要小型管理界面（仍以 git 为真源） |
| DEC-028 | MLflow **独立 chart + 独立 ns**，日常走本地 / 演示走集群 | 塞进应用 chart 会连带重启，HPA 语义也错配；同 ns 撞 NetworkPolicy | `infra/k8s/mlflow/`（1/1 Running、PVC 2Gi Bound、`mlflow-artifacts:/1`、Pod 重启后 artifact 仍在） | 演示频次低于每月一次时，可以只留本地，删集群侧 |

## 四、W4 · 编排与流量（DEC-029 ~ DEC-033）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-029 ★ | **只有 RAG 索引重建上编排器（Prefect）**，训练 / 评估 / 打分先写可参数化脚本 | 编排器解决「定时 / 回填 / 定位哪一步挂」，没有这三类需求时它只是多一层 | `infra/` Prefect flow（4 task，flow **79.51 s**，`train_model` 78.19 s 占 98.3%；失败 3 次尝试 1s→3s 后定位到具体步） | 出现任一条就重评：脚本要无人值守定时、需要回填历史批次、排查 10 分钟还定位不到失败步 |
| DEC-030 | 形态选**在线为主 + 离线为辅**，明确不做流式 | 流式要改客户端与指标口径，而本项目的价值在「答得对 + 可追溯」，不在首字延迟 | 部署三形态实测（在线 p50 15 ms / 离线 7.4 万行 0.64 s） | 出现真实多轮对话体验诉求时，再补流式并同步改指标与护栏 |
| DEC-031 ★ | **并发买吞吐不买降本**：批量默认 8 并发 | 并发不减少 token；把并发当降本会在账单上翻车 | `evals/batch_score.py`（8 并发 **1.05 s** vs 串行 **8.08 s ≈ 7.7×**，token 四组 **4810 / $0.001261** 一位不变） | 上游限流阈值变化时重测最优并发（当前 8 未触发限流） |
| DEC-032 ★ | 网关**三档别名 + 精确缓存**，语义缓存暂缓，`similarity_threshold: 0.85` 标「暂定无实测」 | 语义缓存三个前置条件本机全缺（无 embedding 端点 / 无 RediSearch / 无 redisvl），写个阈值等于编数 | `infra/litellm/config.yaml` · 故障转移 **200** vs 去 fallback 的 **5xx**；精确缓存 **1161.6 → 10.3 → 8.9 ms**（响应 id 相同） | 具备 embedding 端点 + 向量缓存索引后，先量相似度分布再定阈值 |
| DEC-033 ★ | **分流用规则不用 LLM**，重试与降级只留网关一层 | LLM 判路由 0.5–2 s，规则 4.8–6.0 µs，差五个数量级；两层重试会把 1 次失败放大成 N 次 | `app/routing.py` · 离线 24 条 **12/12** 与标注 0 条不符 · 在线 18 请求 **18/18** + 缓存 6/6 命中 | 规则误判率随语料漂移上升（>10%）时，再考虑小模型判档并保留规则兜底 |

## 五、W5 · 监控、工程规范与 IaC（DEC-034 ~ DEC-038）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-034 ★ | 看板**只画能回答问题的图**：8 块面板只 4 块是数字，第一块必须是 `up` | 面板堆满会让人以为在监控；无 5xx 时错误率查询返回空集，靠 `noValue` 显示 0 不等于真 0 | `infra/grafana/dashboards/`（P95 `/chat` **4.75 ms**、QPS **0.212 → 0.929**） | 加面板前先答「它回答哪个问题」，答不出就不加 |
| DEC-035 | LLM 指标**只从网关取**，且**分母比分子重要** | 应用层看不见后面调了几次模型、命中没命中缓存；`cache_misses` 只记一部分，教科书公式算出 50% 而真实 11.6% | `app/metrics.py` · `scripts/fake_upstream.py` + `scripts/gateway_load.py`（17 请求 = 12 真调用 + 2 缓存 + 3 打不通，与上游 `/stats` 独立对账） | 网关换实现（非 LiteLLM）时，先确认它能提供同口径的分母 |
| DEC-036 | 单测**锁纯函数**、集成测走 **mock HTTP**、lint 只留 ruff | 真调模型进 pytest = 又慢又不稳，红了也分不清是模型还是代码 | `tests/`（179 项全绿）· `pyproject.toml`（ruff 配置） | 接入真实检索后补 `tests/test_retrieval_probes.py` 类集成测（已发生） |
| DEC-037 | CI 三件事：**同一条检查搬上云 / 测试全 mock / push registry 留台阶** | 云上跑真模型会被限流搞成假红；不推 registry 则「构建成功」没闭环 | `.github/workflows/`（四 job 全绿，曾真抓到 alpine 装不上 sklearn） | 有稳定额度或私有 registry 时打开 push 开关补「推 + 拉」闭环 |
| DEC-038 | 本地 IaC **另起全新环境**，管集群 + 数据库两类资源 | 让 Terraform 接管已有手搓环境，状态会对不上；范围铺太大则永远 apply 不完 | `infra/terraform/`（7 资源，destroy → apply 验收） | kind 修好 `save/import` 兼容（#3795）后改回 `kind_load` 资源；上云时同结构换 provider |

## 六、W6 · RAG 与评估（DEC-039 ~ DEC-047）

| 编号 | 决策 | 为什么不是另一个 | 证据（路径 + 读数） | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-039 ★ | 切块选**递归 B（400/50）**，中文按标点切不按字符硬切 | 512 硬切块尾完整率仅 **7.2%**，按标点切到 **42.8%**；overlap 25% 是白付成本 | `scripts/w6d1_chunk_bench.py` · 语料冻结 `evals/corpus/`（104,798 字符） | 换语料（条款结构不同）后重跑五种切法，别默认 400/50 |
| DEC-040 ★ | 混合检索**先上 RRF**，验收看 **hit@1 / MRR**，不看 hit@5 | 单路向量 hit@1 只 7/10；hit@5 太松看不出融合价值；两路分数量纲不同不能直接加权 | `scripts/w6d2_hybrid_bench.py`（hit@1 **7/10 → 10/10**、MRR **0.825 → 1.0**） | 换语料或换 embedding 后重跑 hit@1；k=60 可随候选规模调 |
| DEC-041 ★ | 灌库后**必须显式设 `ivfflat.probes`**，否则离线基线搬到生产会掉分 | 默认 `probes=1` 只探 1 个聚类，三次掉成 9/10、9/10、7/10，且漏题随索引重建漂移 | `app/retrieval.py`（统一连接入口固定 `probes = 3`）· `scripts/w6d1_verify_pgvector.py` | 换语料 / 换模型 / 行数变化后重跑三组对照；精确检索与 probes=3 分歧时先查索引是否重建 |
| DEC-042 ★ | 生产链路 RRF 已满分，**rerank 默认不开** | 精排 top-5 净增 **2.2–2.4 s**（约 530–620 倍延迟）换不到 hit@1 提升；50 题 CP 复核 Δ **+0.0608** 落在 σ 0.2399 内 | `scripts/w6d2_rerank_recheck.py` · `evals/RERANK_RECHECK.md`（CP 0.7843 → 0.8451，配对 Δ 不显著） | 换更大语料或需要 top-10 长上下文时重评；判据用带 reference 的 context precision |
| DEC-043 | 复标没过线就**留欠账**，不降标准也不冒充真人双标 | 事后把验收线从「严格一致」换成软指标 = 挑能过的数；降标准会让评估体系失去意义 | `evals/ACCEPTANCE.md` · `evals/ANNOTATION.md`（三轮 55.3% → 68.1% → 70.2%） | 换两名真人按第三轮规范独立重标，或走正式口径变更（DEC-047 即此路） |
| DEC-044 | 首份基线用**带 reference 的正式口径**，不拿免 LLM 近似指标凑数 | 没有 reference 的 context utilization 量的是「块有没有被用到」，不等于检索对不对 | `evals/RAGAS_BASELINE.md`（10 题小样 0.9714 / 0.5817 / 0.9200 / 0.8988；全量 50 题 0.9434 / 0.4768 / 0.8113 / 0.8591） | 换裁判模型时量化同模型自评抬高幅度；D3 收口后按最终标准答案重算检索两项 |
| DEC-045 ★ | **先量噪声再谈提升**，改动无效就不并入生产链路 | 配对 Δ **+0.0046** 小于不该变的 context recall 噪声标尺 **+0.0074**，且 faithfulness 反降 0.1214 | `evals/ATTRIBUTION.md` · `scripts/w6d5_attribution.py`（逐题 5 涨 5 跌，σ 0.3718） | 配到独立固定裁判、50 题全量、按题型分组后重跑；变化仍小于噪声就维持不改 |
| DEC-046 ★ | CI 门禁**只用非 LLM 指标**，全量 LLM 评估留本地 | 上游限流会让 CI 红成与代码无关的假红；太松的闸门又拦不住检索退化 | `evals/CI_EVAL_GATE.md` · `scripts/w6d6_eval_gate.py`（20 题分层抽样、字符 bigram BM25、`IDBasedContextRecall@5` **0.75**、阈值 **0.65**；改坏 query 模板塌到 **0.0** 且退出 1） | 有稳定额度后补一个小的 LLM-as-judge 子集 job；改切法或语料后重定基线 |
| DEC-047 | D3 验收**改双指标**（平均 Jaccard ≥0.8 且严格一致 ≥0.65），第三轮 0.816 / 70.2% 双项过线 | 换两名真人不现实；继续 AI 复标边际收益已消失（+2.1pp 且 Jaccard 反降）；静默把线降到 0.70 是 DEC-043 点名的反模式 | `evals/ACCEPTANCE.md` §口径变更与验收结论 · `evals/ANNOTATION.md`（κ 线一并废止） | 换真人双标后才可回到原线；口径变更留痕，不回溯改三轮旧数字 |

## 七、W7 · 护栏、成本、微调与 Agent（DEC-048 ~ DEC-052）

W7 四条线的实测原先只留在 `evals/` 报告里，**2026-11-11 补成正式决策条目**，编号接在 DEC-047 之后：

| 编号 | 决策 | 为什么不是另一个 | 证据 | 复核条件 |
| --- | --- | --- | --- | --- |
| DEC-048 | 输入护栏**规则先挡 + 模型判定补残余**，不做纯规则 | 纯规则漏 `A10`（「把这张表所有字段导出来」措辞是业务问句），纯模型 0.5–2 s 且不可解释 | `app/input_guard.py` · `scripts/w7d1_input_guard_demo.py`（10 条攻击拦 **9/10**、正常 5 条误拦 0、PII 遮盖 2/2） | 攻击面变化（多语言 / 编码绕过 / 分段注入）后重测规则覆盖率；模型层需补批量量化 |
| DEC-049 | 格式错**最多修一次**，仍不合格走**固定兜底** | 无限重试把一次坏输出放大成 N 次调用；直接 500 让模型抖一下变成服务不可用 | `app/output_guard.py` · `evals/OUTPUT_GUARD_BENCH.md`（30 条误拦 0 / 漏拦 0、资料依据 10/10；解析失败 **16 → 0**；P50 1.849 s / P95 10.004 s；一次结构化回答 **+513 tokens**） | 真实流量分布下的误拦率与 20 s 超时 fail-closed 比例（当前 **2/30**）必须先量，再决定开关默认值 |
| DEC-050 | 精确缓存 + 规则分流，**语义缓存继续暂缓**（延续 DEC-032） | 同条件四组只改这两件事：**1754.6 → 626.5（−64.3%）**；语义缓存三前置仍缺，阈值写了也是编数 | `app/cost.py` · `scripts/w7d3_cost_bench.py` · `evals/COST_BENCH.md`（只开分流 −61.5% / 只开缓存 −27.8%、命中率 28.4%、便宜档 69.7%、分流一致 20/20） | 点数是演示单位、非模型成本未计入；真实账单口径下重算，重复率按流量日志重取 |
| DEC-051 | **先 RAG 后微调**，微调只当候选；本机演示数学、真训练上云单次代跑 | 40 条合成样本只能证明格式行为可学，不能证明业务收益；单次代跑不可复现 | `app/lora_demo.py` · `scripts/w7d4_lora_demo.py` · `evals/LORA_DEMO.md`（本机 r=4 参数 **12.5%**、留出 MSE **0.000106** vs r=1 的 **190.111**；云端 Qwen2.5-0.5B + r=8、可训练 **0.1%**、留出 20 条 **0% → 100%**） | 真实任务上的单变量 A/B 与评估集齐备后，才决定是否上微调；`E-FT-2` 维持「业务效果未量」 |
| DEC-052 | 真 **LangGraph（StateGraph + ToolNode + InMemorySaver）**，每步工具调用发 OTel span | 零依赖版能跑但非框架，讲不清「换框架能带来什么」；checkpointer 用 InMemory 是**已知边界**不是选型终点 | `app/agent_graph.py` · `scripts/w7d6_langgraph_demo.py` · `evals/AGENT_GRAPH.md`（8 场景 **24/24**、四类结束原因全覆盖、根 span + 3 条 `agent.tool.*` 子 span、续跑 completed/steps=3） | 需要跨进程 / 跨重启续跑时换持久化 checkpointer（Postgres / Redis）；规划器换模型规划前需重测规划准确率 |

---

## 八、怎么讲（3 分钟版挑选顺序）

面试只讲这 8 条（表内 ★），按「一条链路一个决策」组织：

1. **入口到应用** → DEC-011（ingress-nginx，三条 Host 各 200）+ DEC-015（Helm，回滚一条命令）
2. **检索** → DEC-039（递归 B：硬切块尾完整率 7.2% vs 42.8%）+ DEC-040（RRF：hit@1 7/10 → 10/10）+ DEC-041（probes=3，否则生产掉分）
3. **生成与降级** → DEC-033（规则判档 4.8–6.0 µs vs LLM 0.5–2 s）+ DEC-032（精确缓存 1161.6 → 8.9 ms，语义缓存暂缓）
4. **评估** → DEC-046（CI 只用非 LLM 指标，改坏模板塌到 0.0）+ DEC-045（Δ 0.0046 < 噪声 0.0074，改动不采纳）
5. **成本** → DEC-031（并发 7.7× 只买时间，token 一位不变）
6. **可观测** → DEC-024（MLflow 管实验、Langfuse 管链路，靠 prompt_version 串）
7. **W7 五条（追问才展开，别主动铺）** → DEC-048（规则 9/10 + 模型补 A10）+ DEC-049（解析失败 16 → 0、P50 1.849 s）+ DEC-050（−64.3%，但比单项只多 7 点）+ DEC-051（0% → 100% 是格式不是业务）+ DEC-052（8 场景 24/24、span 可回查）

被追问时的三句底线：

- 「这条证据在本仓库哪？」——每条都能指到 `evals/` 报告或 `scripts/` 脚本，且脚本能重跑。
- 「为什么不是另一个？」——表里第三列就是答案，都是当时真实对比过的备选，不是事后补的理由。
- 「什么情况下要推翻？」——第五列写的是复核触发条件，不是免责声明。

## 相关

- 原始日志：[`docs/decisions.md`](decisions.md)（54 条全文，含完整选项与代价）
- 架构图终稿：[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)（本清单里 ★ 的决策在图上标注为 D1–D8）
- 三线验收：[`evals/ACCEPTANCE_W7.md`](../evals/ACCEPTANCE_W7.md) · [`evals/ACCEPTANCE.md`](../evals/ACCEPTANCE.md)
