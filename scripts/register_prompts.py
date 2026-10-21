#!/usr/bin/env python
"""读 prompts/<name>/v<版本>.yaml → 注册进 Langfuse → 给指定版本打 label。

用法：
  python scripts/register_prompts.py --name structured_json --version 1 --label production
  python scripts/register_prompts.py --name structured_json --version 2 --label canary
  python scripts/register_prompts.py --name structured_json --version 2 --label production --move
  python scripts/register_prompts.py --name structured_json --version 1 --dry-run

设计取舍（DEC-027）：
  · 文件是真相源：脚本只读 yaml，不接受「手打的 prompt」
  · 幂等：同内容版本已存在 → 只移 label，不重复建版本（版本号不因重跑而膨胀）
  · label 不会自动搬走：Langfuse 的 label 挂在版本上，换指向必须显式清旧的（见 3.3）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

# 代理预检（D15 坑九）
_np = os.environ.get("no_proxy", "")
if "127.0.0.1" not in _np:
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(
        x for x in (_np, "127.0.0.1,localhost") if x
    )


def h8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def sync_labels_file(name: str, label: str | None, version: int) -> None:
    """把「指针指向」镜像进 prompts/<name>/labels.yaml（registry 仍是真指针）。

    ⚠️ 2026-10-21 实测：没有这个镜像，registry 挂掉时回落只能「猜最新版」，
       label=production 会静默拿到 v2（实际指向 v1）。镜像还让 CI 能对账。
    """
    f = REPO / "prompts" / name / "labels.yaml"
    doc: dict[str, int] = {}
    if f.exists():
        doc = {str(k): int(v) for k, v in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items()}
    if label:
        doc[label] = version   # label 跨版本唯一：直接覆盖指向，不需要「先清旧的」
    f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=True), encoding="utf-8")
    print(f"[ok] labels.yaml 同步：{doc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="prompt 名（= prompts/ 下的目录名）")
    ap.add_argument("--version", type=int, required=True, help="读哪个 vN.yaml")
    ap.add_argument("--label", default=None, help="要打/移动的 label（production / canary …）")
    ap.add_argument("--move", action="store_true", help="只移动 label，不新建版本")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    f = REPO / "prompts" / args.name / f"v{args.version}.yaml"
    if not f.exists():
        print(f"[err] 文件不存在：{f}")
        return 2
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    tpl = doc["template"]
    h = h8(tpl)
    print(f"[info] {f.relative_to(REPO)}  hash={h}  variables={doc.get('variables')}")

    from langfuse import Langfuse

    lf = Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        host=os.getenv("LANGFUSE_HOST"),
    )
    if not lf.auth_check():
        print("[err] Langfuse 认证失败：查 .env 三件套 + HOST 可达性（记得 no_proxy）")
        return 2

    labels = [args.label] if args.label else []

    # 该版本已存在吗？内容一致就只移 label（幂等）
    same = False
    try:
        cur = lf.get_prompt(args.name, version=args.version, cache_ttl_seconds=0)
        cur_text = cur.prompt if isinstance(cur.prompt, str) else str(cur.prompt)
        same = h8(cur_text) == h
        if not same:
            print(f"[warn] {args.name}@v{args.version} 已存在但内容不同"
                  f"（registry={h8(cur_text)} vs 文件={h}）—— 不覆盖，请升版本号")
            return 3
    except Exception:
        same = False

    if args.move or same:
        if args.dry_run:
            print(f"[dry-run] 只移动 label：{args.name}@v{args.version} labels={labels}")
            return 0
        lf.update_prompt(name=args.name, version=args.version, new_labels=labels)
        sync_labels_file(args.name, args.label, args.version)
        print(f"[ok] label {labels or '（清空）'} → {args.name}@v{args.version}（未新建版本）")
        return 0

    if args.dry_run:
        print(f"[dry-run] 将新建 {args.name}@v{args.version}，hash={h}，labels={labels}")
        return 0

    p = lf.create_prompt(
        name=args.name,
        prompt=tpl,
        labels=labels,
        config={"hash": h, "variables": doc.get("variables", []), "file": str(f.relative_to(REPO))},
        commit_message=(doc.get("changelog") or "").strip().splitlines()[0][:200],
    )
    sync_labels_file(args.name, args.label, args.version)
    print(f"[ok] {args.name}@{p.version} 新建，hash={h}，labels={labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())