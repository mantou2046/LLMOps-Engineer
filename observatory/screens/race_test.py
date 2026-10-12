# 并发切换压力测试：同时打 prod/dev 两个 /api/target，验证 ns/deployment 永不错配
import json
import threading
import urllib.request

BASE = "http://127.0.0.1:8899"

TARGETS = {
    "default": {"ns": "default", "deployment": "llmops-api", "container": "api",
                "source": "yaml", "release": "", "chart": ""},
    "dev": {"ns": "dev", "deployment": "llmops-dev-llmops-chart", "container": "llmops-chart",
            "source": "chart", "release": "llmops-dev", "chart": "llmops-chart-0.1.0"},
    "prod": {"ns": "prod", "deployment": "llmops-prod-llmops-chart", "container": "llmops-chart",
             "source": "chart", "release": "llmops-prod", "chart": "llmops-chart-0.1.0"},
}
# 合法组合：ns -> (deployment, container, source)
VALID = {
    "default": ("llmops-api", "api", "yaml"),
    "dev": ("llmops-dev-llmops-chart", "llmops-chart", "chart"),
    "prod": ("llmops-prod-llmops-chart", "llmops-chart", "chart"),
}


def post_target(name):
    body = json.dumps(TARGETS[name]).encode()
    req = urllib.request.Request(BASE + "/api/target", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get_state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=15) as r:
        return json.loads(r.read())


ROUNDS = 25
bad = []
for i in range(ROUNDS):
    # 两个请求真并发：各起一个线程，尽量让它们在服务端交错
    results = {}
    def hit(name):
        try:
            results[name] = post_target(name)
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": str(e)}

    t1 = threading.Thread(target=hit, args=("prod",))
    t2 = threading.Thread(target=hit, args=("dev",))
    t1.start(); t2.start(); t1.join(); t2.join()

    st = get_state().get("target") or {}
    ns = st.get("ns")
    dep, cont, src = st.get("deployment"), st.get("container"), st.get("source")
    ok = ns in VALID and (dep, cont, src) == VALID[ns]
    if not ok:
        bad.append((i, ns, dep, cont, src, results))

print(f"并发切换 {ROUNDS} 轮：{'全部一致 ✅' if not bad else str(len(bad)) + ' 轮错配 ❌'}")
for b in bad[:5]:
    print("  错配样本:", b)
if not bad:
    st = get_state().get("target") or {}
    print("末态:", st.get("ns"), st.get("deployment"), st.get("container"), st.get("source"))
