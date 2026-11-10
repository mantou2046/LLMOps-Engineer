"""W7 D5 · 微调 / RAG / Prompt 策略选择器（可解释规则，不含任何模型调用）。

为什么是规则而不是「让 LLM 判」：和 W4 D6 的分流器同一条纪律 ——
为省一次调用先花一次调用不划算，而且模型判决策不可复现、没法回放对账。
这里只有 if 和证据指针，同输入永远同结论。

⚠️ 证据纪律（任务卡 11/20 的硬要求）：
    每条推荐都必须带**非空的证据指针**，指向 W6/W7 已落库的实测报告；
    凡涉及「微调」的推荐必须同时带上「业务效果未量」的 caveat ——
    D4 已在 Kaggle T4 跑通真 LoRA 并量到小样本格式行为，但那是 40 条合成样本，
    不等于真实任务收益，不许含糊过去写成「微调有效」。

证据索引（全部来自本仓已提交报告，不是编的）：
  · E-RAG-1   W6 D2：RRF 融合 hit@1 7/10→10/10、MRR 0.825→1.0
  · E-RAG-2   W6 D4 全量 50 题 RAGAS 四项 0.9434/0.4768/0.8113/0.8591
  · E-RAG-3   W6 D6 非 LLM 门禁 IDBasedContextRecall@5 0.75（全量 0.8421，阈值 0.65）
  · E-RAG-4   W6 D2 rerank 精排 top-5 净增 2.2–2.4s → 默认不开
  · E-PROMPT-1 W6 D5 单变量 A/B：relevancy Δ +0.0046 < 噪声标尺 +0.0074，判定不采纳
  · E-PROMPT-2 W7 D2 输出护栏：JSON Schema 校验 + 最多修一次 + 固定兜底
  · E-FT-1    W7 D4 数学实跑：LoRA r=4 参数 12.5%、产物 12.8%、r=1 学不全
  · E-FT-2    W7 D4：Kaggle T4 云端真 LoRA，留出 20 条格式符合率 0%→100%（**业务效果未量**）
  · E-COST-1  W7 D3 压测：每千次成本点数 1754.6→626.5（−64.3%），缓存命中 28.4%
  · E-GUARD-1 W7 D2 量化评估：单次判定 P50 1.849s / P95 10.004s、+513 tokens，默认关闭
"""

from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_INDEX: dict[str, str] = {
    "E-RAG-1": (
        "W6 D2 · RRF 融合 hit@1 7/10→10/10、MRR 0.825→1.0"
        "（evals/RAGAS_BASELINE.md / 任务卡）"
    ),
    "E-RAG-2": (
        "W6 D4 全量 50 题 RAGAS 四项 0.9434 / 0.4768 / 0.8113 / 0.8591"
        "（evals/RAGAS_BASELINE.md）"
    ),
    "E-RAG-3": (
        "W6 D6 非 LLM 门禁 IDBasedContextRecall@5 0.75、全量 0.8421、阈值 0.65"
        "（evals/CI_EVAL_GATE.md）"
    ),
    "E-RAG-4": (
        "W6 D2 rerank 精排 top-5 净增 2.2–2.4s → 默认不开"
        "（evals/RERANK_RECHECK.md）"
    ),
    "E-PROMPT-1": (
        "W6 D5 单变量 A/B：relevancy Δ +0.0046 < 噪声 +0.0074，判定不采纳"
        "（evals/ATTRIBUTION.md）"
    ),
    "E-PROMPT-2": (
        "W7 D2 输出护栏：JSON Schema 校验 + 最多修一次 + 固定兜底"
        "（evals/OUTPUT_GUARD_BENCH.md）"
    ),
    "E-FT-1": (
        "W7 D4 数学实跑：LoRA r=4 参数 12.5% / 产物 12.8%，r=1 学不全"
        "（evals/LORA_DEMO.md）"
    ),
    "E-FT-2": (
        "W7 D4 云端补跑（2026-11-10）：Kaggle T4 / Qwen2.5-0.5B + LoRA r=8，"
        "留出 20 条格式符合率与准确率 0%→100%、训练 8.2s / 峰值显存 2890MB；"
        "**小样本格式证据已量、业务效果未量**（evals/LORA_DEMO.md §5）"
    ),
    "E-COST-1": (
        "W7 D3 压测：每千次成本点数 1754.6→626.5（−64.3%）、缓存命中 28.4%"
        "（evals/COST_BENCH.md）"
    ),
    "E-GUARD-1": (
        "W7 D2 量化评估：单次判定 P50 1.849s / P95 10.004s、+513 tokens，默认关闭"
        "（evals/OUTPUT_GUARD_BENCH.md）"
    ),
}

FT_CAVEAT = (
    "真微调业务效果未量：D4 已在 Kaggle T4 跑通 LoRA（留出 20 条格式符合率 0%→100%），"
    "但只有 40 条合成样本，未在真实任务上做 A/B；上生产前需补真实评估集"
)


