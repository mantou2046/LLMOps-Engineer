# W6 D4 评估报告 · RAGAS 四项基线（首份）

> 2026-11-06 提前实跑，对应任务卡 11/12。题库正本是 `evals/golden_set.jsonl`（50 题，12 道应拒答），**本次基线只跑了前 10 题**，不是全量。回答与检索上下文在 `evals/out/w6d4/responses.jsonl`，逐题分数在 `evals/out/w6d4/metrics.jsonl`，汇总在 `metrics.json`。`evals/out/` 被 gitignore，产物不进仓库，可按本报告末尾的命令复现。
>
> 本轮 D3 的复标验收未通过（`evals/ACCEPTANCE.md`），这份基线的检索两项要连着那条欠账一起读。

## 基线数字

**样本**：golden set 前 10 题（`g001`–`g010`），8 道 `single_hop` + 2 道 `multi_hop`，全是可答题。12 道应拒答与 3 道 `boundary` 不在这批里，下面的数字不能代表它们。生成模型 `deepseek-v4-flash`（AMD），裁判 `auto`。

| 指标 | 均值 | 有效题数 | 缺口 | 对照门禁阈值 |
| --- | --- | --- | --- | --- |
| `faithfulness` | **0.9714** | 10/10 | 0 | ≥ 0.80，过线 |
| `answer_relevancy` | **0.5817** | 10/10 | 0 | 无预设 |
| `context_recall` | **0.9200** | 10/10 | 0 | ≥ 0.75，过线 |
| `llm_context_precision_with_reference` | **0.8988** | 9/10 | 1（`g006`） | 无预设 |

阈值取 `.env` 的 `EVAL_PASS_THRESHOLD_FAITHFULNESS` 与 `EVAL_PASS_THRESHOLD_CONTEXT_RECALL`，是 11/14 接 CI 门禁要用的线，这里顺手对一下。

读完这张表，最先该问的是 **`answer_relevancy` 为什么只有 0.58**：生成端的提示词虽然要求「别加『根据资料』这类前缀」，实际回答里还是带了「根据资料」「块 1 和块 3」这些元信息，反推问题时就把它们算进去了；本地 `bge-small-zh-v1.5` 对中文短问句与长回答的余弦也整体偏低。这是四项里最低的，11/13 挑指标做改进实验时该从它入手。

检索两项则和 W6 D2 对得上：RRF 融合在这批题上把相关块捞回来且排得靠前（`context_recall` 0.92、`context_precision` 0.8988），`faithfulness` 0.9714 说明生成端基本只照着资料答、没怎么加料。四项的绝对数值仍受标准答案一致性影响（见局限 2），这里只看量级和组合关系。

`g006` 是跨文档的 `multi_hop`（要结合决策日志里的 ServiceAccount 与 K8s 概念笔记里的 RBAC 三层模型），`context precision` 补跑四次都在 ragas 内部 180 秒超时上失败。均值按 9 题算，没有拿别的题补位。

## 怎么跑出来的

链路分两段，都在 `scripts/` 下，不碰 `app/`：

1. `w6d4_rag_chain.py` 产出回答。语料读 `evals/corpus/` 六篇冻结快照（104,798 字符），切法沿用 W6 D1 选出的递归 B（400/50），共 330 块；检索沿用 W6 D2 的 bge-small-zh-v1.5 余弦 + jieba BM25、RRF（k=60）融合，取 top-5 块作为 `retrieved_contexts`。生成走本机网关的 `deepseek-v4-flash`，系统提示词只允许依据给定资料作答，资料没覆盖就明确拒答。
2. `w6d4_ragas_eval.py` 打分。四项指标用 ragas 0.4.3 的 legacy 类：Faithfulness、ResponseRelevancy、LLMContextRecall、LLMContextPrecisionWithReference；裁判走网关的 `auto` 路由（压测 15 次落到 GLM-5.2 / dots-3-note / qwen3.8-27b，均非 AMD），embedding 是本地 `models/bge-small-zh-v1.5`（512 维、L2 归一化）。分批跑、逐题落盘，超时由 `RunConfig` 退避，单题某项出分不全的记 NaN，重跑时只补缺的那个指标。

复跑命令：

```bash
cd /e/Projects/LLMOps-Engineer
./.venv-eval/Scripts/python.exe scripts/w6d4_rag_chain.py
./.venv-eval/Scripts/python.exe scripts/w6d4_ragas_eval.py --judge-model auto --batch-size 5 --workers 3 --batch-wait 5
```

## 四项指标分别量什么，低分该动哪一层

| 指标 | 口径 | 需要标准答案 | 低分意味着 | 该动哪一层 |
| --- | --- | --- | --- | --- |
| `faithfulness` | 回答里的断言，有多少条能在检索上下文里找到依据 | 不要 | 生成端加了资料里没有的内容，或者把上下文读歪了 | 生成：收紧引用约束、改 prompt、换模型；**不看答对没有** |
| `answer_relevancy` | 由回答反推一个问题，算它与原问题的 embedding 余弦 | 不要 | 答非所问，或者拒答太多、回答太绕 | 生成与检索排序都要查；拒答题会把这个数拉低，要分开看 |
| `context_recall` | 标准答案里的断言，有多少条在检索结果里找得到 | **要** | 该找的资料根本没捞回来 | 检索：切法、top-k、融合权重、召回池；这是唯一能直接判检索层的指标 |
| `llm_context_precision_with_reference` | 相关块在检索结果里排得有多靠前（带 reference 的正式口径） | **要** | 召回了一堆无关块，或相关块排在后面 | 检索排序：rerank、过滤、top-k；W6 D2 量过 rerank 的延迟代价，见 DEC-042 |

