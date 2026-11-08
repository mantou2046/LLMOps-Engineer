# W6 D6 · 不调 LLM 的 eval 门禁

> 2026-11-08 实跑，对应任务卡 11/14。上游：`evals/SOURCE_CHUNK_BACKFILL.md`（块号回填）、
> `evals/golden_set.jsonl`（题库）、`evals/RAGAS_BASELINE.md`（为什么暂不把 LLM 评估搬进 CI）。
> 门禁配置 `evals/gate/config.json`，query 模板 `evals/gate/query_template.txt`。

## 结论

CI 里加了一个 `eval-gate` job，**零网络、零模型、零第三方包**，只用 Python 标准库。
抽样子集 20 题跑块号召回，低于阈值直接 fail；把 query 模板里的 `{question}` 改掉，
召回从 0.75 掉到 0，退出码 1。任务卡 11/14 的三件事和「故意改坏 prompt 验证能拦住」都落地了。

| 场景 | IDBasedContextRecall@5 | 阈值 | 退出码 |
| --- | --- | --- | --- |
| 20 题抽样子集（CI 默认） | **0.75**（15/20） | 0.65 | 0 |
| 38 道可答题全量（本地周跑） | **0.8421**（32/38） | 0.65 | 0 |
| 改坏 query 的反例 | **0.0**（0/20） | 0.65 | 1 |

阈值取抽样子集基线减 0.10 安全余量。全量基线 0.8421 比抽样高，是因为抽样刻意收齐了难例。

## 一、先补的那半笔账：source_chunk 回填

D3 标注规范里写明「`source_chunk` 给 `IDBasedContextRecall` 和 CI 门禁用，50 题现在全是 null，回填脚本本次没写」。
这次按 W6 D1 的递归 B（400/50）切法补上：

- `scripts/w6d6_chunks.py` 复刻切分逻辑，与 `w6d1_chunk_bench.split_recursive` **330 块逐字一致**，
  并按 `evals/corpus/manifest.json` 的 sha256 逐个核对六篇冻结快照。
- `scripts/w6d6_backfill_source_chunk.py` 把 `reference_atoms` 的每条要点逐字核进块文本，
  取覆盖要点最多的块当主块。**38 道可答题全部逐字命中，没有一题需要片段兜底**，12 道拒答题保持 `null`。
- 写回前校验 JSONL 序列化可逆，`git diff` 确认 38 行只改了 `source_chunk` 一列，其余字段逐字节不动。
- 全部命中块另存 `evals/corpus/source_chunks.json`，门禁算召回时用的是这个集合，不是单个主块。
- `python scripts/w6d3_validate_golden.py --strict` 退出码 0，块号已回填 38。

## 二、门禁查什么

三件事，都在 `scripts/w6d6_eval_gate.py` 里：

1. **数据完整性**。题库 strict 校验、语料 sha256、每个 `source_chunk` 指向的块真实存在且确实含标准答案要点、
   拒答题不带出处。任何一条不过就 fail，不进检索环节。
2. **检索回归**。抽样子集用纯标准库 BM25 对 330 块取 top-5，按块编号判命中（IDBasedContextRecall@5）。
   中文用字符 unigram + bigram 近似分词，不装 jieba；这套口径只服务门禁的确定性，
   **不拿它的数字跟 D2 的 jieba BM25 或 D4 的向量检索混用**。
3. **阈值**。低于 `config.json` 的 0.65 就退出 1。全量 50 题留到本地周跑，CI 只跑 20 题抽样子集。

抽样子集是确定性分层：先收齐 3 道 `multi_hop`、3 道 `boundary`、3 道 `instruction`，
再按题号补 11 道 `single_hop`，共 20 题。这样难例不会被随机抽样漏掉，任何人重跑都是同一批题。

## 三、反例怎么验的

query 模板只有一行 `{question}`。门禁读它并把题干填进去；模板被改坏（占位符删掉或换成固定串），
检索就拿不到题干，召回立刻塌到 0。两条自动化证据：

