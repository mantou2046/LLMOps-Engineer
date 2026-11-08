# W6 D6 · golden set 的 source_chunk 块号回填

> 2026-11-08 实跑。切法与 W6 D1 的递归 B(400/50) 逐字对账：六篇冻结语料 104,798 字符 → **330 块**，全局块号 0..329。

## 口径

`source_chunk` 形如 `k8s-concepts.md#84`，前半是冻结语料文件名，后半是**全局块号**，
与 `scripts/w6d1_chunk_bench.py` / `w6d4_rag_chain.py` 里的 `chunk_indices` 同一套编号。
判据是把 `reference_atoms` 的每条要点逐字核进块文本，取覆盖要点最多的块当主块；
所有命中的块另存 `evals/corpus/source_chunks.json`，给 `IDBasedContextRecall` 用。
逐字找不到才降到 20 字片段匹配；仍找不到就留 `null`，不猜。

## 结果

| 项 | 数量 |
| --- | --- |
| 题目总数 | 50 |
| 可答题 | 38 |
| 拒答题（保持 null） | 12 |
| 逐字全部命中 | 38 |
| 片段兜底 / 部分命中 / 未定位 | 0 / 0 / 0 |
| 所有要点都落在同一批块里 | 29/38 |
| 已写入 golden_set.jsonl | 是 |

## 需要人工复核的题

无。所有可答题的每条要点都逐字核进了块文本。

## 复跑

```bash
cd /e/Projects/LLMOps-Engineer
python scripts/w6d6_backfill_source_chunk.py --check
python scripts/w6d6_backfill_source_chunk.py --apply
python scripts/w6d3_validate_golden.py --strict
```

改动的是 `source_chunk` 一列，其余字段逐字节不动；写回前会校验 JSONL 序列化可逆。
语料按 `evals/corpus/manifest.json` 的 sha256 逐个核对，换语料要先重冻快照再重跑。

#LLMOps #RAG #评估
