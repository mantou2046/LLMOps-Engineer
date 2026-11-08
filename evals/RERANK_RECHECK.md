# W6 D2 复核报告 · rerank 该不该开（2026-11-08）

> 兑现 DEC-042 的复核条件：「11/11 建好 50 题 golden set 后，用同一套 RRF 候选重跑，判据换成标准答案与 context precision」。
> 本报告只回答一件事：换上能判语义排序的尺子之后，rerank 值不值得开。
> 脚本 `scripts/w6d2_rerank_recheck.py`，数字 `evals/out/w6d2/rerank_recheck.json`（`evals/out/` 被 gitignore，可复跑）。

## 怎么保证只改一个变量

- **样本**：从 `evals/golden_set.jsonl` 分层抽 20 道可答题 —— `single_hop` 11 / `multi_hop` 3 / `instruction` 3 / `boundary` 3。排除 12 道 `refuse`，它们的 `reference` 写的是期望行为而不是事实答案，CP 不适用。
- **候选池**：离线重建 D4 那条链路（递归 B 400/50 → 330 块 → bge-small-zh-v1.5 余弦 + jieba BM25 → RRF k=60），取融合后 top-10 交精排。
- **基线**：RRF top-5，直接对齐 `evals/out/w6d4/responses.jsonl` 里现成的 `retrieved_contexts`。脚本逐字校验 **20/20 一致**，不一致就退出，避免多出一个变量。
- **实验组**：`BAAI/bge-reranker-base` 对每题 top-10 打分重排，取 top-5。回答、`reference`、检索块内容、生成模型全部不动。
- **判据**：主判据 `LLMContextPrecisionWithReference`（ragas 0.4.3 legacy，带 `reference`，独立裁判 `glm-5.3-flash`，40 个样本全部出分）；旁证是零 LLM 的 `reference_atoms` 逐字覆盖率；延迟用本次实测 P50/P95。

## 数字

| 项 | RRF top-5 | rerank 后 top-5 | 差 |
| --- | --- | --- | --- |
| context precision（20 题均值） | 0.7843 | 0.8451 | **+0.0608** |
| 配对 Δ 标准差 | — | — | **0.2399**（7 涨 / 5 跌 / 8 平） |
| atoms 覆盖率（确定性） | 0.7100 | 0.7783 | +0.0683 |
| 精排延迟（top-10 候选） | — | P50 **4545 ms** / P95 5193 ms | D2 向量查询约 4 ms |

分组读数：

| 题型 | 题数 | CP RRF → rerank | atoms RRF → rerank |
| --- | --- | --- | --- |
| `single_hop` | 11 | 0.9384 → 0.9655 | 0.818 → 0.909 |
| `multi_hop` | 3 | 0.7769 → 0.7773 | 0.400 → 0.689 |
| `instruction` | 3 | 0.2333 → 0.5000 | **0.667 → 0.333** |
| `boundary` | 3 | 0.7778 → 0.8167 | 0.667 → 0.833 |

逐题最大涨 `g027`（`boundary`）CP +0.6667，最大跌 `g038`（`boundary`）−0.5；`g022`（`instruction`）CP +0.3 但 atoms 从 1.0 掉到 0.0，两个判据在这组上反向。

## 结论

**DEC-042「默认不开 rerank」维持，但理由升级了**：原来的理由是「关键词判据量不了语义排序，现在判不了」；现在有正式 CP 判据，实测均值 +0.0608 落在 σ 0.2399 的逐题波动里（8 题持平、5 题下降），`instruction` 组两个判据还互相打架，20 题样本也撑不起「提升」的说法。代价是每查询净增约 4.5 秒（P95 5.2 秒），比 D2 测的 top-5 精排 2.2–2.4 秒更慢（这次候选是 top-10）。收益不稳、代价确定，性价比不成立。

有一条值得记下的正向线索：`single_hop` 11 题的 CP 与 atoms 同向小幅上升，`multi_hop` 的 atoms 覆盖率 0.400 → 0.689 但 CP 没动。这说明 rerank 可能确实把相关块捞到了更靠前的位置，只是当前判据和样本量分辨不出来。要判得更硬，得扩到 50 题全量、固定裁判重复多次，并把 `instruction` 这类题的 `reference_atoms` 口径先对齐。

**边界**：本次是 20 题分层抽样、只取可答题、top-10 候选、单一裁判单次评分；`instruction` / `multi_hop` / `boundary` 各只有 3 题，分组数字只作线索，不作统计结论。CP 的绝对值受 D3 复标验收未过影响（`evals/ACCEPTANCE.md`），但本次只看同一裁判下的配对变化。

## 复跑

```bash
export RAGAS_JUDGE_BASE_URL=https://api.experientiallabs.ai/v1
export LLM_PRIMARY_API_KEY=<本人填入>
./.venv-eval/Scripts/python.exe scripts/w6d2_rerank_recheck.py \
    --judge glm-5.3-flash --batch-size 4 --candidates 10
```

换裁判前先跑 `scripts/w6d4_judge_preflight.py`（退出码 0 才放行）。本轮约 22 分钟，其中 CP 打分 40 个样本占大头。
