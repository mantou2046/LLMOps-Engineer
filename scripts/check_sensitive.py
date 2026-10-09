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
    # 连接串单独处理（见 scan_file：要判 user/pass 是否占位符），此处不列。
]

# 键值型：**值必须非空**才报（.env.example 里是空占位，不该误伤）
KV_NAME = (
    r"(?i)\b([A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY"
    r"|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*)"
)
KV = re.compile(KV_NAME + r"\s*[:=]\s*[\"']?([^\s\"'#,;]{8,})")

# ---------------------------------------------------------------- 占位符豁免
# 目的：区分「真实凭据」与「对凭据的引用 / 模板」，后者不该拦。
# 背景（2026-10-08）：加 Dockerfile / compose 时，合法的占位写法被大量误报 ——
#   compose:  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}
#   python :  "postgresql://{u}:{p}@{h}:{port}/{db}"
#   doc    :  postgresql://user:password@host:5432/db
# 若不放行，每次提交都得 --no-verify，门禁形同虚设。
# **放宽的只是「形式上不可能是真凭据」的写法**，真凭据（一串具体随机字符）一条都没放过。
PLACEHOLDER_RX = [
    re.compile(r"^\$\{"),                            # shell / compose 引用：${VAR}
    re.compile(r"^\$\("),                            # 命令替换：$(...)
    re.compile(r"^\{[^}]*\}$"),                      # python / k8s 模板：{u} / {password}
    re.compile(r"^<[^>]*>$"),                        # 文档占位：<your-password>
    re.compile(r"^\.\.\.$|^\*+$|^-+$|^x+$", re.IGNORECASE),   # 省略号 / 掩码
    re.compile(r"^(?i:your|my|some|the|change|replace|insert|enter|todo|example|dummy|fake|test)[-_]"),
    re.compile(r"(?i)^(password|passwd|secret|token|changeme|placeholder|redacted|none|null)$"),
    # 文档示例里「变量名当值」：user / password / localhost ...
    re.compile(r"^(?i:user|username|pass|pwd|host|localhost|dbname|database|port|dsn)$"),
    # 形如 user:password@（占位用户名 + 占位口令）
    re.compile(r"(?i)^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*@?$"),
]

# 「键名语义上就不是凭据值」的键 —— 这类键的值是**引用 / 标识符**，不是秘密。
# 背景（2026-10-09）：加 ingress-nginx 上游清单时，
#   `secretName: ingress-nginx-admission`（K8s 字段名 + 资源名）
#   被当成「疑似凭据键值」误报 —— 它 23 字符，正好撞上长度启发式。
# 这些键的**值永远是名字**，泄露它不构成泄露凭据（真正的凭据在 Secret 的 data 里）。
# ⚠️ 变量名刻意不含敏感词、值另起一行：否则本文件会被自己的 KV 启发式扫中（自伤）。
_KEY_IS_REF = re.compile(
    r"(?i)^(?:secret[-_]?name|secretname|secretkeyref|configmap[-_]?name|"
    r"service[-_]?account(?:[-_]?name)?|imagePullSecrets?)$"
)


def is_placeholder(value: str) -> bool:
    """判断一个「值」是否只是占位符 / 引用，而非真实凭据。

    ⚠️ 判定从严：只放过「形式上不可能是真凭据」的写法。
    真实凭据是随机字符串，不会长成 your-password / ${VAR} / <xxx> 这样。
    """
    v = value.strip().strip("\"'")
    if not v:
        return True
    if "${" in v or "{{" in v or "<" in v:      # 未展开的模板标记
        return True
    return any(rx.search(v) for rx in PLACEHOLDER_RX)


# 带口令的连接串：命中后再判 user/pass 是否占位符，是则放行
CONN_RX = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|oracle)"
    r"://([^\s:@/]+):([^\s@/]+)@"
)

# ---------------------------------------------------------------- 跳过规则
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "mlruns", "mlartifacts",
    "models", "dist", "build", ".idea", ".vscode",
    # 本地备份目录：含**真实**凭据与库内容（正是为了不丢才备份的），
    # 已 gitignore 永不提交 → 扫它只会制造上百条噪音，掩盖真正的问题。
    # 背景（2026-10-09）：导出 K8s 资源清单后，全树扫描从 2 条暴增到 98 条。
    ".local-backup",
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
        # 1) 通用模式
        for name, rx in patterns:
            m = rx.search(line)
            if m:
                findings.append(f"{rel}:{lineno}  命中「{name}」  {mask(m.group(0))}")
                break
        else:
            # 2) 连接串：user / pass 都是占位符则放行
            mc = CONN_RX.search(line)
            if mc and not (is_placeholder(mc.group(1)) and is_placeholder(mc.group(2))):
                findings.append(
                    f"{rel}:{lineno}  命中「带口令的连接串」  {mask(mc.group(0))}"
                )
                continue
            # 3) 键值型：值为占位符则放行；键名本身「不是凭据」也放行
            m = KV.search(line)
            if m and not is_placeholder(m.group(2)) and not _KEY_IS_REF.match(m.group(1).strip()):
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
