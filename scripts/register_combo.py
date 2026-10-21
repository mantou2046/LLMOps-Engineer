#!/usr/bin/env python
"""W3 D15 · 把「可部署单元」注册进 MLflow Model Registry（作品集侧）

设计取舍（三条，都是踩过坑才定的）：
  1) **prompt 模板不另存一份，从产出它的那个 run 的 artifact 里取。**
     这样「模型版本」与「prompt 版本」永远绑在一起（DEC-026 的幽灵状态：
     模型回滚了、prompt 没回滚）。复制一份到本地 = 制造第二个真相源。
  2) 用 MlflowClient.search_runs 而不是 mlflow.search_runs：后者返回 pandas
     DataFrame，而**本 venv 没装 pandas**（`import pandas` 直接 ImportError）。
  3) 注册用 create_model_version + run 的 artifact_uri。
     ⚠️ 别用 mlflow.register_model("runs:/<id>/combo")：实测它只认 log_model
     产生的「logged_model」，对 log_artifacts 打进去的目录会抛
     "Unable to find a logged_model with artifact_path combo"。

⚠️ 坑九（2026-10-21 实测，沙箱/本机代理环境）：本机 HTTP_PROXY 会指向
   127.0.0.1:xxxx 的本地代理，且往往没有 no_proxy → 发往 127.0.0.1:5000 的
   MLflow 请求被代理截胡，表现为**时好时坏的 RestException INTERNAL_ERROR:
   'Not Found'**（search_runs / get_run / list_artifacts 随机中招）。
   修法：在任何 import mlflow / 发请求之前，把 no_proxy 补上 127.0.0.1,localhost。

用法：
    python scripts/register_combo.py --prompt-version v0 --alias production
    python scripts/register_combo.py --prompt-version v1 --alias canary
    python scripts/register_combo.py --show --alias production
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# ── 坑九预检：本地 tracking server 必须绕过系统代理 ────────────────────────────
_no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
for _host in ("127.0.0.1", "localhost"):
    if _host not in _no_proxy:
        _no_proxy = f"{_no_proxy},{_host}" if _no_proxy else _host
os.environ["no_proxy"] = _no_proxy
os.environ["NO_PROXY"] = _no_proxy

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")
os.environ.setdefault("OBSERVABILITY", "off")

import mlflow  # noqa: E402
from mlflow.entities import ViewType  # noqa: E402

REGISTRY_NAME = "qa-combo"
SOURCE_EXPERIMENT = "qa-prompt-eval"


def make_client() -> "mlflow.MlflowClient":
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    return mlflow.MlflowClient()


def find_latest_run(cl: "mlflow.MlflowClient", prompt_version: str) -> Any:
    """按 tag 找最近一次 FINISHED 的评估 run（取舍 1：血缘从这里来）。"""
    exp = cl.get_experiment_by_name(SOURCE_EXPERIMENT)
    if exp is None:
        raise SystemExit(
            f"实验 {SOURCE_EXPERIMENT} 不存在 —— 先跑 scripts/track_prompt_run.py --prompt-version {prompt_version}"
        )
    runs = cl.search_runs(
        experiment_ids=[exp.experiment_id],
        filter_string=f"tags.prompt_version = '{prompt_version}'",
        run_view_type=ViewType.ACTIVE_ONLY,
        order_by=["attributes.start_time DESC"],
        max_results=1,
    )
    if not runs:
        raise SystemExit(
            f"实验 {SOURCE_EXPERIMENT} 里没有 tag prompt_version={prompt_version} 的 run —— "
            f"先跑 scripts/track_prompt_run.py --prompt-version {prompt_version}"
        )
    return runs[0]


def build_combo(cl: "mlflow.MlflowClient", run: Any, out: Path) -> dict:
    """四元组 + prompt 原文打成 artifact。combo.json 是权威，提示词随版本一起走。"""
    p = dict(run.data.params)
    combo = {
        "prompt_version": p.get("prompt_version"),
        "prompt_hash": p.get("prompt_hash"),
        "model": p.get("model"),
        "temperature": float(p.get("temperature", 0.0)),
        "max_tokens": int(p.get("max_tokens", 256)),
        "llm_mode": p.get("llm_mode"),
        "source_run_id": run.info.run_id,
        "git_sha": p.get("git_sha"),
        "eval_set_version": run.data.tags.get("eval_set_version"),
    }
    (out / "combo.json").write_text(
        json.dumps(combo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # prompt 原文从源 run 下载；hash 对不上就整体失败（不静默降级成空模板）
    local = cl.download_artifacts(run.info.run_id, "eval/system_prompt.txt", str(out))
    src = Path(local).read_text(encoding="utf-8")
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:8]
    if digest != combo["prompt_hash"]:
        raise SystemExit(
            f"prompt 原文 hash {digest} 与 run 记录的 {combo['prompt_hash']} 不一致 —— 源 run 被动过，拒绝注册"
        )
    return combo


def show(cl: "mlflow.MlflowClient", alias: str) -> None:
    mv = cl.get_model_version_by_alias(REGISTRY_NAME, alias)
    print(f"alias '{alias}' -> {REGISTRY_NAME} version {mv.version}")
    print(f"  version_run_id : {mv.run_id}   <- 打包 combo 的那次 run")
    print(f"  source_eval_run: {mv.tags.get('source_run_id')}   <- 产出指标的评估 run（血缘）")
    print(f"  tags           : {json.dumps(mv.tags, ensure_ascii=False)}")
    data = mlflow.artifacts.load_dict(f"models:/{REGISTRY_NAME}@{alias}/combo.json")
    print("  combo.json     :")
    for k, v in data.items():
        print(f"    {k:<18} {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-version", choices=["v0", "v1"], help="要注册/切换的 prompt 版本")
    ap.add_argument("--alias", default="production", help="production / canary / 任意名")
    ap.add_argument("--show", action="store_true", help="只查看 alias 当前指向，不注册")
    args = ap.parse_args()

    cl = make_client()

    if args.show:
        show(cl, args.alias)
        return 0
    if not args.prompt_version:
        ap.error("要么给 --prompt-version，要么给 --show")

    src_run = find_latest_run(cl, args.prompt_version)
    print(f"[info] 源评估 run: {src_run.info.run_id}")

    with tempfile.TemporaryDirectory() as tmp:
        combo = build_combo(cl, src_run, Path(tmp))
        with mlflow.start_run(run_name=f"combo-{combo['prompt_version']}-{combo['prompt_hash']}") as run:
            mlflow.log_params({k: v for k, v in combo.items() if isinstance(v, (str, int, float))})
            mlflow.set_tags({
                "prompt_version": combo["prompt_version"],
                "prompt_hash": combo["prompt_hash"],
                "source_run_id": combo["source_run_id"],
            })
            mlflow.log_artifacts(tmp, artifact_path="combo")
            combo_run_id = run.info.run_id
            combo_artifact_uri = run.info.artifact_uri
            print(f"[info] 组合 run: {combo_run_id}")

    try:
        cl.create_registered_model(REGISTRY_NAME, tags={"owner": "llmops-portfolio"})
        print(f"[info] 已创建 registered model: {REGISTRY_NAME}")
    except Exception as exc:  # noqa: BLE001 - 已存在是正常路径
        if "already exists" not in str(exc).lower():
            raise
        print(f"[info] registered model 已存在: {REGISTRY_NAME}")

    mv = cl.create_model_version(
        name=REGISTRY_NAME,
        source=f"{combo_artifact_uri}/combo",
        run_id=combo_run_id,
        tags={
            "prompt_version": combo["prompt_version"],
            "prompt_hash": combo["prompt_hash"],
            "source_run_id": combo["source_run_id"],
            "git_sha": combo["git_sha"],
            "eval_set_version": combo["eval_set_version"],
        },
    )
    print(f"[info] {REGISTRY_NAME} 新建 version {mv.version} (status={mv.status})")

    cl.set_registered_model_alias(REGISTRY_NAME, args.alias, mv.version)
    print(f"[ok] alias '{args.alias}' -> {REGISTRY_NAME} version {mv.version}")

    show(cl, args.alias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
