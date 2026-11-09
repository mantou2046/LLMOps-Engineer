"""离线可复现的输入护栏首轮基线。

它只检查用户消息：先遮盖邮箱/中国大陆手机号，再用少量规则拦截明显越权指令。
规则用于建立基线和演示，不代替模型判定、服务端权限校验或完整的安全方案。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}

INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_rules",
        re.compile(
            r"(?:忽略|忘记|无视).{0,12}(?:之前|前面|以上|原有|系统).{0,8}(?:规则|指令|要求)|"
            r"ignore (?:all )?(?:previous|prior) instructions",
            re.I,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"(?:说出|输出|显示|泄露|reveal|print|show).{0,16}(?:系统提示|隐藏指令|"
            r"system prompt|hidden instructions)",
            re.I,
        ),
    ),
    (
        "privilege_claim",
        re.compile(
            r"(?:我是|自称|i am|i'm)\s*(?:系统)?管理员.{0,24}(?:其他用户|所有用户|全部用户)"
            r".{0,12}(?:资料|数据|记录)|(?:admin|administrator).{0,24}(?:all users|other users)"
            r".{0,16}(?:records|data)",
            re.I,
        ),
    ),
    (
        "secret_extraction",
        re.compile(
            r"(?:读取|导出|打印|查找|dump|export|read|print).{0,24}(?:密钥|token|环境变量|"
            r"secret|api key|env(?:ironment)? vars?)",
            re.I,
        ),
    ),
    (
        "role_bypass",
        re.compile(
            r"(?:扮演|假装|roleplay|pretend).{0,20}(?:不受限制|没有规则|无视安全|"
            r"no rules|unrestricted)",
            re.I,
        ),
    ),
    (
        "document_override",
        re.compile(
            r"(?:文档|材料|文件|document|file).{0,20}(?:最高级命令|系统指令|覆盖系统|"
            r"highest[- ]priority|system instruction)",
            re.I,
        ),
    ),
)


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    messages: list[dict[str, str]]
    reasons: list[str]
    redacted_types: list[str]


def redact_pii(text: str) -> tuple[str, list[str]]:
    """遮盖演示范围内的邮箱和中国大陆手机号，并返回命中类型。"""
    found: list[str] = []
    for kind, pattern in PII_PATTERNS.items():
        text, count = pattern.subn(f"[已遮盖:{kind}]", text)
        if count:
            found.append(kind)
    return text, found


def inspect_messages(
    messages: list[dict[str, str]],
    judge: Callable[[str], object] | None = None,
) -> GuardResult:
    """检查用户消息；被拦截时只返回规则编号，不把原文放进日志字段。"""
    safe_messages: list[dict[str, str]] = []
    reasons: list[str] = []
    redacted: list[str] = []

    for message in messages:
        safe_message = dict(message)
        if message.get("role") == "user":
            safe_text, kinds = redact_pii(message.get("content", ""))
            redacted.extend(kind for kind in kinds if kind not in redacted)
            safe_message["content"] = safe_text
            matched_rule = False
            for rule_name, pattern in INJECTION_RULES:
                if pattern.search(safe_text):
                    matched_rule = True
                    if rule_name not in reasons:
                        reasons.append(rule_name)

            if not matched_rule and judge is not None:
                try:
                    verdict = parse_judge_response(judge(safe_text))
                except Exception:
                    # 判定器报错或返回格式不合规时，按安全原则拦截。
                    if "classifier_error" not in reasons:
                        reasons.append("classifier_error")
                else:
                    if verdict["decision"] == "block":
                        reason = f"model_{verdict['category']}"
                        if reason not in reasons:
                            reasons.append(reason)
        safe_messages.append(safe_message)

    return GuardResult(
        allowed=not reasons,
        messages=safe_messages,
        reasons=reasons,
        redacted_types=redacted,
    )
JUDGE_CATEGORIES = {
    "prompt_injection",
    "secret_extraction",
    "privilege_escalation",
    "none",
}


def parse_judge_response(content: object) -> dict[str, str]:
    """只接受字段和值都符合约定的模型判定结果。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("判定结果不是合法 JSON") from exc

    if not isinstance(content, dict):
        raise ValueError("判定结果必须是 JSON 对象")

    if set(content) != {"decision", "category", "reason"}:
        raise ValueError("判定结果字段不符合约定")

    decision = content["decision"]
    category = content["category"]
    reason = content["reason"]

    if decision not in {"allow", "block"}:
        raise ValueError("decision 必须是 allow 或 block")
    if category not in JUDGE_CATEGORIES:
        raise ValueError("category 不在允许范围内")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
        raise ValueError("reason 必须是 1 到 120 个字符")

    if decision == "allow" and category != "none":
        raise ValueError("放行时 category 必须是 none")
    if decision == "block" and category == "none":
        raise ValueError("拦截时必须说明攻击类别")

    return {
        "decision": decision,
        "category": category,
        "reason": reason.strip(),
    }
def judge_injection(text: str) -> dict[str, str]:
    """用已配置的模型判定输入；mock 模式下拒绝伪装成真实判断。"""
    import llm

    import prompts

    if llm.LLM_MODE != "openai":
        raise RuntimeError("输入判定器需要真实模型；当前不是 LLM_MODE=openai")

    system_prompt = prompts.load_prompt_local(
        "input_guard",
        version=1,
    )["template"]

    result = llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        tier_name="primary",
        structured=False,
        temperature=0,
        max_tokens=128,
    )
    return parse_judge_response(result.content)