```bash
cd /e/Projects/LLMOps-Engineer
python scripts/w6d6_eval_gate.py --self-test
python -m pytest tests/test_w6d6_gate.py -q
```

`--self-test` 一次跑正常与 sabotage 两个方向，断言正常 `exit=0`、改坏 `exit=1`。
`tests/test_w6d6_gate.py` 用临时坏模板走 `--template` 参数，断言退出码 1，
这就是任务卡「故意改坏 prompt 的 PR 被 CI 拦下」的可复现版本，且不会污染仓库里的模板文件。

`.github/workflows/ci.yml` 新增的 job 只有一行命令：

```yaml
- name: 数据完整性 + 检索回归 + 反例
  run: python scripts/w6d6_eval_gate.py --self-test
```

本机实测：`ruff check tests/` 全绿、`pytest` 29 passed（原 26 + 新增 3）、自检通过、正常门禁 PASS。

## 四、为什么没用 promptfoo / DeepEval

阅读清单 D6 给过两个候选，都要 LLM 裁判。11/8 下午那轮重跑已经把边界摸清了：
免费 key 的路由级配额撑不住 ragas 的调用密度，CI 更拿不到本地代理的 key。
门禁如果依赖 LLM，就会出现「上游限流导致 CI 红」这种跟代码改动无关的失败。
所以这里按 D4 报告里写的方向走：门禁用不调 LLM 的块号指标，全量 LLM 评估留本地每周跑。

代价也直说：**这个门禁只守检索层和评估集完整性，守不住生成质量**。
prompt 改坏导致回答变差、但检索块没变，它拦不住。要补那一层，得等有稳定额度的固定裁判，
再挂一个小的 LLM-as-judge 子集，跟这个非 LLM job 并列，不互相替。

## 五、局限与欠账

- 抽样子集基线只有 0.75，绝对值不高。它量的是字符 bigram BM25 的召回，不是 D2 的 RRF 融合质量；
  门禁用来拦回归，不当效果结论。
- 20 题里 5 题没命中，逐题清单在 `evals/out/w6d6/gate_result.json`（gitignore）。
- 10 题全量 RAGAS、`context_precision`、独立固定裁判重跑、D3 真人双标仍是欠账，见 `evals/ATTRIBUTION.md`。
- 12 道应拒答与 3 道 `boundary` 的分组报分还没做，分组后门禁子集可以按题型再调。
- 11/15 周复盘要的「改动前 / 后」对比数字，现在有检索侧底座了，生成侧还差一套可复现的全量评估。

## 六、复跑

```bash
cd /e/Projects/LLMOps-Engineer
python scripts/w6d6_backfill_source_chunk.py --check
python scripts/w6d6_eval_gate.py
python scripts/w6d6_eval_gate.py --subset 50
python scripts/w6d6_eval_gate.py --self-test
```

换语料要先重冻 `evals/corpus/` 快照并更新 `manifest.json`，再重跑回填；
块号是全局编号，语料一变整张表的块号都会漂。

## 七、交付清单

| 文件 | 内容 |
| --- | --- |
| `scripts/w6d6_chunks.py` | 零依赖切块 + bigram BM25，与 D1 逐字对账 |
| `scripts/w6d6_backfill_source_chunk.py` | 块号回填与可核证据 |
| `scripts/w6d6_eval_gate.py` | 非 LLM 门禁、自检、sabotage |
| `evals/corpus/source_chunks.json` | 50 题完整命中块清单 |
| `evals/gate/config.json` / `query_template.txt` | 阈值与 query 模板 |
| `tests/test_w6d6_gate.py` | 反例单测 3 条 |
| `.github/workflows/ci.yml` | `eval-gate` job |
| `evals/SOURCE_CHUNK_BACKFILL.md` / `evals/CI_EVAL_GATE.md` | 两份报告 |

#LLMOps #RAG #评估 #CI