@dataclass(frozen=True)
class TaskFeatures:
    """一个待决策任务的六个可观察特征。全部是布尔，便于人工核对与回放。"""

    name: str
    knowledge_changes: bool = False      # 事实/制度经常更新
    needs_traceability: bool = False     # 答案要能追溯到原文
    private_knowledge: bool = False      # 涉及私有/外部知识，模型权重里没有
    behavior_unstable: bool = False      # 格式/风格/工具选择长期不稳定
    has_labeled_samples: bool = False   # 有足量高质量任务样本
    rules_simple: bool = False           # 规则能用指令说清


@dataclass(frozen=True)
class Recommendation:
    primary: str                         # 主键："Prompt" / "RAG" / "微调" / "缓存与护栏"
    also: tuple[str, ...]                # 需要叠加的手段
    reason: str
    evidence: tuple[str, ...]            # 证据编号，**必须非空**
    caveats: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "primary": self.primary,
            "also": list(self.also),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "caveats": list(self.caveats),
        }


def decide(f: TaskFeatures) -> Recommendation:
    """按优先级判，先处理「用错工具最贵」的情况。

    顺序不是随便排的：
      1. 知识会变 / 要可追溯 / 私有知识 → 必须 RAG。用微调记知识是经典反模式，
         而且新资料更新检索库比重新训练便宜得多、还能追溯。
      2. 行为长期不稳定且有高质量样本 → 才轮到微调（并且要带未量 caveat）。
      3. 规则能说清 → Prompt 起步，配结构校验。
      4. 兜底：先 Prompt，不行再 RAG，最后才评估微调。
    """
    if f.knowledge_changes or f.needs_traceability or f.private_knowledge:
        also = ["Prompt 结构校验"] if f.behavior_unstable else []
        evidence = ["E-RAG-1", "E-RAG-2", "E-RAG-3"]
        if f.behavior_unstable:
            evidence.append("E-PROMPT-2")
        return Recommendation(
            primary="RAG",
            also=tuple(also),
            reason=(
                "知识会更新 / 需要可追溯 / 是私有知识：把最新资料检索给模型，"
                "答案能追到原文；格式问题另用结构校验解，别拿微调补知识。"
            ),
            evidence=tuple(evidence),
            caveats=("rerank 精排净增 2.2–2.4s，本项目默认不开（E-RAG-4）",),
        )

    if f.behavior_unstable and f.has_labeled_samples:
        return Recommendation(
            primary="微调",
            also=("Prompt 结构校验", "回归评估集"),
            reason=(
                "稳定的格式/风格/工具选择行为，且有足量高质量样本："
                "用例子把行为训进去，比每次都在 prompt 里重复强调更稳。"
                "LoRA 只训低秩增量，可插拔、产物小。"
            ),
            evidence=("E-FT-1", "E-FT-2", "E-PROMPT-1"),
            caveats=(
                FT_CAVEAT,
                "先证明确实是「行为不稳」而不是「指令没写清」——W6 D5 改 prompt 的 A/B 已证伪一次",
                "微调要承担训练、部署与回归评估成本；上线不作要求",
            ),
        )

    if f.behavior_unstable and not f.has_labeled_samples:
        return Recommendation(
            primary="Prompt",
            also=("结构校验", "攒高质量样本"),
            reason=(
                "行为不稳但没有样本：先写清指令 + 加结构校验，边跑边攒样本；"
                "样本够了再评估微调。没有样本就上微调等于没有训练集。"
            ),
            evidence=("E-PROMPT-2", "E-PROMPT-1", "E-FT-2"),
            caveats=(FT_CAVEAT,),
        )

    if f.rules_simple:
        return Recommendation(
            primary="Prompt",
            also=("结构校验",),
            reason="规则简单、能用指令或少量范例说清：改起来最快、零训练成本。",
            evidence=("E-PROMPT-1", "E-PROMPT-2"),
            caveats=("别指望改 prompt 一定提分：W6 D5 的单变量 A/B 判定不采纳（E-PROMPT-1）",),
        )

    return Recommendation(
        primary="Prompt",
        also=("必要时叠加 RAG", "回归评估集"),
        reason=(
            "默认阶梯：先 Prompt（最便宜、最快验证）→ 不够再 RAG（补知识）"
            "→ 最后才评估微调（改行为）。每一步都要有评估集兜底。"
        ),
        evidence=("E-PROMPT-1", "E-RAG-2", "E-FT-2", "E-COST-1"),
        caveats=(FT_CAVEAT, "成本敏感场景可叠加 W7 D3 的精确缓存与分流（E-COST-1）"),
    )


def validate(rec: Recommendation) -> list[str]:
    """证据纪律检查：结论不能空口讲，微调不能不带未量声明。"""
    problems: list[str] = []
    if not rec.evidence:
        problems.append("结论没有证据指针")
    for e in rec.evidence:
        if e not in EVIDENCE_INDEX:
            problems.append(f"未知证据编号：{e}")
    if "微调" in rec.primary and not any("效果未量" in c for c in rec.caveats):
        problems.append("推荐微调却没写「业务效果未量」声明")
    return problems
