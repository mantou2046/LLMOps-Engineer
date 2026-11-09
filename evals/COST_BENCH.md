# W7 D3 · 成本控制三件套压测报告（2026-11-09）

> [!important] 一句话结论
> 同一批 1000 次查询、同一份单价表，只切两个开关：精确缓存 + 规则分流后**每千次查询的模型 Token 成本点数从 1754.6 降到 626.5，下降 64.3%**。这是离线仿真数字，**不是真钱，也不是线上流量**；语义缓存本机跑不起来，不在其中。

## 1. 验收口径

任务卡 11/18 要求「压测出成本下降百分比」，并提醒「别只算模型费 —— 向量库、rerank、缓存服务、网络出流量都要算进去」。本报告的做法：

- 模型侧：按 `app/cost.py` 的演示单价（成本点数 / 千 Token）算，可给出百分比。
- 非模型侧：**显式列出四项未计价成本与各自需要的数据**（见 §5），没有真实单价就不编数字冒充已计入。
- 公平比较：四组共用**同一个固定种子的查询序列**、同一份 Token 估算、同一份单价表，唯一变量是开关。这是阅读清单自测题「改路由后便宜 30%，还要检查什么」的答案。

## 2. 四组结果

`scripts/w7d3_cost_bench.py`（种子 `20261118`，每组 1000 次查询，20 条题集，其中 30% 是字面重复题、70% 是带唯一工单号的一次性问题）：

| 场景 | 每千次成本点数 | 每千次 Token | 缓存命中率 | cheap 占比 | 相对基线 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A 基线（全 primary、无缓存） | 1754.6 | 175456 | 0.0% | 0.0% | — |
| B 只开分流 | 675.3 | 175456 | 0.0% | 69.7% | **−61.5%** |
| C 只开精确缓存 | 1266.8 | 126680 | 28.4% | 0.0% | **−27.8%** |
| D 分流 + 精确缓存 | **626.5** | **126680** | **28.4%** | **69.7%** | **−64.3%** |

规则分流器 `app/routing.py` 与 20 条题集标注的一致率 **100%（0 条不符）**；四项断言 4/4 通过（两件单开都省钱、组合更省、缓存确有命中）。明细：`evals/out/w7d3/cost_bench.json`（本地产物，不入库）。

> [!note]- 为什么首轮数字被推翻重跑
> 第一版把 20 条题直接抽 1000 次，命中率冲到 **98.3%**、组合降 **99%** —— 因为题集里只有 20 种缓存键，第一次 miss 之后全命中。这不是成本优化，是题太少的假象。改成「30% 字面重复 + 70% 一次性唯一题」后才符合 W4 D5 的实测结论：**精确缓存只对一模一样的重复请求有效，命中率上限就是重复率**。

## 3. 一个值得写进面试的观察：两件收益不叠加

单看 B（−61.5%）和 C（−27.8%），直觉会以为组合该有 −70% 以上，实际只有 **−64.3%**，比只开分流（B 的 675.3）只多省 48.8 点。

原因不是代码有问题，是**流量分布**：题集里重复出现的恰好都是「这个字段是什么意思？」这类简单问法，它们本来就被分流到 cheap（1/10 单价）。缓存命中的 28.4% 请求，省的是**便宜档**的钱；贵的 primary 请求几乎都是一次性复杂题，缓存帮不上。

所以顺序上应该先量「重复率 × 重复题的档位分布」，再决定要不要为缓存付运维成本。这条结论只在本压测的分布下成立，真实流量要先看日志。

## 4. `/chat` 接口层验证（Docker `llmops-api:w7d3`）

离线压测之外，缓存也真的接进了 `/chat`，用默认关闭的 `COST_CACHE_ENABLED=1` / `COST_LEDGER_ENABLED=1` 打开，mock 上游、零真实模型费用：

| 请求 | 响应头 | 响应体 |
| --- | --- | --- |
| 第一次同样请求 | `x-cost-cache: MISS` | `usage` = 3 / 42 / 45（mock 估算） |
| 第二次同样请求 | `x-cost-cache: HIT` | `cost_cache.hit=true`、`usage` 全 0 且 `cached=true`、`request_id` 换成 `cache-<key>` |
| 多轮请求（3 条消息） | **无**该头 | 按 `multi_turn` 跳过，不写缓存 |

> [!warning] 修掉的一处假象
> 首轮容器验证里，HIT 响应把缓存中**上次**的 `usage`（45 tokens）原样回显，账本记 0 但前端看到 45，会误以为这次又调了模型。已改成命中时用量归零 + 换 request_id，并补了单测断言 `r2.usage.total_tokens == 0`、`r2.request_id != r1.request_id`。

## 5. 没算进去的成本（任务卡点名项）

`app/cost.py::UNPRICED_ITEMS` 显式列了四项，报告里原样输出，**不计入上面的百分比**：

| 成本项 | 要算它需要的数据 | 状态 |
| --- | --- | --- |
| 向量库（pgvector） | 实例规格 × 时长 + 存储容量 | 未计入 |
| rerank（bge-reranker-base） | GPU/CPU 时长 × 精排次数 | 未计入 |
| 缓存服务（Redis） | 实例规格 × 时长 | 未计入 |
| 网络出流量 | 出网 GB × 单价 | 未计入 |

