"""W7 D2 输出护栏：验证结构、遮盖演示范围内的个人信息，并安全降级。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}

SAFE_FALLBACK = {
    "answer": "暂时无法生成符合要求的回答，请稍后重试。",
    "citations": [],
}


class Answer(BaseModel):
    """对外 JSON 回答的白名单格式。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class OutputGuardResult:
    output: dict[str, Any]
    degraded: bool
    repaired: bool
    redacted_types: list[str]
    toxicity_status: str
    grounding_status: str
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_pii(text: str) -> tuple[str, list[str]]:
    """遮盖邮箱和中国大陆手机号；返回本次命中的类型，不返回原文。"""
    found: list[str] = []
    for kind, pattern in PII_PATTERNS.items():
        text, count = pattern.subn(f"[已遮盖:{kind}]", text)
        if count:
            found.append(kind)
    return text, found


def _to_json_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    raise TypeError("模型回答必须是 JSON 字符串或对象")


def _parse_answer(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise TypeError("模型回答必须是 JSON 对象")
    return Answer.model_validate(raw).model_dump()


def _fallback(
    reason: str,
    *,
    repaired: bool = False,
    redacted_types: list[str] | None = None,
    toxicity_status: str = "not_checked",
    grounding_status: str = "not_checked",
) -> OutputGuardResult:
    return OutputGuardResult(
        output=SAFE_FALLBACK.copy(),
        degraded=True,
        repaired=repaired,
        redacted_types=redacted_types or [],
        toxicity_status=toxicity_status,
        grounding_status=grounding_status,
        reasons=[reason],
    )


def inspect_output(
    raw: object,
    *,
    repair: Callable[[str, str], object] | None = None,
    toxicity_judge: Callable[[str], str] | None = None,
    grounding_judge: Callable[[str, str], bool] | None = None,
    context: str | None = None,
) -> OutputGuardResult:
    """检查模型回答。修复至多一次；检查器报错时不把原回答当作安全结果。"""
    try:
        raw_text, detected = redact_pii(_to_json_text(raw))
        parsed = _parse_answer(raw_text)
    except (TypeError, ValueError, ValidationError) as first_error:
        if repair is None:
            return _fallback("invalid_format")
        try:
            repair_input = raw_text if "raw_text" in locals() else str(raw)
            repaired_raw = repair(repair_input, str(first_error))
            repaired_text, repaired_types = redact_pii(_to_json_text(repaired_raw))
            parsed = _parse_answer(repaired_text)
        except Exception:
            return _fallback(
                "format_repair_failed",
                repaired=True,
                redacted_types=detected if "detected" in locals() else [],
            )
        detected.extend(kind for kind in repaired_types if kind not in detected)
        repaired_once = True
    else:
        repaired_once = False

    answer, answer_types = redact_pii(parsed["answer"])
    detected.extend(kind for kind in answer_types if kind not in detected)
    citations: list[str] = []
    for citation in parsed["citations"]:
        safe_citation, citation_types = redact_pii(citation)
        citations.append(safe_citation)
        detected.extend(kind for kind in citation_types if kind not in detected)
    parsed["answer"] = answer
    parsed["citations"] = citations

    reasons: list[str] = []
    if detected:
        reasons.append("pii_redacted")
    if repaired_once:
        reasons.append("format_repaired")

    toxicity_status = "not_checked"
    if toxicity_judge is not None:
        try:
            decision = toxicity_judge(answer)
            if decision not in {"allow", "block"}:
                raise ValueError("内容检查器只能返回 allow 或 block")
        except Exception:
            return _fallback(
                "toxicity_check_error",
                repaired=repaired_once,
                redacted_types=detected,
                toxicity_status="error",
            )
        toxicity_status = decision
        if decision == "block":
            return _fallback(
                "harmful_content",
                repaired=repaired_once,
                redacted_types=detected,
                toxicity_status="block",
            )

    grounding_status = "not_checked"
    if context and grounding_judge is not None:
        try:
            supported = grounding_judge(answer, context)
            if not isinstance(supported, bool):
                raise ValueError("资料依据检查器必须返回布尔值")
        except Exception:
            return _fallback(
                "grounding_check_error",
                repaired=repaired_once,
                redacted_types=detected,
                toxicity_status=toxicity_status,
                grounding_status="error",
            )
        grounding_status = "supported" if supported else "unsupported"
        if not supported:
            return _fallback(
                "unsupported_claim",
                repaired=repaired_once,
                redacted_types=detected,
                toxicity_status=toxicity_status,
                grounding_status="unsupported",
            )

    return OutputGuardResult(
        output=parsed,
        degraded=repaired_once,
        repaired=repaired_once,
        redacted_types=detected,
        toxicity_status=toxicity_status,
        grounding_status=grounding_status,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# W7 D2 真实模型判定器：默认不启用，由 /chat 的 OUTPUT_GUARD_MODEL_JUDGE_ENABLED
# 注入 inspect_output。判不出来、格式不合约定、上游报错时，调用方一律安全兜底。
# ---------------------------------------------------------------------------

def _judge_objects(content: object, label: str) -> list[dict]:
    """取出返回里所有顶层 JSON 对象。

    实测 primary 档模型会把同一个结论重复输出两三次、或前后夹解释文字；
    这里逐个扫描而不是整段 json.loads，避免把「格式包装」误判成「判定失败」。
    字段契约和取值枚举仍在 _parse_judge_json 里严格校验，没有放宽安全口径。
    """
    if isinstance(content, dict):
        return [content]
    if not isinstance(content, str):
        raise ValueError(f"{label}判定结果必须是 JSON 对象或字符串")

    decoder = json.JSONDecoder()
    found: list[dict] = []
    pos = 0
    while pos < len(content):
        brace = content.find("{", pos)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(content, brace)
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        pos = max(end, brace + 1)
    if not found:
        raise ValueError(f"{label}判定结果里没有可解析的 JSON 对象")
    return found


def _parse_judge_json(
    content: object, *, allowed: set[str], label: str
) -> tuple[str, str]:
    """只接受约定字段与取值的判定结果；格式不合规就抛错，绝不猜一个安全结论。

    返回里若有多个 JSON 对象（模型重复输出），要求每个都合规且结论一致；
    出现互相矛盾的结论时拒绝，由调用方安全兜底。
    """
    decisions: list[str] = []
    reason = ""
    for obj in _judge_objects(content, label):
        if set(obj) != {"decision", "reason"}:
            raise ValueError(f"{label}判定结果字段不符合约定")
        decision = obj["decision"]
        if decision not in allowed:
            raise ValueError(f"{label}判定结果 decision 不在允许范围内")
        candidate = obj["reason"]
        if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 120:
            raise ValueError(f"{label}判定结果 reason 必须是 1 到 120 个字符")
        decisions.append(decision)
        reason = candidate.strip()

    if len(set(decisions)) != 1:
        raise ValueError(f"{label}判定结果前后结论不一致")
    return decisions[0], reason


def parse_toxicity_response(content: object) -> str:
    """把毒性判定结果收敛成 allow / block。"""
    decision, _ = _parse_judge_json(content, allowed={"allow", "block"}, label="毒性")
    return decision


def parse_grounding_response(content: object) -> bool:
    """把资料依据判定结果收敛成布尔值。"""
    decision, _ = _parse_judge_json(
        content, allowed={"supported", "unsupported"}, label="资料依据"
    )
    return decision == "supported"


def _model_judge(name: str, payload: dict[str, str]) -> object:
    """调用配置好的主模型判一次；mock 模式拒绝冒充真实判断。"""
    import llm

    import prompts

    if llm.LLM_MODE != "openai":
        raise RuntimeError("输出护栏判定器需要真实模型；当前不是 LLM_MODE=openai")

    system_prompt = prompts.load_prompt_local(name, version=1)["template"]
    return llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        tier_name="primary",
        structured=False,
        temperature=0,
        # 128 会把 reason 截断，实测造成判定结果半截 JSON；放宽到 256。
        max_tokens=256,
    ).content


def judge_toxicity(answer: str) -> str:
    """真实毒性判定，只返回 allow / block；异常由 inspect_output 兜底。"""
    return parse_toxicity_response(_model_judge("output_toxicity", {"answer": answer}))


def judge_grounding(answer: str, context: str) -> bool:
    """真实资料依据判定，只返回是否 supported。"""
    return parse_grounding_response(
        _model_judge("output_grounding", {"answer": answer, "context": context})
    )
