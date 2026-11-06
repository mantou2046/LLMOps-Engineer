#!/usr/bin/env python3
"""生成不含任何已标答案的盲标数据，供两位独立标注员各自使用。

正式网页预载了 47 题答案；若让 muse / codex 直接看，测出来的是「抄得像不像」，
不是「规范写得清不清楚」。本脚本把 preselect / preselect_refuse / carried / pending
全部清空，产出一份纯题目数据和一份可点选的盲标网页。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "evals" / "golden_set.quiz.html"
OUT_JSON = ROOT / "evals" / "out" / "w6d3" / "blind_items.json"
OUT_HTML = ROOT / "evals" / "golden_set.quiz.blind.html"


def main() -> int:
    html = SRC.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    if not m:
        print("正式网页里找不到内嵌 DATA", file=__import__("sys").stderr)
        return 1
    data = json.loads(m.group(1))
    for it in data["items"]:
        it["preselect"] = []
        it["preselect_refuse"] = None
    data["carried"] = []
    data["pending"] = []
    data["blind"] = True

    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    blind_html = re.sub(
        r"const DATA = \{.*?\};\n",
        lambda _: "const DATA = " + embedded + ";\n",
        html,
        count=1,
        flags=re.S,
    )
    OUT_HTML.write_text(blind_html, encoding="utf-8")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(data["items"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    assert not any(it["preselect"] or it["preselect_refuse"] is not None for it in data["items"])
    print(f"已生成 {OUT_JSON.name}（{len(data['items'])} 题，无任何答案）")
    print(f"已生成 {OUT_HTML.name}（打开显示 0 / {len(data['items'])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
