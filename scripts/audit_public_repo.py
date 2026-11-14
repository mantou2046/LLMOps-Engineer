#!/usr/bin/env python3
"""公开仓库脱敏终检：当前跟踪文件 + 全部提交历史 + 历史文件名。

用法
----
    python scripts/audit_public_repo.py                  # 全量（工作区 + 已公开历史 + 历史文件名）
    python scripts/audit_public_repo.py --worktree      # 只扫当前跟踪文件
    python scripts/audit_public_repo.py --history       # 只扫提交历史
    python scripts/audit_public_repo.py --history --all-refs
                                                        # 连本地未推送分支一起扫

退出码
------
    0 = 干净
    1 = 有命中（不自动修，先人工判定）
    2 = 脚本执行失败（git 命令报错等）

安全约定
--------
* 只输出「提交号 / 文件 / 命中类型」，**绝不回显命中原文** —— 检查工具本身不能成为泄露渠道。
* 规则与 `scripts/check_sensitive.py` 同源：模式、占位符豁免、连接串与键值判定全部直接 import，
  那边改了内置规则本脚本自动跟随；只有 KV 的豁免链（引用型 / 配置型键）在此复述一遍。
* 自定义词表读本地 `scripts/sensitive_terms.txt`（已 gitignore，真实词条不进公开仓库）。
* 历史扫描看的是每个提交的 **+ 新增行**，所以「后来删掉的文件」也照样查得到。

背景（2026-11-14 · W8 D5）：提交前门禁只扫暂存区、CI 只扫当前全树，
两者都看不见「曾经提交过、后来删掉」的内容。GitHub 官方指南明确：删文件不够，历史里还在。
本脚本补的就是这一层，用于公开仓库的定期终检，不替代 `check_sensitive.py` 的提交门禁。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_sensitive as cs  # noqa: E402  （规则正本，故意后置于 sys.path 调整）

MARK = "@@AUDIT-COMMIT@@ "

# 历史里出现过的「敏感命名」文件：即便内容干净也值得人工看一眼
# （.env.example / *.example.* / secret.yaml 这类模板不算，用负向排除）
RISKY_NAME = re.compile(
    r"(?i)(^|/)(\.env(\.local|\.production|\.prod)?$|\.env\.[^.]+$"
    r"|.*\.tfstate.*|.*\.kdbx$|.*\.pem$|.*\.key$|.*\.p12$|.*\.pfx$"
    r"|credentials\.json$|service-account.*\.json$)"
)
NAME_EXEMPT = re.compile(
    r"(?i)(\.example\.|\.example$|/(example|sample|template)s?/|secret\.ya?ml$|values-secret\.example)"
)

BINARY_HINT = re.compile(r"(?i)\.(png|jpe?g|gif|svg|webp|pdf|zip|gz|whl|xlsx?|docx?|pptx?|"
                         r"pt|pth|bin|safetensors|gguf|onnx|so|dll|exe|pyc|parquet|sqlite|db|"
                         r"mp4|mp3|woff2?|ttf|ico|tar)$")


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print(f"audit: git {' '.join(args)} 失败：{r.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def patterns():
    return cs.PATTERNS + cs.build_custom_patterns(cs.load_custom_terms())


def scan_line(line: str, pats) -> list[str]:
    """与 cs.scan_file 同一套判定，只返回命中类型名，不返回原文。"""
    for name, rx in pats:
        if rx.search(line):
            return [name]                      # 与门禁一致：通用模式命中即停
    mc = cs.CONN_RX.search(line)
    if mc and not (cs.is_placeholder(mc.group(1)) and cs.is_placeholder(mc.group(2))):
        return ["带口令的连接串"]
    m = cs.KV.search(line)
    if (
        m
        and m.group(2).strip().rstrip(")]}>.,:;") != m.group(1).strip()
        and not cs.is_placeholder(m.group(2))
        and not cs._KEY_IS_REF.match(m.group(1).strip())
        and not cs._KEY_IS_CONFIG.match(m.group(1).strip())
    ):
        return ["疑似凭据键值"]
    return []


def short(h: str) -> str:
    return h[:8]


# ------------------------------------------------------------------ 1 · 工作区
def audit_worktree(pats) -> tuple[int, list[str]]:
    tracked = [p for p in git("ls-files", "-z").split("\0") if p]
    scanned = 0
    findings: list[str] = []
    for rel in tracked:
        p = ROOT / rel
        if not p.is_file():
            continue
        if not cs.is_scannable(p):
            continue
        scanned += 1
        for f in cs.scan_file(p, pats):
            findings.append("  " + f)
    print(f"[1/3] 工作区跟踪文件：{len(tracked)} 个，实扫文本 {scanned} 个")
    return len(findings), findings


# ------------------------------------------------------------------ 2 · 历史内容
def audit_history(pats, all_refs: bool) -> tuple[int, list[str]]:
    ref = "--all" if all_refs else "origin/main"
    scope = "全部分支（含未推送）" if all_refs else "已公开分支 origin/main"
    # -m：merge 提交对每个父各出一份 diff，避免 combined diff 漏内容
    # --no-renames：重命名也按「删 + 加」展开，保证新增行不被折叠
    raw = git("log", ref, "-p", "-m", "--no-color", "--no-ext-diff",
              "--no-renames", f"--format={MARK}%H %s")
    commit = ""
    path = ""
    commits: set[str] = set()
    added = 0
    hits: Counter[tuple[str, str, str]] = Counter()
    for line in raw.splitlines():
        if line.startswith(MARK):
            rest = line[len(MARK):]
            commit, _, subject = rest.partition(" ")
            commits.add(commit)
            for t in scan_line(subject, pats):          # 提交信息本身也扫
                hits[(short(commit), "<提交信息>", t)] += 1
            path = ""
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            elif path == "/dev/null":
                path = ""
            continue
        if not line.startswith("+") or line.startswith("+++") or not path:
            continue
        added += 1
        for t in scan_line(line[1:], pats):
            hits[(short(commit), path, t)] += 1
    print(f"[2/3] 提交历史（{scope}）：{len(commits)} 个提交，{added} 条新增行")
    findings = [
        f"  {c}  {p}  {t}" + (f"  ×{n}" if n > 1 else "")
        for (c, p, t), n in sorted(hits.items())
    ]
    return len(hits), findings


# ------------------------------------------------------------------ 3 · 历史文件名
def audit_names(all_refs: bool) -> tuple[int, list[str]]:
    ref = "--all" if all_refs else "origin/main"
    names = sorted({n for n in git("log", ref, "--name-only", "--format=", "--").splitlines() if n})
    risky = [n for n in names if RISKY_NAME.search(n) and not NAME_EXEMPT.search(n)]
    print(f"[3/3] 历史文件名体检：{len(names)} 个曾出现过的路径")
    return len(risky), [f"  {n}" for n in risky]


def main() -> int:
    ap = argparse.ArgumentParser(description="公开仓库脱敏终检（工作区 + 提交历史）")
    ap.add_argument("--worktree", action="store_true", help="只扫当前跟踪文件")
    ap.add_argument("--history", action="store_true", help="只扫提交历史")
    ap.add_argument("--all-refs", action="store_true", help="历史扫描含本地未推送分支")
    args = ap.parse_args()

    only_w = args.worktree and not args.history
    only_h = args.history and not args.worktree
    pats = patterns()
    custom = len(pats) - len(cs.PATTERNS)
    print(f"公开仓库脱敏终检 · 内置规则 {len(cs.PATTERNS)} 条 + 本地自定义词 {custom} 条")
    print("（只报位置与命中类型，不回显原文）\n")

    total = 0
    blocks: list[str] = []
    if not only_h:
        n, fs = audit_worktree(pats)
        total += n
        blocks += fs
    if not only_w:
        n, fs = audit_history(pats, args.all_refs)
        total += n
        blocks += fs
        n, fs = audit_names(args.all_refs)
        total += n
        blocks += fs

    print()
    if not total:
        print("✅ 未发现疑似凭据、内网地址或敏感命名文件")
        print("   提示：本结论只覆盖「模式能识别」的内容；公司名 / 真实账号 / 表名字段名")
        print("         请写进 scripts/sensitive_terms.txt 后重跑，该文件已被 gitignore。")
        return 0
    print(f"❌ 共 {total} 处命中（只列位置）：\n")
    for f in blocks:
        print(f)
    print("\n处理方式：命中若为真凭据，仅删当前文件不够 —— 按 GitHub 官方指南清理历史并轮换凭据；")
    print("          若为误报，改写法或补占位符豁免，别关掉门禁。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