四项不是四个独立开关。`faithfulness` 高但 `context_recall` 低，是「检索没给料、模型只照着给的料老实说」；`context_recall` 高但 `faithfulness` 低，是「料捞对了、生成时掺了别的」。11/13 的归因实验按这个对照表挑最低的一项动手，一次只改一个变量。

## 分组读数

| 题型 | 题数 | faithfulness | answer relevancy | context recall | context precision |
| --- | --- | --- | --- | --- | --- |
| `single_hop` | 8 | 0.9643 | 0.5928 | 0.9000 | 0.9313 |
| `multi_hop` | 2 | 1.0000 | 0.5372 | 1.0000 | 0.6389（只有 1 题出分） |

`multi_hop` 只有 2 题、其中 1 题 `context precision` 还缺分，0.6389 这个数没有统计意义，只作线索：跨文档题的排序质量可能比单文档题差，值得在 50 题全量里单独看。

本批没覆盖 `refuse` 与 `boundary`。全量里要分开报：系统提示词要求「资料没覆盖就说没覆盖」，拒答题的回答断言极少，`faithfulness` 与 `answer_relevancy` 的分布和可答题不同，混在一起求均值会掩盖问题；`boundary` 三题的文档本来就只覆盖一部分，`context_recall` 天然偏低，属于题目设计而不是检索故障。

## 已知局限

1. **裁判是 `auto` 路由，实际模型不固定**。压测 15 次落到 GLM-5.2 / dots-3-note / qwen3.8-27b，同一题的不同断言可能由不同模型判，分数不完全可复现。生成端仍是 `deepseek-v4-flash`（AMD），两者不是同一个模型，避开了「自己评自己」的偏差，代价是裁判口径不统一。要更硬的结论，得固定一个裁判模型重跑。用户提到的 `deepseek-v4.1-flash` 在 catalog 里存在但状态是 `no_key`，没有配 key 用不了。
2. **标准答案的一致性没过验收**。D3 三轮复标 70.2%、三方全体一致 55.3%（`evals/ACCEPTANCE.md`）。依赖 `reference` 的 `context_recall` 与 `context_precision` 的绝对值要打折扣，适合看量级和同一次基线内的相对变化，不适合拿去跟别的项目比。
3. **免费池限流与超时**。首轮用 `deepseek-v4-flash`（AMD Radeon）当裁判，429 后要等 65–86 秒，还吃过一次 provider 504，10 题里大量指标变 NaN；换 `auto` 后不再有 429，但仍偶发 ragas 内部 TimeoutError。脚本按缺失指标补分，最终有效题数以 `metrics.json` 的 `valid` 为准，不拿其他题的分数补位。
4. **用的是 ragas legacy 指标**。0.4.3 的 collections 版要求 `InstructorBaseRagasLLM`，本次沿用 11-04 实测跑通的 `LangchainLLMWrapper` 路线，import 时有 DeprecationWarning。迁移到 collections 是后续事项，不改这次的数字。
5. **`embedding_factory("huggingface")` 不能直接喂给 legacy 指标**，它没有 `embed_query`，`answer_relevancy` 会报 AttributeError；必须再包一层 `LangchainEmbeddingsWrapper`。11-04 只测过 embedding 本身的维度与范数，没接到指标上，这次实跑才撞到。
6. `source_chunk` 仍 0/50，按递归 B 回填块号的脚本没写，所以 `IDBasedContextRecall` 这类不调 LLM 的省成本指标暂时接不上，11/14 接 CI 门禁前要补。

## 成本与限流

| 项 | 实测 |
| --- | --- |
| 生成 50 题回答 | 247 秒，中间一次 502 → 429，退避 60 秒后恢复 |
| `auto` 首轮评 10 题 | 859 秒（14 分 19 秒），3 次 ragas 内部 TimeoutError |
| 三轮补分 | 332 + 202 + 182 秒，最终 9/10 题四项齐全 |
| 评估合计 | 约 26 分钟，加生成约 30 分钟 |
| `auto` 压测（换裁判前） | 15 次连发 15/15 成功、零重试、平均 1.58 秒、总 23.6 秒 |
| 对照 `deepseek-v4-flash`（AMD） | 429 后 65–86 秒 cooldown，还吃过一次 provider 504，10 题里大量 NaN |
| 对照 `gemini-flash-lite` / `qwen3.8-flash-next` | 前者 12 次短重试全失败（109 秒），后者 12/12 但 73.7 秒 |

阅读清单 D4 的成本账估的是每题约 4–9 次拆断言/校验、50 题 700–1000 次 LLM 调用。按这次 10 题约 26 分钟外推，全量 50 题要两小时上下；瓶颈不在算力，是免费池的速率限制与偶发上游超时。11-04 记的「429 后约 86 秒恢复」在首轮完全应验，换 `auto` 路由后才绕开。

省法仍然成立：CI 门禁只抽样子集、检索两项优先用非 LLM 指标、judge 用便宜模型。这次按阅读清单的建议先跑小样，没有一口气上 50 题全量。
