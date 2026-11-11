# W7 D6 · 真 LangGraph Agent 实跑记录（2026-11-11 补）

> [!important] 这份是 11/09 [evals/AGENT_DEMO.md](AGENT_DEMO.md) 的补强实现
> 11/09 那份（纯标准库最小 Agent）**保留不动**，作为本机没装依赖时的历史快照。本文是装好
> `langgraph 1.2.20` / `langfuse 3.15.0` 之后重写的正式实现：真 `StateGraph`、`ToolNode`、
> checkpointer，每一步工具调用是真正的 OTel span。两份实现跑同一组场景，结果逐项对账。
>
> ✅ **已真上报**（2026-11-11 13:3x 补验）：自托管 Langfuse 服务从 11/09 起一直 Up，
> 「服务没启动」是误判。真因是 `.env` 的 `LANGFUSE_HOST` 写 `localhost`（Windows 解析到
> IPv6 `::1`，Docker 端口转发在 IPv6 上不通）+ 进程有 `HTTP_PROXY` 且无 `no_proxy`。
> 归一化成 `127.0.0.1` 并绕开代理后，`auth_check()` 通过，S1 的 `agent.run` +
> `agent.tool.calculator` 两条 span 已用 `/api/public/traces` 回查确认落在面板数据源里。
> 两个坑与验证方法见 §10。

## 1. 装依赖踩的三个坑

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| Python 3.14 的 `xxhash` 轮子 | `pip install langgraph==1.2.20` 解析到 `xxhash>=3.5.0` 后报 `No matching distribution found`，3.5.0 没有 cp314 轮子 | 显式钉 `xxhash==4.1.0`（有 cp314 轮子），依赖解析即通过 |
| `wrapt` 版本冲突 | `langfuse 3.15.0` 要 `wrapt<2`，本机 2.6.0.dev1 被 `formulaic` 依赖；pip 卸载旧版时清理临时目录，被 WorkBuddy 沙箱的批量删除守卫拦下，安装事务中断 | 第一次失败已把 `wrapt` 降到 1.17.3（`formulaic 1.2.2` 回读仍可 import），**不带任何卸载动作重试**即装完；没有禁用安全开关，也没动 `formulaic` |
| 装完 OTel SDK 之后的既有噪声 | 全量 pytest 通过，但退出时多出 `Transient error ... exporting spans batch` 重试日志 | 现有观测测试会初始化 Langfuse 客户端、往 `.env` 的 `localhost` 发批量 span，撞的正是 §10 的 IPv6 坑；**D6 新代码**在默认路径不初始化客户端，用独立本地 provider，只有 `--langfuse` 才上报 |

钉版：`langgraph==1.2.20`、`xxhash==4.1.0`、`langfuse==3.15.0`（与 W2 实测的 SDK v3 API 对齐，
不升 4.x/5.x）。`requirements.txt` 里 `langfuse>=2.50,<4.0` 原本就声明了，本轮补上 `langgraph`。

## 2. 图长什么样

```
START → plan → [finish_reason?] → END
                  ↓ 否
                tools（ToolNode）→ observe → [finish_reason?] → END
                                                  ↓ 否
                                                plan …
```

| 部件 | 用的是什么 | 干什么 |
| --- | --- | --- |
| 图 | `langgraph.graph.StateGraph` 编译出的 `CompiledStateGraph` | 三个节点 + 两条条件边，状态写在 `GraphState` |
| 工具执行 | `langgraph.prebuilt.ToolNode`，`handle_tool_errors` + `wrap_tool_call` | 执行 `AIMessage.tool_calls`，异常转 `TOOL_ERROR=...`，每次调用外面包一个 span |
| 状态 | `GraphState`（`messages` 用 `add` reducer） | 队列、当前 pending、已收集事实、trace、步数、三条守卫参数 |
| 持久化 | `langgraph.checkpoint.memory.InMemorySaver` + `thread_id` | 编译期挂 checkpointer，`interrupt_before=["tools"]` 演示断点续跑 |
| 规划 | 规则函数 `default_plan()` | 按任务关键词排工具队列，**不调大语言模型**，同输入永远同结论 |