W6 已实测 rerank 精排 top-5 净增 2.2–2.4 s，所以「默认不开」这条本身也是成本控制的一部分，但它没有单价，不能折算进本次百分比。

## 6. 口径边界（复述一遍，防止被当成财务数字）

1. **离线仿真**，未调用真实模型；Token 按题长确定性估算，与档位无关（故意不让「便宜档回答更短」混进省钱结论）。
2. **cost units 是演示点数、不是货币**。`primary:cheap = 10:1` 沿用 W5 D3 实测比例；网关 `model_info` 里的单价是给 fake-model 的演示价，不参与业务路由。
3. **精确缓存，不是语义缓存**。W4 D5 已实测本机三条前置条件全缺（上游无 embedding 端点 / `redis:7` 无 RediSearch / `redisvl` 未装），同义不同字面的题不命中。阈值 0.85 仍是暂定值、无实测支撑。
4. 缓存守卫：流式、多轮、`temperature>0` 一律不缓存；降级回答不写缓存。
5. mock 上游的 `usage` 本身带 `estimated=true`；接真实上游后需换成供应商回报的用量再跑一次。
6. 改动默认关闭：两个开关关掉时 `/chat` 行为与 W7 D2 完全一致。

## 7. 复现

仓库根目录 `E:\Projects\LLMOps-Engineer`。

**Git Bash：**

```bash
cd /e/Projects/LLMOps-Engineer
/d/python314/python.exe scripts/w7d3_cost_bench.py
/d/python314/python.exe -m pytest tests/test_cost.py -q
/d/python314/python.exe -m ruff check app/cost.py scripts/w7d3_cost_bench.py tests/test_cost.py
docker build -f app/Dockerfile -t llmops-api:w7d3 .
docker run --rm -d --name llmops-w7d3 -p 127.0.0.1:18006:8000 \
  -e COST_CACHE_ENABLED=1 -e COST_LEDGER_ENABLED=1 -e LLM_MODE=mock -e OBSERVABILITY=off llmops-api:w7d3
for i in $(seq 1 20); do sleep 2; s=$(docker inspect --format '{{.State.Health.Status}}' llmops-w7d3 2>/dev/null); [ "$s" = "healthy" ] && break; done
curl -s -D - -o /dev/null -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"这个字段是什么意思？"}],"model":"cheap"}' \
  http://127.0.0.1:18006/chat | grep -i x-cost-cache
curl -s -D - -o /dev/null -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"这个字段是什么意思？"}],"model":"cheap"}' \
  http://127.0.0.1:18006/chat | grep -i x-cost-cache
docker stop llmops-w7d3
```

**PowerShell：**

```powershell
cd E:\Projects\LLMOps-Engineer
D:\python314\python.exe scripts\w7d3_cost_bench.py
D:\python314\python.exe -m pytest tests\test_cost.py -q
D:\python314\python.exe -m ruff check app\cost.py scripts\w7d3_cost_bench.py tests\test_cost.py
docker build -f app/Dockerfile -t llmops-api:w7d3 .
docker run --rm -d --name llmops-w7d3 -p 127.0.0.1:18006:8000 -e COST_CACHE_ENABLED=1 -e COST_LEDGER_ENABLED=1 -e LLM_MODE=mock -e OBSERVABILITY=off llmops-api:w7d3
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $status = docker inspect --format '{{.State.Health.Status}}' llmops-w7d3 2>$null
    if ($status -eq 'healthy') { break }
}
if ($status -ne 'healthy') { docker logs llmops-w7d3; throw '容器未通过健康检查' }
$body = '{"messages":[{"role":"user","content":"这个字段是什么意思？"}],"model":"cheap"}'
$r1 = Invoke-WebRequest -Uri 'http://127.0.0.1:18006/chat' -Method Post -ContentType 'application/json' -Body $body
$r2 = Invoke-WebRequest -Uri 'http://127.0.0.1:18006/chat' -Method Post -ContentType 'application/json' -Body $body
"R1=$($r1.Headers['X-Cost-Cache'])  R2=$($r2.Headers['X-Cost-Cache'])"
($r2.Content | ConvertFrom-Json).usage | ConvertTo-Json -Compress
docker stop llmops-w7d3
```

预期：R1 为 `MISS`、R2 为 `HIT`，第二次响应里 `usage` 的合计 Token 数为 0。容器起来后**必须等 healthy 再发请求**，否则会遇到「连接提前断开、日志里也看不到请求」的假故障（W7 D2 踩过）。

## 8. 落点

| 文件 | 作用 |
| --- | --- |
| `app/cost.py` | 精确缓存 / 演示单价 / Token 审计账本 / 未计价成本清单 |
| `app/main.py` | `/chat` 接线与两个默认关闭开关、命中响应用量归零 |
| `app/Dockerfile` | 补 `cost.py` 的 COPY（精确 COPY 漏一行就 import 失败） |
| `evals/cost/queries.jsonl` | 20 条固定题集（含 3 条字面重复题） |
| `scripts/w7d3_cost_bench.py` | 四组同条件压测 + 断言 |
| `tests/test_cost.py` | 18 项单测 |
| `evals/out/w7d3/cost_bench.json` | 明细（本地产物，不入库） |
