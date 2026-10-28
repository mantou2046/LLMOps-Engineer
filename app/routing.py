"""W4 D6 · 应用侧规则分流器（`model=auto` 时决定走哪个档位）

职责边界（W4 D5 的 DEC-032 之后补的一条）：

  · **网关（LiteLLM）管「怎么送到」**：`primary` 这个别名对应哪个真实模型、
    挂了切谁（fallbacks）、命中缓存没（cache）。属于传输层。
  · **本模块管「送哪个档位」**：这条请求该用 `primary`（贵/强）还是 `cheap`（便宜）。
    属于业务判断 —— 网关看不懂「你这个问题难不难」，只有应用知道。

所以「分流」永远在应用侧判，网关只负责「primary 这个别名对应谁」。

为什么**不用 LLM 判路由**（任务卡点名的坑）：
  用模型判路由 = 为了省一次调用先花一次调用。判一次要 0.5–2s、要花钱、
  还可能判错且不可复现（同样的问题两次判出两个档位，没法回放、没法对账）。
  规则判据是微秒级、零成本、可解释、**同输入永远同结论**。
  所以本文件只有 if 和计分，**没有任何模型调用**（连 embedding 都不用）。

判据（全部可解释、可回放）：

  | 信号 | 权重 | 为什么它意味着「复杂」 |
  | --- | --- | --- |
  | `structured=True` | +3 | 要守 JSON 格式，轻模型更容易破格式 |
  | 长度 ≥ 120 字 | +2 | 长题面通常要多步推理 |
  | 长度 ≥ 40 字 | +1 | 中等长度，给一点分 |
  | 多轮（messages ≥ 3 条） | +1 | 上下文长了，轻模型容易丢前文 |
  | **强信号词**（区别/对比/分析/为什么/评估/合规/风险/优化/取舍/排查/梳理/方案…） | +3 | 出现一个就基本是「要推理」，不再看别的 |
  | 弱信号词（步骤/影响/原因/依据/条款/关系/证明/如何设计…） | 每个 +1，封顶 +2 | 可能只是「问清单」，单独出现不足以下结论 |
  | 引用材料（"根据以下材料"/"制度原文"/"附件"…） | +2 | 长上下文 + 要求对齐原文 |
  | 简单词（是什么/多少/定义/怎么填/在哪/叫什么…） | **-1**，且只在**没有**强/弱信号词时生效 | 防止「这个字段是什么意思」被长度误判成复杂 |

  合计 ≥ `ROUTE_PRIMARY_THRESHOLD`（默认 3）→ primary，否则 cheap。

  简单词为什么带「只在无复杂词时生效」这个前提：真实问法里常见
  「A 和 B 是什么关系？请对比两者的记账时点」—— 里面既有 `是什么`（简单）
  又有 `对比`（强）。若简单词无条件减分，这种题会被拉回 cheap，正好判反。

调参口径：阈值与长度门限都是 env 可覆盖的（见下方常量），改之前先跑
`evals/route_probe.py` 看「便宜档占比 / 与期望不符的条目」，别凭感觉调。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ---- 可调参数（env 覆盖，便于不重启代码做对照实验）-------------------------
ROUTE_PRIMARY_THRESHOLD = int(os.getenv("ROUTE_PRIMARY_THRESHOLD", "3"))
ROUTE_LONG_CHARS = int(os.getenv("ROUTE_LONG_CHARS", "120"))
ROUTE_MID_CHARS = int(os.getenv("ROUTE_MID_CHARS", "40"))

# ---- 词表 ------------------------------------------------------------------
# ⚠️ 词表只放「问法特征」，不放业务名词。业务名词（回单/票据/资金池）每条问题都有，
#    拿它当信号等于全判复杂 —— 这正是 D5 决策里「标签不该记具体名词」的同一条道理。
STRONG_KEYWORDS = (
    "区别", "对比", "比较", "分析", "为什么", "为何", "推导", "计算",
    "评估", "合规", "风险", "优化", "取舍", "排查", "梳理", "方案",
)

WEAK_KEYWORDS = (
    "步骤", "影响", "原因", "依据", "条款", "关系", "证明", "如何设计", "怎么设计",
)

SIMPLE_KEYWORDS = (
    "是什么", "是多少", "多少", "几号", "定义", "怎么填", "在哪", "叫什么",
    "是什么意思", "有没有",
)

REFERENCE_KEYWORDS = (
    "根据以下材料", "以下材料", "以下文档", "附件", "制度原文", "条款原文",
    "给出的材料", "上面这段", "这段话",
)


@dataclass(frozen=True)
class RouteDecision:
    """一次分流判定的完整现场 —— 结论 + 为什么 + 可回放的中间量。

    字段刻意留全：出问题时能回答「它凭什么判成 cheap」，
    而不是只有一句 tier=cheap 让人对着日志猜。
    """

    tier: str            # "primary" | "cheap"
    score: int           # 计分（≥ 阈值 → primary）
    signals: tuple[str, ...]   # ASCII 信号码（可安全写进响应头）
    detail: str          # 中文说明（只进 body / 日志，不进响应头）
    chars: int           # 全部消息正文总字数
    turns: int           # 消息条数
    explicit: bool = False     # 调用方显式点名档位（不判，直接照办）

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "score": self.score,
            "signals": list(self.signals),
            "detail": self.detail,
            "chars": self.chars,
            "turns": self.turns,
            "explicit": self.explicit,
        }


def _hits(text: str, words: tuple[str, ...]) -> list[str]:
    """命中了哪些词（保序、去重）。返回词本身，便于写清「凭哪个词判的」。"""
    seen: list[str] = []
    for w in words:
        if w in text and w not in seen:
            seen.append(w)
    return seen


def decide(
    messages: list[dict],
    *,
    structured: bool = False,
    explicit: str | None = None,
) -> RouteDecision:
    """判档。`explicit` 为 "primary" / "cheap" 时**不判**，直接照办。

    调用方显式点名必须优先于任何规则 —— 分流器是「默认值」，不是「独裁者」：
    评估脚本要固定档位做对照、或者人工要强制走贵模型，都得能绕过规则。
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    turns = len(messages)

    if explicit in ("primary", "cheap"):
        return RouteDecision(
            tier=explicit,
            score=0,
            signals=("explicit",),
            detail=f"调用方显式指定 {explicit}，规则未参与",
            chars=chars,
            turns=turns,
            explicit=True,
        )

    text = "\n".join(str(m.get("content", "")) for m in messages)
    score = 0
    signals: list[str] = []
    notes: list[str] = []

    if structured:
        score += 3
        signals.append("structured")
        notes.append("要结构化 JSON +3")

    if chars >= ROUTE_LONG_CHARS:
        score += 2
        signals.append(f"len>={ROUTE_LONG_CHARS}")
        notes.append(f"长度 {chars} 字 +2")
    elif chars >= ROUTE_MID_CHARS:
        score += 1
        signals.append(f"len>={ROUTE_MID_CHARS}")
        notes.append(f"长度 {chars} 字 +1")

    if turns >= 3:
        score += 1
        signals.append(f"turns={turns}")
        notes.append(f"多轮 {turns} 条 +1")

    strong = _hits(text, STRONG_KEYWORDS)
    if strong:
        score += 3
        signals.append(f"strong={len(strong)}")
        notes.append(f"强信号词 {('/'.join(strong[:3]))} +3")

    weak = _hits(text, WEAK_KEYWORDS)
    if weak:
        add = min(len(weak), 2)
        score += add
        signals.append(f"weak={len(weak)}")
        notes.append(f"弱信号词 {('/'.join(weak[:3]))} +{add}")

    ref = _hits(text, REFERENCE_KEYWORDS)
    if ref:
        score += 2
        signals.append("ref")
        notes.append(f"引用材料「{ref[0]}」+2")

    sm = _hits(text, SIMPLE_KEYWORDS)
    if sm and not strong and not weak:
        score -= 1
        signals.append("simple")
        notes.append(f"简单词「{sm[0]}」-1")

    tier = "primary" if score >= ROUTE_PRIMARY_THRESHOLD else "cheap"
    detail = f"score={score}（阈值 {ROUTE_PRIMARY_THRESHOLD}）→ {tier}；" + (
        "、".join(notes) if notes else "无信号命中（短问题默认走便宜档）"
    )
    return RouteDecision(
        tier=tier,
        score=score,
        signals=tuple(signals) or ("none",),
        detail=detail,
        chars=chars,
        turns=turns,
    )
