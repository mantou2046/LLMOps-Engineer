# vLLM 本地推理服务（W2 D10）

给 `/chat` 提供一条「自托管」上游通道，与托管 API 组成**双通道**，也用作 primary 失败时的 **fallback 兜底**。

## 这套东西解决什么

D10 的「看」讲 vLLM 部署 + continuous batching。落到「做」就一个目标：

> **让应用既能连「自己部署的模型」，也能连「托管 API」，且两者可切换、可兜底。**

关键认知：vLLM 的 server 暴露的是 **OpenAI 兼容协议**（`/v1/chat/completions`），
所以 app 侧**协议层一行都不用改** —— 双通道的实现就是「把 base_url 指到不同地方」。

## 双通道怎么接

`app/llm.py` 用档位（tier）区分上游，env 命名 `LLM_<TIER>_MODEL / _API_KEY / _BASE_URL`：

| 档位 | 指向 | 用途 |
|---|---|---|
| `primary` | 托管 API（如某云厂商 / OpenAI） | 主力通道 |
| `cheap` | 更便宜的模型 | 简单问题分流 |
| `fallback` | **本地 vLLM** 或另一家托管 API | primary 重试**耗尽**后自动切过来 |

**降级语义**（W2 D10 新增）：primary 重试耗尽仍失败 → 自动切 fallback 再试一次；
成功则返回值里标 `degraded=true`、`degraded_from="primary"`，响应头带 `X-LLM-Degraded: true`。
fallback 也没配/也失败 → 如实抛错，行为退回加此功能前。

## 启动本地 vLLM

```bash
# 首次会下模型权重（约 1GB，落 named volume，二次启动不再下）
docker compose -f infra/vllm/compose.yml up

# 探活（等 healthcheck 转 healthy，CPU 模式首次要几分钟加载模型）
curl -s http://localhost:8000/v1/models

# 直接试调（验证是标准 OpenAI 协议）
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"你好"}],"max_tokens":32}'
```

然后把 app 的 fallback 指过来（`.env`）：

```bash
LLM_FALLBACK_ENABLED=true
LLM_FALLBACK_MODEL=Qwen/Qwen2.5-0.5B-Instruct
LLM_FALLBACK_API_KEY=<PLACEHOLDER:任意非空值>   # vLLM 本地默认不校验，但 OpenAI SDK 要求非空
LLM_FALLBACK_BASE_URL=http://localhost:8000/v1
```

## ⚠️ 本机无 GPU 的现实

vLLM 的主战场是 **GPU**。本机无独显（`nvidia-smi` 不存在），只能走 **CPU 模式**：

- 能跑通链路、能拿到可对比的延迟/吞吐**数据**
- 但吞吐远低于 GPU（0.5B 模型 CPU 上大概个位数～几十 token/s）
- **结论**：本地 vLLM 用于「验证集成 + 出对比数据」，不作为可用性指标；真要上量必须 GPU

## 压测对比

```bash
# 先起 app（scripts/dev_app.sh），再压两条通道
python scripts/bench_channels.py --base http://localhost:8787 --channels primary,fallback --n 10
```

产出：p50 / p90 / p99 延迟、RPS、输出 token/s 的对比表。