`summarize` 的入参在 `plan` 节点里由已收集事实动态拼出，所以「下一步取决于上一步结果」是状态驱动的，
不是一次性 if-else（一次性判断与有状态循环的区别见 11/09 报告 §4）。

## 3. 每步工具调用是真 span

用标准 `opentelemetry-api`（开放遥测 API）建 span，不硬绑 Langfuse SDK：

- 根 span：`agent.run`，属性 `agent.task` / `agent.max_steps` / `agent.finish_reason` / `agent.steps`。
- 子 span：`agent.tool.<工具名>`，由 `ToolNode.wrap_tool_call` 包住**真实执行区间**，属性
  `agent.step` / `agent.tool` / `agent.args` / `agent.reason` / `agent.result` /
  `agent.elapsed_ms` / `agent.ok`。
- 字典 `trace` 是 span 的本地投影，供旧断言与回放对账；观测原语是 span，不是字典。
- Langfuse SDK 3.x 注册全局 TracerProvider，`enable_langfuse_tracing()` 初始化后 `run_agent()` 不传
  `ctx` 就会用全局 tracer，同一批 span 自动被采集上报。该函数做三件事：host 归一化
  （`localhost` → `127.0.0.1`）、补 `no_proxy`、用 `httpx.Client(trust_env=False)` 绕开本地代理；
  凭据缺失或 `auth_check()` 失败时返回 `None`，调用方回落 `local_tracing()`（独立
  `TracerProvider` + `InMemorySpanExporter`），只验结构不上报——**不谎称已上报**。

S1 实跑导出（`evals/out/w7d6/langgraph_spans.json`，本地产物不入库）：

| span | 父 span | 自身耗时 | step | tool | elapsed_ms |
| --- | --- | ---: | ---: | --- | ---: |
| `agent.run` | 无 | 27.621 ms | — | — | — |
| `agent.tool.calculator` | `agent.run` | 2.042 ms | 1 | calculator | 1.950 |
| `agent.tool.policy_lookup` | `agent.run` | 1.300 ms | 2 | policy_lookup | 1.237 |
| `agent.tool.summarize` | `agent.run` | 1.036 ms | 3 | summarize | 0.987 |

三个子 span 的 `parent_span_id` 都等于根 span 的 `span_id`，已在实跑与单测里各断言一次。
工具耗时从 11/09 纯函数版的约 0.038 ms 涨到约 1–2 ms，整次 `agent.run` 从 0.245 ms 涨到约 27 ms，
这是图调度、消息对象构造与 span 导出的开销，属于换真框架的代价；计时仍是本机 CPU 时间，
回放对账不比较耗时。

## 4. 八个场景与三条守卫

| ID | 场景 | 步数 | 结束原因 | span 数 | 工具链 |
| --- | --- | ---: | --- | ---: | --- |
| S1 | 算 128*4 并查报销截止日 | 3 | `completed` | 4 | calculator → policy_lookup → summarize |
| S2 | 只查审批人 | 2 | `completed` | 3 | policy_lookup → summarize |
| S3 | 只算 32/8 | 2 | `completed` | 3 | calculator → summarize |
| S4 | 死循环守卫（max_steps=4） | 4 | `max_steps` | 5 | loop_tool ×4 |
| S5 | 超时守卫（timeout=0.05 s） | 1 | `timeout` | 2 | slow_tool |
| S6 | 未知工具 | 1 | `tool_error` | 1 | no_such_tool（规划层拦下，无工具 span） |
| S7 | 注入式表达式 | 1 | `completed` | 2 | calculator（返回 `CALC_ERROR`） |
| S8 | 工具内部抛异常（本轮新增） | 1 | `tool_error` | 2 | broken_tool（`TOOL_ERROR=RuntimeError`） |

三条守卫在框架里各自落在哪：

- **最大步数**：`plan` 节点判 `step > max_steps` 写 `finish_reason=max_steps`；编译期同时设
  `recursion_limit = max_steps*4+4`，作为 LangGraph 自己的框架级兜底。
