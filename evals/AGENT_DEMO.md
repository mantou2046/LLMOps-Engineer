# W7 D6 · 最小可观测 Agent 实跑记录（2026-11-09）

> [!warning] 先说边界：这不是 LangGraph
> 本机 2026-11-09 实测 `langgraph` / `langchain` / `langchain_core` / `langfuse` **全部未安装**。本轮按「文档为主、不动环境」的约定，不装新包，用**纯标准库**做了一个最小可跑的多步 Agent：节点 / 状态 / 分支 / 工具循环都用普通函数表达，trace 全链路可核对。
> 真 `StateGraph` / `ToolNode` / checkpointer 与 Langfuse 自动追踪**尚未实现**，需装依赖后补；任务卡 11/21 的「可观测 agent」按「概念与验收点已验证、LangGraph 实现待补」记账。

> [!success] 2026-11-11 补记：上面的「待补」已闭环
> 真 `StateGraph` / `ToolNode` / checkpointer 与 OTel span 已实现并实跑，见 [evals/AGENT_GRAPH.md](AGENT_GRAPH.md)。本文与 `app/agent_demo.py` 按 11/09 时点快照保留，作为零依赖回退路径与对照基线；下方「待补」§6 与所有 11/09 数字不再回溯改写。

## 1. 任务卡验收点：trace 里能不能看到每一步

`scripts/w7d6_agent_demo.py` 的 S1（「算一下 128*4，并查报销截止日」）实跑输出：

| step | tool | args | result | elapsed_ms | reason |
| --- | --- | --- | --- | ---: | --- |
| 1 | `calculator` | `{"expr": "128*4"}` | `CALC=512` | 0.038 | 任务含算式，先算数 |
| 2 | `policy_lookup` | `{"key": "截止日"}` | `字段=截止日；值=每月 25 日；依据=制度第 3 条。` | 0.002 | 任务提到「截止日」，查制度原文 |
| 3 | `summarize` | `{"text": "CALC=512\|字段=…"}` | `回答：CALC=512；字段=截止日；…` | 0.008 | 事实收集完毕，拼成回答并结束 |

`finish_reason=completed`、`steps=3`、总耗时 0.245 ms。**每一步的工具名、入参、结果、耗时、为什么选它、最终结束原因都在 trace 里**，逐条可回放。计时是本机 CPU 时间，重复跑会变，回放对账时不比较 `elapsed_ms`（已留成单测口径）。

## 2. 七个场景与三条守卫

| ID | 场景 | 步数 | 结束原因 | 工具链 |
| --- | --- | ---: | --- | --- |
| S1 | 算 128*4 并查报销截止日 | 3 | `completed` | calculator → policy_lookup → summarize |
| S2 | 只查审批人 | 2 | `completed` | policy_lookup → summarize |
| S3 | 只算 32/8 | 2 | `completed` | calculator → summarize |
| S4 | 死循环守卫（max_steps=4） | 4 | `max_steps` | loop_tool ×4 |
| S5 | 超时守卫（timeout=0.05 s） | 1 | `timeout` | slow_tool |
| S6 | 未知工具 | 1 | `tool_error` | no_such_tool |
| S7 | 注入式表达式 | 1 | `completed` | calculator（返回 `CALC_ERROR`） |

三条守卫对应任务卡点名的坑「agent 极易死循环 —— 必须设最大步数和超时」：

- **最大步数**：S4 用永远要求「再查一次」的工具，正好在第 4 步停下，不会无限跑。
- **总超时**：S5 工具睡 0.15 s、上限 0.05 s，返回后立刻判定 `timeout`。
- **未知 / 异常工具**：S6 点名不存在的工具记 `UNKNOWN_TOOL`；工具内抛异常记 `TOOL_ERROR=...`。两种情况都**不让 Agent 崩掉**，异常进 trace 再终止。

9 项断言全过（结束原因全对、S1 三步链路、trace 字段齐全、128*4=512、死循环停在 max_steps、注入被拒等）。明细 `evals/out/w7d6/agent_demo.json`（本地产物，不入库）。

## 3. 一条容易被忽略的安全边界：工具不能执行任意代码

`calculator` 用 `ast` 白名单求值（只允许数字与四则运算节点），**不用 `eval`**。S7 把 `__import__('os').system('echo pwned')` 塞进去，得到的是 `CALC_ERROR`，命令没有被执行。

Agent 的工具是模型能间接触达的执行面：给了 `eval` / `shell` / 任意文件读，等于把权限交出去了。凭据由应用持有，不放进模型可见上下文，trace 也不记请求正文之外的敏感值。

## 4. 设计取舍（和前面几周是同一条纪律）

- **不用 LLM 判下一步**：规划器是规则 + 有状态队列，微秒级、零成本、同输入永远同结论，可回放对账。和 W4 D6 的应用侧分流器、W7 D5 的决策选择器一致。
- **状态驱动而不是一次性 if-else**：第一次规划把工具队列写进 `state`，之后每步看剩余计划再决定下一步，所以「下一步取决于上一步执行后的状态」。
- **不接真实模型 / 不接观测后端**：工具是固定制度表与四则运算；trace 是内存结构 + JSON 落盘，不是 Langfuse trace。

## 5. 复现

```bash
cd /e/Projects/LLMOps-Engineer
/d/python314/python.exe scripts/w7d6_agent_demo.py
/d/python314/python.exe -m pytest tests/test_w7d6_agent.py -q
/d/python314/python.exe -m ruff check app/agent_demo.py scripts/w7d6_agent_demo.py tests/test_w7d6_agent.py
```

```powershell
cd E:\Projects\LLMOps-Engineer
D:\python314\python.exe scripts\w7d6_agent_demo.py
D:\python314\python.exe -m pytest tests\test_w7d6_agent.py -q
D:\python314\python.exe -m ruff check app\agent_demo.py scripts\w7d6_agent_demo.py tests\test_w7d6_agent.py
```

单测 13 项，全量 pytest **139 项**通过；新增三个文件 ruff 全过。

## 6. 落点与待补

| 文件 | 作用 |
| --- | --- |
| `app/agent_demo.py` | 三个工具、TraceEntry / AgentResult、规则规划器、`run_agent` 三条守卫 |
| `scripts/w7d6_agent_demo.py` | 7 场景实跑 + 9 项断言 |
| `tests/test_w7d6_agent.py` | 13 项单测 |
| `evals/out/w7d6/agent_demo.json` | trace 明细（本地产物，不入库） |

**待补**：装 `langgraph` / `langfuse` 后用 `StateGraph` + `ToolNode` 重写一遍，把 trace 接到 Langfuse 自动追踪；工具换成真实检索 / 计费接口后再评估权限边界。本轮不改服务、不动 `/chat`。
