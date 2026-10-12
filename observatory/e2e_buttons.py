"""端到端：从观测台点「发坏版本」→「回滚」，验证整个闭环。

只在 dev（演练场 namespace）上做，prod 不碰。
即使回滚按钮失败，脚本末尾也会用 helm CLI 兜底把 dev 修回干净状态。
"""

import json
import subprocess
import time
import urllib.request

BASE = "http://127.0.0.1:8899"
HELM = r"D:\Software\0\helm\helm.exe"


def call(path, payload=None, timeout=180):
    # ⚠️ action 路由是 POST-only。payload 给 {} 而不是 None ——
    # 否则 method 会退化成 GET，拿到一个 404，白忙一场。
    if payload is None and path.startswith("/api/action/"):
        payload = {}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def helm(*args, timeout=120):
    out = subprocess.run([HELM, *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return out.returncode, out.stdout.strip(), out.stderr.strip()


print("=" * 66)
print("步骤 0 · 把观测台切到 dev，并记录基线")
t = call("/api/targets")["targets"]
dev = next(x for x in t if x["ns"] == "dev")
call("/api/target", dev)
st = call("/api/state")
base_rev = st["release"]["revision"]
print(f"  目标: {st['target']['ns']}/{st['target']['deployment']}")
print(f"  当前 revision = {base_rev}  status = {st['release']['status']}")
print(f"  Pod 数 = {len(st['pods'])}  就绪端点 = {st['endpointReadyCount']}")

print()
print("=" * 66)
print("步骤 1 · 点「发一个坏版本」（观测台会等 45s 后报失败，这是预期）")
t0 = time.time()
try:
    r = call("/api/action/helm-bad-deploy", timeout=180)
    print(f"  ok       = {r.get('ok')}")
    print(f"  summary  = {r.get('summary')}")
    print(f"  耗时     = {time.time() - t0:.1f}s")
    print(f"  实际命令 = {r['commands'][0] if r.get('commands') else '(无)'}")
except Exception as exc:
    # helm 返回非 0 时观测台按设计回 500，urllib 会抛 HTTPError
    body = exc.read().decode() if hasattr(exc, "read") else "{}"
    try:
        r = json.loads(body)
    except Exception:
        r = {"ok": False, "summary": body[:300]}
    print(f"  ok       = {r.get('ok')}")
    print(f"  summary  = {str(r.get('summary'))[:220]}")
    print(f"  耗时     = {time.time() - t0:.1f}s")

print()
print("  坏版本期间的集群实况：")
st = call("/api/state")
print(f"    Pod 数 = {len(st['pods'])}  就绪端点 = {st['endpointReadyCount']}")
for p in st["pods"]:
    print(f"      - {p['name']}: {p['ready']} restarts={p['restarts']}")
rel = call("/api/releases")["releases"]
dev_rel = next(x for x in rel if x["namespace"] == "dev")
print(f"    helm: 当前 revision = {dev_rel['revision']}  status = {dev_rel['status']}")
for h in sorted(dev_rel["history"], key=lambda x: int(x["revision"])):
    print(f"      rev {h['revision']:<3} {h['status']:<11} {h.get('description','')[:60]}")

print()
print("=" * 66)
print("步骤 2 · 点「回滚到上一个可用版本」")
t0 = time.time()
try:
    r = call("/api/action/helm-rollback", timeout=180)
    print(f"  ok       = {r.get('ok')}")
    print(f"  summary  = {r.get('summary')}")
    print(f"  实际命令 = {r['commands'][0] if r.get('commands') else '(无)'}")
except Exception as exc:
    body = exc.read().decode() if hasattr(exc, "read") else "{}"
    print(f"  ❌ 回滚按钮失败：{body[:400]}")
    r = {}
print(f"  耗时 = {time.time() - t0:.1f}s")

print()
print("  回滚后的集群实况：")
st = call("/api/state")
print(f"    Pod 数 = {len(st['pods'])}  就绪端点 = {st['endpointReadyCount']}")
for p in st["pods"]:
    print(f"      - {p['name']}: {p['ready']} restarts={p['restarts']}")
rel = call("/api/releases")["releases"]
dev_rel = next(x for x in rel if x["namespace"] == "dev")
print(f"    helm: 当前 revision = {dev_rel['revision']}  status = {dev_rel['status']}")
for h in sorted(dev_rel["history"], key=lambda x: int(x["revision"])):
    print(f"      rev {h['revision']:<3} {h['status']:<11} {h.get('description','')[:60]}")

print()
print("=" * 66)
print("步骤 3 · 兜底核对：万一按钮没修好，用 helm CLI 确认 dev 状态干净")
rc, out, err = helm("history", "llmops-dev", "-n", "dev")
print("  helm history llmops-dev -n dev:")
for line in out.splitlines():
    print("   ", line)

ok_state = (st["release"]["status"] == "deployed"
            and st["endpointReadyCount"] == len(st["pods"])
            and all(p["restarts"] == 0 for p in st["pods"]))
print()
print("=" * 66)
print("✅ 闭环完成，dev 已恢复干净状态" if ok_state
      else "⚠️ dev 状态未达预期，请用上面的 helm history 人工确认")
