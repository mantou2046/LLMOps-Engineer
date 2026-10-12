# 强化竞态测试：并发切换 + 并发轮询 /api/state，校验每份快照内部一致
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8899"

TARGETS = {
    "dev": {"ns": "dev", "deployment": "llmops-dev-llmops-chart", "container": "llmops-chart",
            "source": "chart", "release": "llmops-dev", "chart": "llmops-chart-0.1.0"},
    "prod": {"ns": "prod", "deployment": "llmops-prod-llmops-chart", "container": "llmops-chart",
             "source": "chart", "release": "llmops-prod", "chart": "llmops-chart-0.1.0"},
}
VALID = {
    "default": ("llmops-api", "api", "yaml"),
    "dev": ("llmops-dev-llmops-chart", "llmops-chart", "chart"),
    "prod": ("llmops-prod-llmops-chart", "llmops-chart", "chart"),
}
PREFIX = {"default": "llmops-api-", "dev": "llmops-dev-", "prod": "llmops-prod-"}

bad = []
poll_stop = threading.Event()
poll_count = [0]


def post_target(name):
    body = json.dumps(TARGETS[name]).encode()
    req = urllib.request.Request(BASE + "/api/target", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        json.loads(r.read())


def poller():
    """模拟前端轮询：持续拉 /api/state，校验每份快照内部一致。"""
    while not poll_stop.is_set():
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=15) as r:
                st = json.loads(r.read())
        except Exception:
            continue
        poll_count[0] += 1
        t = st.get("target") or {}
        ns = t.get("ns")
        got = (t.get("deployment"), t.get("container"), t.get("source"))
        problems = []
        if ns not in VALID:
            problems.append(f"未知 ns={ns}")
        elif got != VALID[ns]:
            problems.append(f"错配 {ns} -> {got}")
        # Pod 名前缀必须和 ns 对得上 —— 这是「混合快照」的照妖镜
        for p in st.get("pods") or []:
            if not (p.get("name") or "").startswith(PREFIX.get(ns, "\x00")):
                problems.append(f"{ns} 里混进了 {p.get('name')}")
        # release 快照也要和 ns 一致
        rel = st.get("release") or {}
        if rel and rel.get("name") not in (None, "", "llmops-dev" if ns == "dev" else "llmops-prod"):
            problems.append(f"{ns} 的 release 是 {rel.get('name')}")
        if problems:
            bad.append((time.strftime("%H:%M:%S"), problems[:3], t))


pt = threading.Thread(target=poller, daemon=True)
pt.start()

for i in range(40):
    ts = [threading.Thread(target=post_target, args=(n,))
          for n in ("prod", "dev", "prod", "dev")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    time.sleep(0.3)

poll_stop.set()
pt.join(timeout=20)

print(f"并发轮询 {poll_count[0]} 份快照 + 40 轮×4 并发切换（约 20 秒窗口）")
if bad:
    print(f"❌ {len(bad)} 份快照不一致：")
    for b in bad[:6]:
        print("   ", b)
else:
    print("✅ 每份快照的 ns/deployment/source/Pod/release 全部自相一致")