- **总超时**：`plan` 入口与 `observe` 收尾各查一次总耗时，慢工具返回后立即判 `timeout`，
  不会继续规划下一步。
- **未知 / 异常工具**：未知工具在 `plan` 里比对工具白名单，直接记 `UNKNOWN_TOOL` 收尾，不进
  `ToolNode`（所以 S6 只有根 span）；工具内抛异常由 `ToolNode.handle_tool_errors` 转成字符串进
  `ToolMessage`，`observe` 判 `ok=False` 收尾，图不崩。

`calculator` 仍复用 11/09 的 AST（Abstract Syntax Tree，抽象语法树）白名单求值，不用 `eval`。
S7 把 `__import__('os').system('echo pwned')` 塞进去只得到 `CALC_ERROR`，命令没执行。

## 5. checkpointer 断点续跑

`run_with_resume()` 用 `interrupt_before=["tools"]` 编译，每次进入工具节点前停；**每次续跑都重新
编译一张新图**，只共享同一个 `InMemorySaver` 与 `thread_id`。三次 `graph.invoke(None, config)`
之后 `finish_reason=completed`、`steps=3`、`checkpoints=['tools','tools','tools']`，且链式 span
仍是 1 根 + 3 工具。状态来自 checkpointer 而不是图对象的局部变量，这一点由「换图仍能续」验证。

## 6. 与 11/09 零依赖实现对账

`AgentResult` / `TraceEntry` 直接复用 `app/agent_demo.py` 的数据结构，前三个场景的结束原因、
工具链、每一步入参与 11/09 版逐项一致（实跑 `legacy_crosscheck` 与单测各验一次）。
换句话说，这次换的是执行引擎与观测原语，任务行为和判定没变。

## 7. 设计取舍

- **规划器仍是规则，不接大语言模型**：微秒级、零成本、同输入永远同结论，可回放对账；与 W4 D6
  分流器、W7 D5 决策选择器同一条纪律。LangGraph 解决的是状态与步骤编排，不等于「下一步必须让模型判」。
- **span 用标准 OTel API，不写死 Langfuse 调用**：Langfuse 3.x 是全局 provider，标准 span 在服务
  可达时自动被采集；连不上或没凭据时换本地 exporter 就能验结构与层级，业务代码一行不用改。
  代价是「上报成功」必须单独验证（查面板数据源），不能凭「代码走了上报分支」就当已上报。
- **旧实现不删**：`app/agent_demo.py` 与其 13 项单测、7 场景脚本全部保留，作为零依赖回退路径与
  对照基线。
- **未新增 DEC 编号**：与 11/09 口径一致，决策取舍记录在本节与库内笔记《W7 D6 动手实践 · Agent 编排》，
  不擅自占用 `DEC-048`。

## 8. 复现

**PowerShell 5.1**（本机 electerm 默认）：

```powershell
cd E:\Projects\LLMOps-Engineer
D:\python314\python.exe scripts\w7d6_langgraph_demo.py              # 默认：本地 exporter，不联网
D:\python314\python.exe scripts\w7d6_langgraph_demo.py --langfuse   # 额外把 S1 真上报到面板
D:\python314\python.exe -m pytest tests\test_w7d6_langgraph.py -q
D:\python314\python.exe -m ruff check app\agent_graph.py scripts\w7d6_langgraph_demo.py tests\test_w7d6_langgraph.py
```

**Git Bash**：

```bash
cd /e/Projects/LLMOps-Engineer
/d/python314/python.exe scripts/w7d6_langgraph_demo.py
/d/python314/python.exe scripts/w7d6_langgraph_demo.py --langfuse
/d/python314/python.exe -m pytest tests/test_w7d6_langgraph.py -q
/d/python314/python.exe -m ruff check app/agent_graph.py scripts/w7d6_langgraph_demo.py tests/test_w7d6_langgraph.py
```

实跑：demo **24/24 断言**（默认路径与 `--langfuse` 路径各跑一次）、新增单测 **22 项**、
全量 pytest **161 项**（11/09 的 139 项 + 22 项），三个新文件 Ruff 全过。

