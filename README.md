# LLMOps Engineer 作品集 · 资金/财务制度知识库智能问答与评估平台

> 一个**可复现、可评估、可观测**的 LLM 应用，用来回答资金 / 财务制度类问题。
> 目标不是「能跑」，而是**能证明每次改动让系统变好还是变坏**。

---

## 问题陈述（3 句话，待细化）

1. 资金 / 财务制度分散在多个文档里，人工查找慢且容易漏，问答类工具又常常「答得像那么回事但引错条款」。
2. 本项目做一个带**引用溯源**的知识库问答服务，并且**给它的每个回答建立可量化的评估基线**。
3. 关键不是把模型接上，而是搭出一套 **检索 → 评估 → 观测 → 成本** 的工程闭环，让质量变化可测量、回归可拦截。

> ⚠️ **数据脱敏铁律**：本仓库**只放自造的、同构的样例文档**。任何来自真实单位（制度、手册、表结构、账号、内网地址）的内容一律不入库。提交前必查。

---

## 目录结构

```
LLMOps-Engineer/
├── app/            # FastAPI 服务层（LLM 调用封装、结构化输出、重试、流式）
├── infra/          # 部署与基础设施（Docker / K8s / Helm / Terraform）
├── evals/          # 评估体系（golden set、RAGAS 四指标、CI 门禁脚本）
├── data/           # 样例数据（自造脱敏文档 + 切块产物）
├── notebooks/      # 探索性实验（chunk 策略对比、检索调优）
└── docs/
    ├── decisions.md   # 决策日志（每个关键选型写清「为什么」）
    └── env-setup.md   # 本机环境与已知坑
```

---

## 关键数字（README 必须写清的 5 个指标）

> 这些是面试官唯一会看的东西。每周更新一次，**必须能在本仓库里复现**。

| # | 指标 | 改动前 | 改动后 | 测量方式 |
| --- | --- | --- | --- | --- |
| 1 | faithfulness | — | — | RAGAS，`evals/` |
| 2 | context recall | — | — | RAGAS，`evals/` |
| 3 | P95 延迟 / TTFT | — | — | Grafana + 网关层埋点 |
| 4 | 每千次查询成本 | — | — | Langfuse + 云账单 |
| 5 | 语义缓存命中率 | — | — | LiteLLM 指标 |

---

## 快速开始

> 依赖与网络配置见 [`docs/env-setup.md`](docs/env-setup.md)（含代理端口、pip 源、HF 镜像的**实测结论**）。

```bash
cp .env.example .env          # 填入自己的密钥
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
```

### 启用提交前脱敏检查（克隆后必做一次）

本仓库是**公开**的，所以有一道提交前门禁，防止手滑把凭据或内部信息推上去。

```bash
git config core.hooksPath .githooks
cp scripts/sensitive_terms.example.txt scripts/sensitive_terms.txt   # 填自己的敏感词
python scripts/check_sensitive.py --all                              # 手动全树扫一遍
```

检查内容分三层：

| 层 | 内容 |
| --- | --- |
| 内置规则 | 私钥、各类 API Key / Token、JWT、**内网 IP 段**、带口令的连接串、疑似凭据键值 |
| 自定义词表 | `scripts/sensitive_terms.txt` 里的单位名 / 内网域名 / 真实表名字段名 |
| CI 兜底 | `.github/workflows/ci.yml` 在 PR 上跑同一脚本，防止本地没装钩子的人绕过 |

> ⚠️ 脚本**只报「文件:行号 + 命中类型」，绝不回显命中的原文** —— 否则检查工具本身就成了泄露渠道。
> ⚠️ `scripts/sensitive_terms.txt` 被 gitignore，**真实词表只留本地**。

---

## 进度

对照学习计划：`E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\`

| 周 | 日期 | 主题 | 状态 |
| --- | --- | --- | --- |
| W1 | 10/7 – 10/11 | 起手与环境、Docker、K8s 起手 | 进行中 |
| W2 | 10/12 – 10/18 | K8s 生产化、上云、服务化 + 观测 | 未开始 |
| W3 | 10/19 – 10/25 | 实验追踪与模型管理 | 未开始 |
| W4 | 10/26 – 11/1 | 编排、部署与网关 | 未开始 |
| W5 | 11/2 – 11/8 | 监控、工程规范与 IaC | 未开始 |
| W6 | 11/9 – 11/15 | RAG 与评估（最高优先级） | 未开始 |
| W7 | 11/16 – 11/22 | 护栏、成本、微调与 Agent | 未开始 |
| W8 | 11/23 – 11/30 | 作品集打包与求职启动 | 未开始 |
