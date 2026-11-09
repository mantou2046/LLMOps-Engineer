from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "guardrails" / "output_cases.jsonl"


def demo_toxicity_judge(text: str) -> str:
    """仅用于离线演示的假判定器，不是现实世界的毒性检测器。"""
    if "去死吧" in text and "引用" not in text:
        return "block"
    return "allow"


def demo_grounding_judge(answer: str, context: str) -> bool:
    """仅演示「资料里找不到答案数字就不通过」的流程。"""
    return "999" not in answer or "999" in context


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
    from output_guard import inspect_output

    passed = 0
    for line in CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        result = inspect_output(
            case["raw_output"],
            toxicity_judge=demo_toxicity_judge,
            context=case.get("context"),
            grounding_judge=demo_grounding_judge,
        )
        if "harmful_content" in result.reasons:
            action = "block"
        elif "unsupported_claim" in result.reasons:
            action = "unsupported"
        elif result.redacted_types:
            action = "redact"
        elif result.degraded:
            action = "fallback"
        else:
            action = "pass"
        ok = action == case["expected_action"]
        passed += int(ok)
        print(f"{case['id']} {action:11} {'PASS' if ok else 'FAIL'} · {case['reason']}")
    print(f"结果：{passed}/{10} 条样例符合预期")
    if passed != 10:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