## 9. 落点与边界

| 文件 | 作用 |
| --- | --- |
| `app/agent_graph.py` | 真 StateGraph / ToolNode / checkpointer、规则规划器、OTel span、两条对外入口 |
| `scripts/w7d6_langgraph_demo.py` | 8 场景 + 24 断言 + span 父子层级 + 断点续跑 + 新旧对账 |
| `tests/test_w7d6_langgraph.py` | 22 项单测（含 host 归一化与代理绕过各 1 项） |
| `evals/out/w7d6/langgraph_demo.json` | 场景明细（本地产物，不入库） |
| `evals/out/w7d6/langgraph_spans.json` | 导出的真 span（本地产物，不入库） |

**仍未做**：checkpointer 换成跨进程持久化实现（当前是 `InMemorySaver`，进程退出即丢）；
工具换成真实检索 / 计费接口后重新评估权限边界；`app/observability.py` 自身的 `localhost`
地址未改（属 W2 观测层，改动会影响既有测试与容器配置，需单独确认）。没改 `/chat`、
没改 Dockerfile、没接真实模型。

## 10. 接入自托管 Langfuse 的两个坑（2026-11-11 13:3x 复核）

**结论先说：服务没坏，也没停。** 6 个容器自 11/09 起 `Up`，`/api/public/health` 返回 200。

| 坑 | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| ① `localhost` 走 IPv6 | `LANGFUSE_HOST=http://localhost:3000` 下 `curl` 返回 `000`、Python `urllib` 报连接重置 | Windows 把 `localhost` 优先解析成 `::1`，Docker Desktop 的端口转发只监听 IPv4；`curl -4` 与 `127.0.0.1` 均 200 | `normalize_langfuse_host()` 把 `localhost` 换成 `127.0.0.1`（不改 `.env`，只在运行时归一化） |
| ② 本地代理截胡 | 换 `127.0.0.1` 后 `urllib` 通，但 SDK `auth_check()` 仍失败，日志 `WinError 10053` | 进程环境有 `HTTP_PROXY=http://127.0.0.1:6670` 且无 `no_proxy`，httpx 把请求发给了代理 | `bypass_local_proxy()` 追加 `no_proxy` + 构造 `httpx.Client(trust_env=False)`，不删任何既有代理变量 |

对照验证（同一台机、同一时刻）：

```
curl --noproxy '*' http://localhost:3000/api/public/health   → 000（IPv6 不通）
curl -4 --noproxy '*' http://localhost:3000/api/public/health → 200
curl --noproxy '*' http://127.0.0.1:3000/api/public/health    → 200
SDK auth_check(host=localhost)   → False
SDK auth_check(host=127.0.0.1)   → True（禁用代理变量后）
```

**上报证据**：`python scripts/w7d6_langgraph_demo.py --langfuse` 后，用
`/api/public/v2/observations?limit=100` 回查，同一 `traceId` 下出现
`agent.run` 与 `agent.tool.calculator` 两条记录，`startTime` 与本机运行时刻一致——
是本轮真打进去的，不是历史残留。

**教训**：12:4x 那次用 `urllib` 直连 `localhost` 拿到「连接重置」，就判定「服务未启动」并写进
报告与库内笔记，是**用单一探测手段下了结论**。正确做法是先分层对照（容器状态 → 端口 → IPv4/IPv6
→ 代理），再下判断。这次错了约 1 小时，已在两仓按 append-only 更正。

## 中英术语对照

| 术语 | 英文全称 | 中文 |
| --- | --- | --- |
| OTel | OpenTelemetry | 开放遥测 |
| span | OpenTelemetry Span | 跨度（一段带起止时间与属性的执行记录） |
| StateGraph | LangGraph StateGraph | 状态图 |
| ToolNode | LangGraph ToolNode | 工具节点 |
| checkpointer | LangGraph Checkpointer | 检查点保存器 |
| SDK | Software Development Kit | 软件开发工具包 |
| AST | Abstract Syntax Tree | 抽象语法树 |
| LLM | Large Language Model | 大语言模型 |
