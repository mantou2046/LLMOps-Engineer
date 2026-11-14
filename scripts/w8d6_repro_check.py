#!/usr/bin/env python3
"""W8 D6: reproducibility health check for a fresh clone.

Task card 11/29 asks for a full-chain regression with one rule: no step may
depend on something the author remembers. This script turns that rule into
machine-checkable assertions:

  1. every file a stranger would copy out of README.md actually exists;
  2. the seven preconditions (proxy, healthy, host address, mock upstream,
     ports, credentials, Python) are written down, not implicit;
  3. relative links in README.md resolve;
  4. the switches that are off by default really are off by default;
  5. the non-LLM eval gate still behaves 0 / 1 / 0 when actually executed.

Stdlib only, zero network, zero credentials, no Docker required. Output is
ASCII so it also works on a GBK Windows console.

Usage (from the repository root):

    python scripts/w8d6_repro_check.py
    python scripts/w8d6_repro_check.py --skip-live   # static checks only
    python scripts/w8d6_repro_check.py --json        # machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RX = re.compile(r"\]\(([^)]+)\)")


@dataclass
class Result:
    label: str
    ok: bool
    detail: str = ""


def read(rel: str) -> str:
    path = ROOT / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def readme_section5() -> str:
    """Only the copy-paste section counts: a hint buried in W1 notes is not enough."""
    body = read("README.md")
    start = body.find("## 5 ")
    if start < 0:
        return ""
    rest = body[start:]
    end = rest.find("\n---", 1)
    return rest if end < 0 else rest[:end]


def section5_markers(label: str, needles: tuple[str, ...]) -> Result:
    body = readme_section5()
    missing = [str(i) for i, needle in enumerate(needles) if needle not in body]
    return Result(label, not missing, "missing token idx " + ",".join(missing) if missing else "")


def exists(rel: str) -> Result:
    ok = (ROOT / rel).exists()
    return Result(f"exists: {rel}", ok, "" if ok else "missing")


def markers(label: str, rel: str, needles: tuple[str, ...]) -> Result:
    body = read(rel)
    missing = [str(i) for i, needle in enumerate(needles) if needle not in body]
    return Result(label, not missing, "missing token idx " + ",".join(missing) if missing else "")


def readme_paths(results: list[Result]) -> None:
    """Every path used by the copy-paste command chain must be named and real."""
    paths = (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-eval.txt",
        "app/Dockerfile",
        "infra/compose.yml",
        "scripts/verify_d2.sh",
        "scripts/check_sensitive.py",
        "scripts/audit_public_repo.py",
        "scripts/w8d6_repro_check.py",
        ".env.example",
        ".githooks",
        "evals/COST_BENCH.md",
        "docs/ARCHITECTURE.md",
        "evals/RAGAS_BASELINE.md",
        "evals/OUTPUT_GUARD_BENCH.md",
        "docs/W7_D5_DECISION.md",
    )
    body = read("README.md")
    for rel in paths:
        ok = rel in body and (ROOT / rel).exists()
        results.append(Result(f"README path: {rel}", ok, "not named or missing"))


def relative_links() -> Result:
    broken: list[str] = []
    count = 0
    for raw in LINK_RX.findall(read("README.md")):
        target = raw.strip().split("|")[0].split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        count += 1
        if not (ROOT / target).exists():
            broken.append(target)
    return Result(
        f"README relative links: {count} checked",
        not broken,
        "broken: " + ",".join(broken) if broken else "",
    )


def env_not_tracked() -> Result:
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    detail = "tracked" if proc.returncode == 0 else ""
    return Result(".env is not tracked", proc.returncode != 0, detail)


def live_gate() -> list[Result]:
    cases = (
        (("scripts/w6d6_eval_gate.py",), 0),
        (("scripts/w6d6_eval_gate.py", "--sabotage"), 1),
        (("scripts/w6d6_eval_gate.py", "--self-test"), 0),
    )
    results: list[Result] = []
    for args, expected in cases:
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        label = "gate " + (" ".join(args[1:]) or "normal") + f" -> {expected}"
        results.append(
            Result(label, proc.returncode == expected, f"got {proc.returncode}")
        )
    return results


def ci_scripts() -> list[Result]:
    body = read(".github/workflows/ci.yml")
    out: list[Result] = []
    for rel in (
        "scripts/check_sensitive.py",
        "scripts/w6d6_eval_gate.py",
        "requirements-dev.txt",
        "requirements.txt",
        "app/Dockerfile",
    ):
        ok = rel in body and (ROOT / rel).exists()
        out.append(Result(f"CI reference: {rel}", ok, "not named or missing"))
    return out


def stages(with_live: bool) -> list[tuple[str, list[Result]]]:
    stage1: list[Result] = [
        exists("README.md"),
        exists("pyproject.toml"),
        exists("docs/env-setup.md"),
        exists(".gitignore"),
        markers(
            "README: 7 preconditions written down",
            "README.md",
            (
                "3.13",
                "9527",
                "healthy",
                "host.docker.internal",
                "FAKE_UPSTREAM_BASE_URL",
                "5432",
                "check_sensitive.py --staged",
            ),
        ),
        section5_markers(
            "README sec.5: env-file, eval deps, password explicit",
            ("--env-file .env", "requirements-eval.txt", "POSTGRES_PASSWORD"),
        ),
        markers(
            ".gitignore: credentials excluded",
            ".gitignore",
            (".env", "scripts/sensitive_terms.txt"),
        ),
        env_not_tracked(),
        markers(
            "CI: Python version stated",
            ".github/workflows/ci.yml",
            ('python-version: "3.12"',),
        ),
    ]

    stage2: list[Result] = [
        exists("app/Dockerfile"),
        exists("app/Dockerfile.naive"),
        exists("infra/compose.yml"),
        exists("scripts/verify_d2.sh"),
        exists("infra/DOCKER-SETUP-Win11.md"),
        markers("compose: healthcheck defined", "infra/compose.yml", ("healthcheck:",)),
        markers("verify_d2.sh: explicit env-file", "scripts/verify_d2.sh", ("--env-file .env",)),
        markers(
            "CI: image build step",
            ".github/workflows/ci.yml",
            ("docker build -f app/Dockerfile",),
        ),
    ]

    stage3: list[Result] = [
        exists("evals/golden_set.jsonl"),
        exists("evals/corpus"),
        exists("scripts/w6d1_ingest_pgvector.py"),
        exists("scripts/w6d1_verify_pgvector.py"),
        exists("scripts/w6d2_hybrid_bench.py"),
        exists("scripts/w6d4_ragas_eval.py"),
        exists("evals/RAGAS_BASELINE.md"),
        exists("docs/ARCHITECTURE.md"),
        markers(
            "README: 5 numbers keep report references",
            "README.md",
            (
                "evals/COST_BENCH.md",
                "docs/ARCHITECTURE.md",
                "evals/RAGAS_BASELINE.md",
                "evals/OUTPUT_GUARD_BENCH.md",
                "docs/W7_D5_DECISION.md",
            ),
        ),
    ]

    stage4: list[Result] = [
        exists("scripts/w6d6_eval_gate.py"),
        exists("evals/gate/config.json"),
        exists("evals/gate/query_template.txt"),
        exists("evals/CI_EVAL_GATE.md"),
        exists("tests/test_w6d6_gate.py"),
    ]
    if with_live:
        stage4.extend(live_gate())

    stage5: list[Result] = [
        exists("app/input_guard.py"),
        exists("app/output_guard.py"),
        exists("app/cost.py"),
        exists("app/routing.py"),
        exists("evals/OUTPUT_GUARD_BENCH.md"),
        exists("evals/COST_BENCH.md"),
        exists("evals/ACCEPTANCE_W7.md"),
        markers(
            "app: model judge off by default",
            "app/main.py",
            (
                "OUTPUT_GUARD_MODEL_JUDGE_ENABLED",
                'os.getenv("OUTPUT_GUARD_MODEL_JUDGE_ENABLED", "0")',
            ),
        ),
        markers(
            "app: cost switches off by default",
            "app/main.py",
            ('os.getenv("COST_CACHE_ENABLED", "0")', 'os.getenv("COST_LEDGER_ENABLED", "0")'),
        ),
    ]

    stage6: list[Result] = [
        exists("app/agent_graph.py"),
        exists("app/observability.py"),
        exists("scripts/verify_trace.py"),
        exists("scripts/verify_langfuse_trace.py"),
        exists("evals/AGENT_GRAPH.md"),
        exists("evals/AGENT_DEMO.md"),
    ]

    stage7: list[Result] = [
        markers(
            "README: five sections in order",
            "README.md",
            ("## 1 ", "## 2 ", "## 3 ", "## 4 ", "## 5 "),
        ),
        markers("README: 30-second nav table", "README.md", ("30 ",)),
        relative_links(),
        exists("scripts/check_sensitive.py"),
        exists("scripts/audit_public_repo.py"),
        exists("docs/REPO_AUDIT.md"),
        exists("docs/W8_REPRO_AUDIT.md"),
        exists("docs/blog/01-noise-ruler.md"),
        exists("docs/demo/w8d3-demo-final.mp4"),
    ]
    readme_paths(stage7)
    stage7.extend(ci_scripts())
    stage7.append(
        section5_markers(
            "README sec.5: repro check in the command chain",
            ("python scripts/w8d6_repro_check.py",),
        )
    )

    return [
        ("1 Environment / preconditions", stage1),
        ("2 Container build", stage2),
        ("3 Ingest and retrieval", stage3),
        ("4 Eval gate", stage4),
        ("5 Guardrails and cost switches", stage5),
        ("6 Trace", stage6),
        ("7 Docs, links and safety", stage7),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="W8 D6 full-chain reproducibility check")
    parser.add_argument("--skip-live", action="store_true", help="skip executing the eval gate")
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    args = parser.parse_args()

    all_stages = stages(not args.skip_live)
    total = passed = 0
    failures: list[str] = []
    stage_summaries: list[dict[str, object]] = []

    for title, results in all_stages:
        stage_ok = 0
        for result in results:
            total += 1
            if result.ok:
                passed += 1
                stage_ok += 1
            else:
                failures.append(f"{title} :: {result.label} :: {result.detail}")
        stage_summaries.append(
            {"stage": title, "passed": stage_ok, "total": len(results)}
        )
        if not args.json:
            state = "PASS" if stage_ok == len(results) else "FAIL"
            print(f"[{state}] {title:<40} {stage_ok}/{len(results)}")

    if args.json:
        print(
            json.dumps(
                {
                    "passed": passed,
                    "total": total,
                    "stages": stage_summaries,
                    "failures": failures,
                },
                ensure_ascii=True,
            )
        )
    else:
        for line in failures:
            print("  FAIL  " + line)
        print(f"TOTAL {passed}/{total} PASS")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
