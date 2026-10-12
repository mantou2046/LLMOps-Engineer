"""观测台目标切换 / Helm 按钮逻辑的非破坏性自检。

只读不写：不触发任何 action，只验证
  1. discover_targets() 能不能正确识别三个目标
  2. set_target() 切换后，collect_state() 的 Pod / Service / release 对不对
  3. _current_revision() / _last_deployed_revision() 的取值是否正确 ——
     这是「回滚」按钮安全性的核心：**绝不能回滚到 failed 的 revision**
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "obsrv", Path(__file__).resolve().parent / "server.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        fails.append(label)


print("=== 1. discover_targets() ===")
targets = m.discover_targets()
print(f"  扫到 {len(targets)} 个目标")
for t in targets:
    print(f"    · {t['ns']}/{t['deployment']}  source={t['source']} "
          f"release={t['release'] or '-'} rev={t['revision']} pods={t['podsReady']}")
check("目标数量", len(targets), 3)
check("第一个是 default 裸清单",
      (targets[0]["ns"], targets[0]["source"]), ("default", "yaml"))
by_ns = {t["ns"]: t for t in targets}
check("dev 是 chart 来源", by_ns["dev"]["source"], "chart")
check("dev release 名", by_ns["dev"]["release"], "llmops-dev")
# dev 的 revision 不写死 —— 演练会让它一直涨。这里只验「两个来源口径一致」。
m.set_target(by_ns["dev"])
check("dev 当前 revision 与 helm history 口径一致",
      by_ns["dev"]["revision"], m._current_revision())
check("prod release 名", by_ns["prod"]["release"], "llmops-prod")

print("\n=== 2. 逐个目标 collect_state() ===")
for t in targets:
    m.set_target(t)
    st = m.collect_state()
    dep = st["deployment"]
    rel = st["release"]
    print(f"  · {t['ns']}/{t['deployment']}")
    print(f"      Service={st['target']['serviceName'] or '(未找到)'}  "
          f"Pod={len(st['pods'])}  端点={st['endpointReadyCount']}")
    print(f"      replicas={dep['replicas']} ready={dep['readyReplicas']} "
          f"stuck={dep['rolloutStuck']}")
    if t["source"] == "chart":
        print(f"      release={rel.get('name')} rev={rel.get('revision')} "
              f"{rel.get('status')}")
    if not st["target"]["serviceName"]:
        fails.append(f"{t['ns']} 未找到 Service")
    if not dep["selector"]:
        fails.append(f"{t['ns']} Deployment 选择器为空")

# dev 的基本面：1 副本、1 端点、release 是 deployed
m.set_target(by_ns["dev"])
st = m.collect_state()
check("dev Pod 数", len(st["pods"]), 1)
check("dev 就绪端点", st["endpointReadyCount"], 1)
check("dev release status", st["release"]["status"], "deployed")
check("dev 全部 Pod restarts=0（回滚是干净的）",
      all(p["restarts"] == 0 for p in st["pods"]), True)

m.set_target(by_ns["prod"])
st = m.collect_state()
check("prod Pod 数", len(st["pods"]), 2)
check("prod 就绪端点", st["endpointReadyCount"], 2)
check("prod release rev", st["release"]["revision"], 1)

m.set_target(by_ns["default"])
st = m.collect_state()
check("default Pod 数（W1 演练场未受影响）", len(st["pods"]), 2)
check("default 无 release", st["release"], {})

print("\n=== 3. 回滚目标选择逻辑（按钮安全性的核心） ===")
# ⚠️ 这里刻意**不写死 revision 号**。历史会随着反复演练不断变长
#    （每点一次按钮就多一条），写死等于每次跑都要改断言。
#    真正该保证的是几条**不变量**：
#      a. 当前 revision 与 helm list 报告的一致
#      b. 回滚目标必须存在，且其 status 不是 failed
#      c. 回滚目标 != 当前 revision
m.set_target(by_ns["dev"])
cur = m._current_revision()
best = m._last_deployed_revision(cur)
hist = {int(h["revision"]): h["status"] for h in m._release_history()}
print(f"  llmops-dev 当前 revision = {cur}")
print(f"  最近一个可用 revision   = {best}")
print(f"  历史: {hist}")
check("a. 当前 revision 与 helm list 一致",
      cur, m.discover_targets()[1]["revision"] if len(m.discover_targets()) > 1 else cur)
check("b. 回滚目标存在", best > 0, True)
check("b. 回滚目标的 status 不是 failed",
      hist.get(best, "failed") != "failed", True)
check("c. 回滚目标 != 当前 revision", best != cur, True)
check("c. 回滚目标 <= 最大 revision", best <= max(hist), True)

print("\n=== 4. 按钮作用域 ===")
visible = {a["name"] for a in m.ACTIONS.values()
           if a.get("scope", "all") in ("all", "chart")}
print(f"  chart 目标可见: {sorted(visible)}")
check("helm-bad-deploy 对 chart 可见", "helm-bad-deploy" in visible, True)
check("helm-rollback 对 chart 可见", "helm-rollback" in visible, True)
check("restore 对 chart 不可见", "restore" not in visible, True)

visible_yaml = {a["name"] for a in m.ACTIONS.values()
                if a.get("scope", "all") in ("all", "yaml")}
print(f"  yaml 目标可见:  {sorted(visible_yaml)}")
check("restore 对 yaml 可见", "restore" in visible_yaml, True)
check("helm-bad-deploy 对 yaml 不可见", "helm-bad-deploy" not in visible_yaml, True)

print("\n" + "=" * 60)
if fails:
    print(f"❌ {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
