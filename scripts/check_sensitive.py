#!/usr/bin/env python3
"""提交前脱敏检查：扫描暂存区（或全树）里的凭据与敏感词。

用法
----
    python scripts/check_sensitive.py            # 扫描暂存文件（pre-commit 钩子用）
    python scripts/check_sensitive.py --staged   # 同上，显式指定
    python scripts/check_sensitive.py --all      # 扫描全树

退出码
------
    0 = 通过
    1 = 命中（阻止提交 / 让 CI 失败）

⚠️ 本脚本**只输出「文件:行号 + 命中类型」**，绝不回显命中的原文 ——
   否则检查工具本身就成了泄露渠道。

自定义词表
----------
把本单位/本项目的敏感词写进 `scripts/sensitive_terms.txt`（一行一个，`#` 开头为注释）。
该文件已被 .gitignore 忽略，**只提交模板** `sensitive_terms.example.txt`。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 内置模式
# (显示名, 正则)
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("私钥内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "内网 IP",
        re.compile(
            r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"
        ),
    ),
    (
        "带口令的连接串",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|oracle)"
            r"://[^\s:@/]+:[^\s@/]+@"
        ),
    ),
]

# 键值型：**值必须非空**才报（.env.example 里是空占位，不该误伤）
KV_NAME = (
    r"(?i)\b([A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY"
    r"|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*)"
)
KV = re.compile(KV_NAME + r"\s*[:=]\s*[\"']?([^\s\"'#,;]{8,})")

# ---------------------------------------------------------------- 跳过规则
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "mlruns", "mlartifacts",
    "models", "dist", "build", ".idea", ".vscode",
}

# 模板文件：内容本身就是「示例敏感词」，扫它必然自伤
SKIP_FILES = {
    "scripts/sensitive_terms.example.txt",
}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl",
    ".xlsx", ".xls", ".docx", ".pptx", ".pt", ".pth", ".bin", ".safetensors",
    ".gguf", ".onnx", ".so", ".dll", ".exe", ".pyc", ".parquet", ".sqlite",
    ".db", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
}

MAX_BYTES = 2 * 1024 * 1024  # 单文件超过 2 MB 不扫


def load_custom_terms() -> list[str]:
    """读取用户自定义词表（不存在则返回空）。"""
    f = ROOT / "scripts" / "sensitive_terms.txt"
    if not f.exists():
        return []
    terms: list[str] = []
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


def build_custom_patterns(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for t in terms:
        out.append((f"自定义词表：{t}", re.compile(re.escape(t))))
    return out


def staged_files() -> list[Path]:
    """取暂存区新增/修改的文件。"""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("check_sensitive: 无法读取暂存区，回退为全树扫描", file=sys.stderr)
        return all_files()
    return [ROOT / p for p in r.stdout.splitlines() if p.strip()]


def all_files() -> list[Path]:
    """遍历全树，跳过 SKIP_DIRS。"""
    files: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        files.append(p)
    return files


def is_scannable(p: Path) -> bool:
    rel = p.relative_to(ROOT).as_posix()
    if rel in SKIP_FILES:
        return False
    if p.suffix.lower() in BINARY_EXT:
        return False
    try:
        if p.stat().st_size > MAX_BYTES:
            return False
    except OSError:
        return False
    return True


def mask(value: str) -> str:
    """只给长度提示，不回显内容。"""
    return f"<已隐藏，{len(value)} 字符>"


def scan_file(p: Path, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """返回该文件的命中描述列表（不含原文）。"""
    findings: list[str] = []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [f"读取失败：{e}"]

    rel = p.relative_to(ROOT).as_posix()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, rx in patterns:
            m = rx.search(line)
            if m:
                findings.append(f"{rel}:{lineno}  命中「{name}」  {mask(m.group(0))}")
                break  # 一行只报一次，避免噪音
        else:
            m = KV.search(line)
            if m:
                findings.append(
                    f"{rel}:{lineno}  命中「疑似凭据键值」  键={m.group(1)}  值={mask(m.group(2))}"
                )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="提交前脱敏检查")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="只扫暂存文件（默认）")
    g.add_argument("--all", action="store_true", help="扫描全树")
    args = ap.parse_args()

    targets = all_files() if args.all else staged_files()
    patterns = PATTERNS + build_custom_patterns(load_custom_terms())
    custom_count = len(patterns) - len(PATTERNS)

    findings: list[str] = []
    scanned = 0
    for p in targets:
        if not p.is_file() or not is_scannable(p):
            continue
        scanned += 1
        findings.extend(scan_file(p, patterns))

    mode = "全树" if args.all else "暂存区"
    print(f"check_sensitive: 扫描 {scanned} 个文件（{mode}），"
          f"内置规则 {len(PATTERNS)} 条 + 自定义词 {custom_count} 条")

    if not findings:
        print("✅ 未发现疑似凭据或敏感词")
        return 0

    print(f"\n❌ 发现 {len(findings)} 处命中，已阻止：\n")
    for f in findings:
        print("  " + f)
    print(
        "\n处理方式：\n"
        "  · 误报 → 把该行改成不含敏感值的写法，或加进 scripts/sensitive_terms.example.txt 的同名跳过逻辑\n"
        "  · 真命中 → 从暂存区撤下（git restore --staged <文件>），改用 .env（已被 gitignore）\n"
        "  · 已提交过 → 仅删文件不够，历史里还在，需改写历史或作废该凭据\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
