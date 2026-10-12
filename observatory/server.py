#!/usr/bin/env python3
"""K8s 观测台 · 本地后端代理。

为什么需要这个进程？
    浏览器不能直接调 K8s API —— 两个原因：
      1) **认证**：访问 API 需要客户端证书 / token，这些在 kubeconfig 里，
         不能（也不该）塞进网页。
      2) **CORS**：K8s API server 默认不允许网页跨域调用。
    所以标准做法是：网页 → 本机小代理 → kubectl → K8s API。
    本文件就是这个「小代理」，只监听 127.0.0.1，不暴露到局域网。

设计上刻意的取舍
    · **只用标准库**：不依赖 fastapi / requests，任何有 python3 的机器直接跑。
    · **所有读操作都走 `kubectl get -o json`**：不做缓存、不猜状态。
      网页看到的永远是集群此刻的真实状态。
    · **所有写操作都走真实 kubectl**：不是动画、不是 mock。
      你随时可以开个终端用 `kubectl get pods -w` 交叉验证。
      这是本观测台唯一的价值来源 —— 假的那种对你毫无意义。

用法：
    python observatory/server.py            # 默认 127.0.0.1:8899
    python observatory/server.py --port 9000
然后浏览器打开 http://127.0.0.1:8899
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 目标工作负载。观测台是「围绕这个 Deployment」建的。
#
# ⚠️ 这四个是**可变全局**：前端切换观测目标时由 set_target() 重写
#    （见文件后半部分）。之所以用全局可变、而不是把 ns 一路当参数传下去，
#    是为了让下面几十个 `"-n", NAMESPACE, ...` 调用点一行都不用改。
#    代价：切换的那一瞬间存在微秒级的「新旧值混读」窗口 —— 本机单人演示
#    工具可接受，且最坏结果只是某一轮轮询报错、下一轮自愈。
DEPLOYMENT = "llmops-api"
NAMESPACE = "default"
CONTAINER = "api"
APP_YAML = REPO_ROOT / "infra" / "k8s" / "app.yaml"

# 目标来源。yaml = W1 的裸清单（default ns，配 infra/k8s/app.yaml）；
# chart = W2 D6 起的 Helm 托管（dev / prod ns，配 llmops-chart）。
# 它决定了「恢复默认清单」这类操作该用 kubectl apply 还是 helm rollback。
TARGET_SOURCE = "yaml"
RELEASE_NAME = ""   # Helm release 名，如 llmops-dev（仅 chart 来源有值）
RELEASE_CHART = ""  # Chart 标识，如 llmops-chart-0.1.0

# kubectl 可执行文件。Windows 上通常是 kubectl.exe，shutil.which 会自动处理。
KUBECTL = shutil.which("kubectl") or "kubectl"

# helm 可执行文件。
# ⚠️ 本机 helm 装在 `D:\Software\0\helm` 且加在**用户 PATH** 里，
#    而由 bash / 某些父进程启动的 python 拿不到用户 PATH → shutil.which 返回 None。
#    所以显式补一个已知安装位置，找不到再退回 PATH 里的名字。
HELM = shutil.which("helm")
if not HELM:
    for _cand in (r"D:\Software\0\helm\helm.exe",
                 r"C:\Program Files\Helm\helm.exe"):
        if Path(_cand).exists():
            HELM = _cand
            break
HELM = HELM or "helm"

# Chart 目录 —— Helm 类操作（发坏版本 / 回滚）需要它。
CHART_DIR = REPO_ROOT / "infra" / "k8s" / "llmops-chart"

# 不是「我们自己的」namespace（系统组件），扫描可观测目标时跳过。
SYSTEM_NS = {"kube-system", "kube-node-lease", "kube-public",
             "local-path-storage", "ingress-nginx"}

# 单条 kubectl 命令的超时（秒）。观测台是交互式的，不能让一条卡住的命令
# 把整个页面拖死 —— 超时就报错，让用户看到「这条命令卡住了」这个事实本身。
KUBECTL_TIMEOUT = 20


# ---------------------------------------------------------------------------
# kubectl 封装
# ---------------------------------------------------------------------------

class KubectlError(RuntimeError):
    """kubectl 执行失败。带上 stderr，因为 K8s 的报错信息本身就是答案。"""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(self.friendly())

    def friendly(self) -> str:
        """把 kubectl 的报错压成一句人话。

        为什么需要这一步？
            kubectl 的原始 stderr 前缀很长（完整 exe 路径 + 完整 patch JSON），
            真正的答案被挤在最后几十个字符里。直接甩给用户等于没说。
            这里剥掉噪音，只留 "The Deployment ... is invalid: ..." 那段。
        """
        msg = self.stderr
        # 去掉包裹命令的引号，并截断过长的 patch 内容
        if len(msg) > 400:
            # 尝试找 K8s 自己的错误说明（通常以 The/Error/error 开头）
            for marker in ("The Deployment", "The Pod", "Error from server", "error:"):
                idx = msg.find(marker)
                if idx >= 0:
                    msg = msg[idx:]
                    break
        return msg.strip() or f"kubectl 退出码 {self.returncode}"


def kubectl(*args: str, timeout: int = KUBECTL_TIMEOUT) -> str:
    """跑一条 kubectl，返回 stdout。失败抛 KubectlError。"""
    cmd = [KUBECTL, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KubectlError(cmd, -1, f"超时 {timeout}s 未返回") from exc
    except FileNotFoundError as exc:
        raise KubectlError(cmd, -1, f"找不到 kubectl（{KUBECTL}）") from exc

    if proc.returncode != 0:
        raise KubectlError(cmd, proc.returncode, proc.stderr or proc.stdout)
    return proc.stdout


def kubectl_json(*args: str) -> dict:
    """跑 kubectl 并解析 JSON。"""
    out = kubectl(*args)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise KubectlError([KUBECTL, *args], 0, f"输出不是合法 JSON：{out[:200]}") from exc


def helm(*args: str, timeout: int = KUBECTL_TIMEOUT) -> str:
    """跑一条 helm，返回 stdout。失败抛 KubectlError。

    复用 KubectlError 是有意的：前端的错误展示、friendly() 的噪音剥离
    都是按 kubectl 的报错形状写的，而 helm 的报错形状类似
    （`Error: UPGRADE FAILED: ...`），套同一套展示逻辑正好。
    """
    cmd = [HELM, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KubectlError(cmd, -1, f"超时 {timeout}s 未返回") from exc
    except FileNotFoundError as exc:
        raise KubectlError(cmd, -1, f"找不到 helm（{HELM}）") from exc

    if proc.returncode != 0:
        raise KubectlError(cmd, proc.returncode, proc.stderr or proc.stdout)
    return proc.stdout


def helm_json(*args: str) -> list | dict:
    """跑 helm 并解析 JSON。helm 的空列表输出是 `[]` 或空串，两种都要认。"""
    out = helm(*args).strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise KubectlError([HELM, *args], 0, f"输出不是合法 JSON：{out[:200]}") from exc


# ---------------------------------------------------------------------------
# 状态采集：把 K8s 原始对象翻译成前端好用的形状
# ---------------------------------------------------------------------------

def _age(creation_ts: str | None) -> int:
    """K8s 的 creationTimestamp（RFC3339）→ 已存在秒数。

    ⚠️ 这里**必须**两端都用 UTC —— 不要「顺手改成 datetime.now()」。
        K8s 返回的 creationTimestamp 带 Z 后缀（UTC），
        只有减去另一个 UTC 时刻，差值才与机器时区无关。
        若一边换成本地时间，在 UTC+8 上会凭空多出 8 小时（28800 秒）。
        这是与上面 timestamp 字段相反的处理原则：
          · 给**人看**的时刻   -> 本地时间
          · **计算时长/跨时区** -> UTC
    """
    if not creation_ts:
        return 0
    try:
        # K8s 返回形如 2026-10-09T05:12:33Z
        t = datetime.strptime(creation_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


def _container_status(pod: dict, name: str) -> dict:
    """从 Pod 里挑出指定容器的 status；取不到就是空 dict。"""
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        if cs.get("name") == name:
            return cs
    return {}


def _probe_failures(pod: dict, container: str) -> dict:
    """从 Pod 事件里数各类探针失败次数。

    为什么读 events 而不是只看 status？
        `status` 只告诉你「现在 ready 不 ready」，
        events 才告诉你「是谁在拦它」—— 这是排错的关键分叉点：
        readiness 失败 → 不重启；liveness 失败 → 重启。
        页面上把这两个分开显示，用户一眼就能看出区别。

    ⚠️ 匹配坑（实测踩到）：
        K8s 的事件消息是 `Readiness probe failed: ...`，
        **不含容器名**。早期版本我写了「消息里必须出现容器名」的过滤，
        结果全部事件被判为「与容器无关」而丢弃 → 页面上 probeFailures 恒为 0，
        与肉眼可见的 0/1 状态自相矛盾。
        事件是按 pod 维度聚合的，Pod 里只有一个业务容器，
        所以**不需要**再按容器名过滤。
    """
    counts = {"startup": 0, "liveness": 0, "readiness": 0}
    meta = pod.get("metadata") or {}
    try:
        events = kubectl_json(
            "-n", NAMESPACE, "get", "events",
            "--field-selector", f"involvedObject.name={meta.get('name')},involvedObject.kind=Pod",
            "-o", "json",
        )
    except KubectlError:
        return counts

    for item in events.get("items") or []:
        if (item.get("reason") or "") != "Unhealthy":
            continue
        low = (item.get("message") or "").lower()
        # 用 count 而不是 +1 —— K8s 会把重复事件聚合成一条带 count 的，
        # 不读 count 会严重低估失败次数（实测差 22 倍）
        n = int(item.get("count") or 1)
        if "startup probe failed" in low:
            counts["startup"] += n
        elif "liveness probe failed" in low:
            counts["liveness"] += n
        elif "readiness probe failed" in low:
            counts["readiness"] += n
    return counts


def _read_deployment_spec(ns: str | None = None, deploy: str | None = None,
                          cont: str | None = None) -> tuple[dict, dict]:
    """读 Deployment 的一次性信息：探针配置 + 副本数等。

    ns/deploy/cont 可显式传入（collect_state 会传它的目标快照，保证
    整个采集过程用的是同一个目标）；不传则用当前全局量。

    抽出来的原因：这些是 **Deployment 层级**的属性，不随 Pod 变。
    早期版本把它写在 Pod 循环里，导致「N 个 Pod 就查 N 次 Deployment」——
    既慢又浪费。这里查一次，全循环复用。
    """
    probes: dict = {}
    dep_status: dict = {}
    ns = NAMESPACE if ns is None else ns
    deploy = DEPLOYMENT if deploy is None else deploy
    cont = CONTAINER if cont is None else cont
    try:
        dep = kubectl_json("-n", ns, "get", "deployment", deploy, "-o", "json")
    except KubectlError:
        return probes, dep_status

    try:
        # 一路 `or {}` 兜底：K8s 在「字段未设置」和「字段是 null」两种情况下的
        # JSON 形状不同，用 `.get(k, {})` 只能挡住前者。下面的写法两者都能挡。
        tmpl = (dep.get("spec") or {}).get("template") or {}
        tmpl_containers = (tmpl.get("spec") or {}).get("containers") or []
        for tc in tmpl_containers:
            if (tc.get("name") or "") != cont:
                continue
            for ptype in ("startupProbe", "livenessProbe", "readinessProbe"):
                p = tc.get(ptype) or {}
                if not p:
                    continue
                http = p.get("httpGet") or {}
                period = p.get("periodSeconds") or 0
                threshold = p.get("failureThreshold") or 0
                probes[ptype] = {
                    "path": http.get("path", ""),
                    "periodSeconds": p.get("periodSeconds"),
                    "failureThreshold": p.get("failureThreshold"),
                    "successThreshold": p.get("successThreshold"),
                    # 容忍窗口是 period × failureThreshold —— 最实用的派生值。
                    # 用户看到「0/1」时，第一个该问的就是「它还能撑多久」。
                    "toleranceSeconds": period * threshold,
                }
            # 演示旋钮当前值 —— 让用户看到「我刚改的那个值现在是多少」
            for e in tc.get("env") or []:
                if e.get("name") == "STARTUP_DELAY_SECONDS":
                    probes["_startupDelay"] = e.get("value")
    except (KeyError, TypeError):
        pass

    try:
        dep_status = {
            "replicas": (dep.get("spec") or {}).get("replicas"),
            "readyReplicas": (dep.get("status") or {}).get("readyReplicas", 0),
            "updatedReplicas": (dep.get("status") or {}).get("updatedReplicas", 0),
            "availableReplicas": (dep.get("status") or {}).get("availableReplicas", 0),
            "generation": (dep.get("metadata") or {}).get("generation"),
            "observedGeneration": (dep.get("status") or {}).get("observedGeneration"),
            # Deployment 自己的选择器 —— 下面 collect_state() 靠它找 Pod。
            # ⚠️ 原来这里写死 `app=llmops-api`，那是 W1 裸清单的标签；
            #    W2 D6 之后 dev / prod 的 Pod 带的是 Helm 的
            #    `app.kubernetes.io/name` + `app.kubernetes.io/instance`，
            #    写死的标签一条都匹配不到 → 页面永远是空的。
            #    从 spec.selector 读是唯一通用的做法。
            "selector": ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {},
            # 滚动更新卡住时，这两个值不相等 —— 是「为什么一直不收敛」的关键线索
            "rolloutStuck": (
                ((dep.get("status") or {}).get("updatedReplicas", 0))
                != ((dep.get("spec") or {}).get("replicas"))
            ),
        }
    except (KeyError, TypeError):
        pass

    return probes, dep_status


def _selector_of(dep_status: dict) -> str:
    """dep_status["selector"]（dict）→ kubectl -l 的字符串形式。

    Deployment 找不到时返回空串，调用方据此降级：
    退回写死的旧标签，至少 W1 那个 default ns 的目标还能动。
    """
    labels = dep_status.get("selector") or {}
    return ",".join(f"{k}={v}" for k, v in labels.items())


def _find_service(dep_status: dict, ns: str | None = None) -> str:
    """在当前 namespace 里找出「选择器被 Deployment 选择器覆盖」的 Service。

    ns 可显式传入（collect_state 传它的目标快照）；不传用当前全局量。

    为什么要按选择器匹配、而不是写死 Service 名？
        写死只适用于 W1（Service 名 == `llmops-api`）。Helm 管起来之后
        Service 名是 `<release>-<chart>`（如 `llmops-dev-llmops-chart`），
        不同 release 名字不同，写死必然对不上。

    匹配规则：Service 的选择器必须是 Deployment 选择器的**子集**。
        · Helm：两者完全相同（都来自 _helpers.tpl 的 selectorLabels）→ 命中
        · W1：Service `app=llmops-api`、Deployment `app=llmops-api` → 命中
        反过来不成立（Service 选择器比 Deployment 宽会选中别人的 Pod）。
    """
    dep_labels = dep_status.get("selector") or {}
    if not dep_labels:
        return ""
    ns = NAMESPACE if ns is None else ns
    try:
        svcs = kubectl_json("-n", ns, "get", "svc", "-o", "json")
    except KubectlError:
        return ""
    for svc in svcs.get("items", []) or []:
        sel = (svc.get("spec") or {}).get("selector") or {}
        if sel and all(dep_labels.get(k) == v for k, v in sel.items()):
            return svc["metadata"]["name"]
    return ""


def _release_info(ns: str | None = None, release: str | None = None) -> dict:
    """当前 namespace 的 Helm release 信息。非 Helm 目标返回空 dict。

    ns/release 可显式传入（collect_state 传它的目标快照）；不传用当前全局量。

    有了它，页面才能显示「这个 Deployment 是第几次发布的、状态是什么」——
    单纯看 Deployment 是看不出发布历史的。
    """
    ns = NAMESPACE if ns is None else ns
    release = RELEASE_NAME if release is None else release
    try:
        rels = helm_json("list", "-n", ns, "-o", "json") or []
    except KubectlError:
        return {}
    for r in rels if isinstance(rels, list) else []:
        if r.get("name") == release:
            return {
                "name": r.get("name"),
                "revision": int(r.get("revision") or 0),
                "status": r.get("status"),
                "chart": r.get("chart"),
                "appVersion": r.get("app_version"),
                "updated": r.get("updated"),
            }
    return {}


def collect_state() -> dict:
    """采集一次完整快照。这是前端每次轮询调用的东西。"""
    # ⚠️ 目标快照： collect_state 全程要读七八次全局量（读 Deployment、
    #    查 Pod、查 EndpointSlice、查 release、回 target 字段……），
    #    整个过程 1~2 秒。中途哪怕发生一次目标切换，就会产出
    #    「target 是 dev 但 Pod 是 prod」的混合快照 —— 页面顶栏和
    #    卡片墙自相矛盾（实测复现过）。锁内一次性读出，后面全用本地变量。
    with _target_lock:
        ns, deploy, cont = NAMESPACE, DEPLOYMENT, CONTAINER
        source, release_name = TARGET_SOURCE, RELEASE_NAME

    probes, dep_status = _read_deployment_spec(ns, deploy, cont)

    # Pod 选择器：优先用 Deployment 自己的；读不到就退回写死的旧标签。
    # 后面这个分支只在「Deployment 不存在」时走到，保证 W1 的 default
    # 目标即使清单被删了也能看到一个明确的空状态，而不是 500。
    selector = _selector_of(dep_status) or f"app={deploy}"
    pods_raw = kubectl_json(
        "-n", ns, "get", "pods",
        "-l", selector,
        "-o", "json",
    )

    pods = []
    for pod in pods_raw.get("items") or []:
        # 统一 `or {}` 兜底 —— 见 _read_deployment_spec 里的说明：
        # K8s 的「未设置」与「显式 null」是两种 JSON 形状，必须都挡住。
        meta = pod.get("metadata") or {}
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        cs = _container_status(pod, cont)

        # READY 分子分母
        ready_count = sum(
            1 for c in status.get("containerStatuses") or [] if c.get("ready")
        )
        total_count = len(spec.get("containers") or [])

        # 是不是「被杀过」—— terminated 且非 0 退出
        last_term = (cs.get("lastState") or {}).get("terminated") or {}
        waiting = (cs.get("state") or {}).get("waiting") or {}
        running = (cs.get("state") or {}).get("running") or {}

        # 退出码语义（前端直接展示，省得用户查）
        exit_code = last_term.get("exitCode")
        exit_hint = ""
        if exit_code == 137:
            exit_hint = "137 = 128+9（SIGKILL）→ 通常是被 OOMKill"
        elif exit_code == 143:
            exit_hint = "143 = 128+15（SIGTERM）→ 被优雅终止（通常是滚动更新/删除）"
        elif exit_code == 1:
            exit_hint = "1 = 进程自身退出码 1 → 应用报错，看容器日志"

        # 拉取镜像失败：waiting.reason 是权威判据
        image_pull_error = waiting.get("reason") in {
            "ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"
        }

        pods.append({
            "name": meta.get("name"),
            "node": spec.get("nodeName"),
            "ip": status.get("podIP"),
            "phase": status.get("phase"),
            "ready": f"{ready_count}/{total_count}",
            "readyBool": ready_count == total_count and total_count > 0,
            "restarts": cs.get("restartCount", 0),
            "ageSeconds": _age(meta.get("creationTimestamp")),
            "containerReady": bool(cs.get("ready")),
            "started": bool(cs.get("started")),
            "image": cs.get("image"),
            "imageID": (cs.get("imageID") or "").split("@")[-1][:19],
            "state": "running" if running else ("waiting" if waiting else "terminated"),
            "stateReason": waiting.get("reason") or last_term.get("reason") or "",
            "stateMessage": (
                waiting.get("message") or last_term.get("message") or ""
            )[:220],
            "exitCode": exit_code,
            "exitHint": exit_hint,
            "imagePullError": image_pull_error,
            "startedAt": running.get("startedAt"),
            "probeFailures": _probe_failures(pod, cont),
            # 探针配置放在 Pod 层级 —— 前端每个卡片都能显示「它被什么参数约束着」。
            # 这是本轮改动里最容易漏的一环：只显示「失败了」不够，
            # 还要显示「按什么节奏判定失败」，用户才能自己算出容忍窗口。
            "probes": probes,
        })

    # EndpointSlice —— 这是 readiness 的直接证据。
    # Pod 卡在 0/1 时，这里会变少；这是「不接流量」的权威判据。
    # ⚠️ Service 名是按选择器**查出来**的，不是写死的 —— 见 _find_service()。
    #    写死只适用于 W1（Service 名恰好等于 `llmops-api`）；Helm 管起来之后
    #    Service 名是 `<release>-<chart>`，写死一条都匹配不到。
    endpoints = []
    service_name = _find_service(dep_status, ns)
    if service_name:
        try:
            eps = kubectl_json(
                "-n", ns, "get", "endpointslice",
                "-l", f"kubernetes.io/service-name={service_name}",
                "-o", "json",
            )
            for slice_ in eps.get("items", []) or []:
                # ⚠️ 实测踩到：副本缩到 0 时，EndpointSlice 对象仍然存在，
                #    但它的 "endpoints" 字段是 **显式 null**，不是空数组。
                #    `.get("endpoints", [])` 的默认值只在「键不存在」时生效，
                #    键存在且值为 null 时照样返回 None → 迭代 None 直接抛
                #    TypeError，整个 /api/state 500，页面变砖。
                #    修法：取值后再 `or []` 兜一层。这类「显式 null」在 K8s
                #    API 里很常见（slices、conditions、status 字段都会这样）。
                for ep in slice_.get("endpoints") or []:
                    conditions = ep.get("conditions") or {}
                    addresses = ep.get("addresses") or [""]
                    endpoints.append({
                        "ip": addresses[0] if addresses else "",
                        "ready": bool(conditions.get("ready")),
                        "serving": bool(conditions.get("serving")),
                        "target": (ep.get("targetRef") or {}).get("name", ""),
                    })
        except KubectlError:
            pass

    # ⚠️ 用 **本地时间**，不是 UTC。
    # 这里踩过一次：原写法是 datetime.now(timezone.utc)，导致页面右上角的
    # 时钟比墙上时间慢 8 小时（UTC+8），而同一页的操作时间线用的却是本地时间
    # —— 同一屏出现两个相差 8 小时的时间，比不显示时间更糟。
    # 判定：给人看的界面一律用本地时间；只在跨时区计算/存储时才用 UTC。
    # （下面的 _age() 属于后者，所以它继续用 UTC，是对的。）
    return {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "timestampFull": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "deployment": dep_status,
        "probes": probes,
        "pods": pods,
        "endpoints": endpoints,
        "endpointReadyCount": sum(1 for e in endpoints if e["ready"]),
        # 当前目标是什么（前端切换栏要高亮）
        "target": {
            "ns": ns,
            "deployment": deploy,
            "container": cont,
            "source": source,
            "serviceName": service_name,
            "selector": selector,
        },
        # Helm release 现状（chart 来源才有）——「这是第几次发布、状态如何」
        "release": _release_info(ns, release_name),
    }


# ---------------------------------------------------------------------------
# 观测目标：扫描 + 切换
# ---------------------------------------------------------------------------

def discover_targets() -> list[dict]:
    """扫描全集群，列出所有可观测的目标（namespace + Deployment）。

    W2 D6 之前观测台只有一个目标（default / llmops-api），写死就够用。
    Chart 化之后 dev / prod 各有各的 Deployment 和 release，
    这个函数让页面能把它们列成一组切换按钮。

    来源判定：Deployment 的 label 里有没有 `app.kubernetes.io/managed-by: Helm`。
    这是 Helm 打的标准标签，比「猜名字」可靠。
    """
    out: list[dict] = []
    try:
        deps = kubectl_json("get", "deploy", "-A", "-o", "json").get("items", []) or []
    except KubectlError:
        return out

    # Helm release 清单，用来给 Helm 托管的 Deployment 补上 revision / status
    releases: dict[str, dict] = {}
    try:
        rels = helm_json("list", "-A", "-o", "json")
        if isinstance(rels, list):
            for r in rels:
                releases[f"{r.get('namespace')}/{r.get('name')}"] = r
    except KubectlError:
        pass

    for d in deps:
        meta = d.get("metadata") or {}
        ns = meta.get("namespace") or ""
        if ns in SYSTEM_NS:
            continue
        labels = meta.get("labels") or {}
        spec = d.get("spec") or {}
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
        containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
        if not containers:
            continue

        managed_by = labels.get("app.kubernetes.io/managed-by", "")
        is_helm = managed_by == "Helm"
        release_name = labels.get("app.kubernetes.io/instance", "") if is_helm else ""
        rel = releases.get(f"{ns}/{release_name}", {})

        out.append({
            "ns": ns,
            "deployment": meta.get("name", ""),
            "container": containers[0].get("name", ""),
            "source": "chart" if is_helm else "yaml",
            "release": release_name,
            "revision": int(rel.get("revision") or 0) if rel else 0,
            "status": rel.get("status", "") if rel else "",
            "chart": rel.get("chart", "") if rel else "",
            "selector": selector,
            "podsReady": f"{(d.get('status') or {}).get('readyReplicas', 0)}"
                         f"/{(d.get('spec') or {}).get('replicas') or 0}",
        })

    # 固定排序：default（W1 裸清单）排最前，其余按 ns 名字典序 ——
    # 让每次打开页面按钮位置都一样，不会因为 K8s 返回顺序变化而跳动。
    out.sort(key=lambda t: (t["ns"] != "default", t["ns"], t["deployment"]))
    return out


# 目标切换的写锁。服务端是 ThreadingHTTPServer，快速点切换栏时可能有两个
# /api/target 请求同时在飞 —— 这组全局量必须整组原子切换，见 set_target 的说明。
_target_lock = threading.Lock()


def set_target(t: dict) -> dict:
    """切换观测目标。重写全局变量，让下面所有 kubectl 调用点自动指向新目标。
    返回「实际生效的目标」，供路由回显。

    为什么用「重写全局」而不是传参？
        这样 act_* 系列函数、_read_deployment_spec、collect_state 里
        几十处 `"-n", NAMESPACE, ...` 一行都不用动 —— 它们本来就是在
        调用时读全局。

    ⚠️ 线程安全：这组全局量**必须整组原子切换**。曾经的坑：两个 /api/target
    请求的赋值交错，留下「ns=dev 但 deployment=llmops-prod-…」这种错配组合，
    之后所有 kubectl 都查无此物，按钮报「没有找到任何 Pod」，而页面顶部显示的
    目标却是对的 —— 极难排查。加锁串行化写方（写方只有这一个函数）。
    """
    global NAMESPACE, DEPLOYMENT, CONTAINER, TARGET_SOURCE
    global RELEASE_NAME, RELEASE_CHART, APP_YAML

    # 先把新目标整个算出来，再在锁内一次性落盘 —— 锁里只做赋值。
    applied = {
        "ns": t["ns"],
        "deployment": t["deployment"],
        "container": t.get("container") or "api",
        "source": t.get("source") or "yaml",
        "release": t.get("release") or "",
        "chart": t.get("chart") or "",
        # 切到 Helm 目标时，"恢复默认清单" 不该再 apply 那个裸 YAML：
        # 把它指向一个 Chart 里的说明性文件也行，但更安全的做法是**清空**，
        # 让按 yaml 作用域过滤的操作直接不出现在按钮区。
        "appYaml": (
            REPO_ROOT / "infra" / "k8s" / "app.yaml"
            if (t.get("source") or "yaml") == "yaml" else None
        ),
    }
    with _target_lock:
        NAMESPACE = applied["ns"]
        DEPLOYMENT = applied["deployment"]
        CONTAINER = applied["container"]
        TARGET_SOURCE = applied["source"]
        RELEASE_NAME = applied["release"]
        RELEASE_CHART = applied["chart"]
        APP_YAML = applied["appYaml"]
    return applied


# ---------------------------------------------------------------------------
# 操作：每个按钮背后是一个真实的集群改动
# ---------------------------------------------------------------------------
#
# 设计原则：每个操作都「可解释、可撤销、可核对」。
#   · 可解释 —— 返回值里带上「我执行了哪条 kubectl」，页面上显示出来
#   · 可撤销 —— 成对的 restore 操作，让用户能自己把状态掰回来
#   · 可核对 —— 全是标准 kubectl，用户开终端就能复现
#
# 所有操作都通过补丁 Deployment 实现，而不是杀掉进程 ——
# 因为「改清单 → 看 kubelet 怎么反应」才是 K8s 的核心心智模型。

ACTIONS: dict[str, dict] = {}


def action(name: str, label: str, explain: str, danger: bool = False,
           scope: str = "all"):
    """把函数注册成可被网页调用的操作。

    scope 决定这个按钮对哪些目标可见。取值必须和 TARGET_SOURCE
    （= "yaml" / "chart"）对得上：
      · "all"  —— 任何目标都能用（对 Deployment 打补丁类操作）
      · "yaml" —— 只对 W1 裸清单目标（default）可见。
                  典型是「恢复默认清单」，它 apply 的是 infra/k8s/app.yaml，
                  对 Helm 托管的目标毫无意义甚至有破坏性。
      · "chart" —— 只对 Helm 托管目标（dev / prod）可见。
                  典型是「发坏版本」「回滚」，走的是 helm upgrade / rollback。
    """
    def wrap(fn):
        ACTIONS[name] = {"name": name, "label": label, "explain": explain,
                         "danger": danger, "scope": scope, "fn": fn}
        return fn
    return wrap


def _patch_deployment(patch: dict) -> str:
    """对 Deployment 打一个 strategic-merge patch，返回实际执行的命令。"""
    payload = json.dumps(patch)
    kubectl(
        "-n", NAMESPACE, "patch", "deployment", DEPLOYMENT,
        "--type", "strategic", "-p", payload,
    )
    return f"kubectl -n {NAMESPACE} patch deployment {DEPLOYMENT} --type strategic -p '{payload}'"


@action(
    "delete-pod",
    "删掉一个 Pod",
    "模拟节点故障 / 进程暴毙。Deployment 控制器发现实际副本数 < 期望值，"
    "会立刻拉起一个新的。你会在卡片墙上看到：一个 Pod 消失 → 新 Pod 出现 → "
    "经历 startupProbe → 变 Ready。全程无需人工干预。",
)
def act_delete_pod() -> dict:
    # ⚠️ 多目标改造漏掉的一处：原来写死 `-l app={DEPLOYMENT}`，
    #    那是 W1 裸清单的标签；dev / prod 的 Pod 带的是 Helm 的
    #    `app.kubernetes.io/name` + `app.kubernetes.io/instance`，
    #    写死的标签一条都匹配不到 → 这个按钮在两个 Helm 目标上必失败
    #    （报「没有找到任何 Pod」，用户还以为集群坏了）。
    #    从 Deployment 自己的 spec.selector.matchLabels 读，三种目标通用。
    # 和 collect_state 同理：整个过程跨多次 kubectl 调用，先锁内读一份一致的
    # ns/deploy 快照，别让一次目标切换把前后两次读取劈成两个环境。
    with _target_lock:
        ns, deploy = NAMESPACE, DEPLOYMENT
    _, dep_status = _read_deployment_spec(ns, deploy)
    selector = dep_status.get("selector") or {}
    if not selector:
        # 读不到选择器 = Deployment 在该命名空间里不存在（典型：目标刚切换、
        # 或 ns/deployment 错配）。给一句能定位问题的话，别只说「没有 Pod」。
        raise RuntimeError(
            f"读不到 {ns}/{deploy} 的选择器 —— "
            f"Deployment 不在这个命名空间里，或目标状态还没就绪"
        )
    label_sel = ",".join(f"{k}={v}" for k, v in selector.items())
    pods = kubectl_json(
        "-n", ns, "get", "pods", "-l", label_sel, "-o", "json"
    )["items"]
    if not pods:
        raise RuntimeError("没有找到任何 Pod，先确认 Deployment 在运行")
    # 挑最新创建的那个删 —— 删掉最近的那个，观察窗口里剩下的那个还活着，
    # 能顺带证明「Service 不会因为少一个 Pod 就整体挂掉」。
    target = sorted(pods, key=lambda p: p["metadata"]["creationTimestamp"])[-1]
    name = target["metadata"]["name"]
    kubectl("-n", ns, "delete", "pod", name, "--wait=false")
    return {
        "summary": f"已删除 Pod {name}",
        "commands": [f"kubectl -n {ns} delete pod {name}"],
        "watchFor": "3 秒内会出现一个新 Pod（名字不同），它会走完 startupProbe 再变 Ready。",
    }


@action(
    "break-readiness",
    "把 readinessProbe 路径改坏",
    "把探针路径从 /ready 改成 /readiness（一个不存在的路径）。"
    "⚠️ 这是 D5 点名的头号坑：Pod 会一直显示 Running，但 READY 变成 0/1，"
    "而且 RESTARTS 保持 0 —— 因为 readiness 失败**不重启容器**，"
    "只是把 IP 从 EndpointSlice 里摘掉。症状是「Service 无响应，但 kubectl 看着一切正常」。",
    danger=True,
)
def act_break_readiness() -> dict:
    cmd = _patch_deployment({
        "spec": {"template": {"spec": {"containers": [{
            "name": CONTAINER,
            "readinessProbe": {"httpGet": {"path": "/readiness", "port": "http"}},
        }]}}}
    })
    return {
        "summary": "readinessProbe 路径已改为 /readiness（故意写错）",
        "commands": [cmd],
        "watchFor": "约 6 秒后（period 3s × threshold 2）Pod 变 READY 0/1，"
                    "STATUS 仍是 Running，RESTARTS 仍是 0，EndpointSlice 里的就绪条目变 0。",
    }


@action(
    "break-liveness",
    "把 livenessProbe 路径改坏",
    "把 liveness 路径也改成不存在的 /liveness。"
    "与上一个按钮对照着看：同样「探针失败」，readiness 失败只是摘流量，"
    "liveness 失败**会真的重启容器**。这是两个探针最本质的区别。",
    danger=True,
)
def act_break_liveness() -> dict:
    cmd = _patch_deployment({
        "spec": {"template": {"spec": {"containers": [{
            "name": CONTAINER,
            "livenessProbe": {"httpGet": {"path": "/liveness", "port": "http"}},
        }]}}}
    })
    return {
        "summary": "livenessProbe 路径已改为 /liveness（故意写错）",
        "commands": [cmd],
        "watchFor": "约 30 秒后（period 10s × threshold 3）容器被 kubelet 杀掉重启，"
                    "RESTARTS 开始增长。注意：它永远好不了，会一直重启。",
    }


@action(
    "slow-start",
    "模拟慢启动（30 秒）",
    "给应用注入 STARTUP_DELAY_SECONDS=30。应用启动时会先睡 30 秒再监听端口。"
    "因为配置了 startupProbe（容忍 60 秒），kubelet 会耐心等 —— "
    "这 30 秒里 liveness/readiness **都不会执行**，所以不会被误杀。",
)
def act_slow_start() -> dict:
    cmd = _patch_deployment({
        "spec": {"template": {"spec": {"containers": [{
            "name": CONTAINER,
            "env": [{"name": "STARTUP_DELAY_SECONDS", "value": "30"}],
        }]}}}
    })
    return {
        "summary": "STARTUP_DELAY_SECONDS 已设为 30",
        "commands": [cmd],
        "watchFor": "新 Pod 会有约 30 秒处于 0/1 且 STATUS 可能是 Running；"
                    "期间探针事件只出现 Startup probe failed，**不会**出现 Liveness probe failed。"
                    "30 秒后自动变 Ready —— 这就是 startupProbe 的价值。",
    }


@action(
    "trigger-oom",
    "触发 OOMKilled",
    "把内存 limit 降到 24Mi（低于应用实际占用）。容器会被内核 OOM Killer 干掉，"
    "退出码 137 = 128 + 9（SIGKILL）。这是排查「Pod 反复重启」时最该先看的一个信号。",
    danger=True,
)
def act_trigger_oom() -> dict:
    # ⚠️ 实测踩到的坑：requests 必须一起降。
    #    只改 limits 会被 K8s 拒绝：
    #      "resources.requests: Invalid value: 128Mi: must be less than or equal to memory limit of 32Mi"
    #    —— 因为 requests 是「调度预留」，语义上不能大于 limit，
    #    否则调度器会按一个容器永远达不到的规格去占位。
    #    所以这里把 requests 和 limits 一起设小。
    cmd = _patch_deployment({
        "spec": {"template": {"spec": {"containers": [{
            "name": CONTAINER,
            "resources": {
                "requests": {"cpu": "50m", "memory": "24Mi"},
                "limits": {"cpu": "500m", "memory": "24Mi"},
            },
        }]}}}
    })
    return {
        "summary": "内存 limit/request 已降到 24Mi（低于应用实际需求）",
        "commands": [cmd],
        "watchFor": "容器会迅速被杀，退出码 137、reason=OOMKilled，RESTARTS 增长。"
                    "卡片上会显示红色的 OOMKilled 标记。点「恢复默认清单」即可复原。",
    }


@action(
    "restore",
    "恢复默认清单",
    "从头应用 infra/k8s/app.yaml —— 把前面所有改动一次性抹掉，"
    "回到 D5 验收通过的干净状态。这是你的「一键复原」按钮。",
    scope="yaml",
)
def act_restore() -> dict:
    kubectl("-n", NAMESPACE, "apply", "-f", str(APP_YAML))
    # 滚动更新期间旧 Pod 会残留，触发一次 rollout restart 让状态收敛得更快
    kubectl("-n", NAMESPACE, "rollout", "restart", f"deployment/{DEPLOYMENT}")
    return {
        "summary": "已重新应用 infra/k8s/app.yaml 并触发滚动重启",
        "commands": [
            f"kubectl -n {NAMESPACE} apply -f {APP_YAML}",
            f"kubectl -n {NAMESPACE} rollout restart deployment/{DEPLOYMENT}",
        ],
        "watchFor": "旧 Pod 会逐个被新 Pod 替换。全部变 Ready 后，"
                    "EndpointSlice 的就绪条目应恢复为 2。",
    }


# --- Helm 专属操作 -------------------------------------------------------------
#
# 这两个按钮把 W2 D6「做③」的手工流程变成一键：
#   发坏版本 → 看闸门拦住 → 回滚 → 看服务恢复
# 用手工命令一样能完成，但页面上能同时看到 Pod 卡片墙 / EndpointSlice /
# 操作时间线三处的联动，这是终端里做不到的。

def _release_history() -> list[dict]:
    """当前 release 的完整 revision 历史（helm history -o json）。"""
    if not RELEASE_NAME:
        return []
    hist = helm_json("history", RELEASE_NAME, "-n", NAMESPACE, "-o", "json")
    return hist if isinstance(hist, list) else []


def _current_revision() -> int:
    """当前 revision。

    ⚠️ 踩过的坑：原本写的是 `hist[0].revision`，假定了 helm history
    按 revision **倒序**返回。实测（helm v4.3.0）它是**正序**的
    （1, 2, 3 …），于是这个函数永远返回 1。
    连带后果：「回滚」按钮算出的目标是当前版本本身，点下去是空操作。

    改成取**最大 revision**，不依赖任何排序假设 —— 这本就是
    「当前版本」的定义。反正历史记录都要全量解析，取 max 不比取首元素贵。
    """
    revs = []
    for h in _release_history():
        try:
            revs.append(int(h.get("revision") or 0))
        except (TypeError, ValueError):
            continue
    return max(revs) if revs else 0


def _last_deployed_revision(current: int) -> int:
    """找出「最近一个成功发布过、且不是当前」的 revision。

    ⚠️ 两个都踩过的坑：
    1. **不能简单取「上一个 revision」** —— 如果上一次是「发坏版本」造出来的
       failed，`helm rollback` 不带版本号会把自己回滚到坏版本上，白折腾。
    2. **不能只认 status == "deployed"** —— helm 在回滚后会把旧版本的
       status 改写成 `superseded`（意思是「成功发布过、后来被替换」）。
       实测 llmops-dev 的历史：rev1 superseded、rev2 failed、rev3 deployed。
       只认 deployed 时，当前是 rev3 就**一个都选不出来**。

    所以判据用**白名单排除坏状态**：superseded 也是可用的，只有 failed
    （以及 pending-* / uninstalling 这些中间态）不行。
    """
    best = 0
    for h in _release_history():
        try:
            rev = int(h.get("revision") or 0)
        except (TypeError, ValueError):
            continue
        if rev == current:
            continue
        if h.get("status") not in ("deployed", "superseded"):
            continue
        if rev > best:
            best = rev
    return best


@action(
    "helm-bad-deploy",
    "发一个坏版本（readiness 探针改坏）",
    "helm upgrade 时把 readinessProbe 路径改成不存在的 /bad-path，"
    "并带 --wait --timeout 45s。新 Pod 会一直卡在 0/1，"
    "helm 会在 45 秒后报 UPGRADE FAILED，revision 记为 failed。"
    "✨ 重点观察：**服务不会中断** —— EndpointSlice 里始终只有旧 Pod，"
    "因为 readiness 未就绪的 Pod 根本不会进 endpoints。这就是探针的价值。",
    danger=True,
    scope="chart",
)
def act_helm_bad_deploy() -> dict:
    # ⚠️ 子进程超时必须**大于** helm 自己的 --wait --timeout。
    #    踩过的坑：helm() 默认用 KUBECTL_TIMEOUT=20s，而这里传给 helm 的是
    #    --timeout 45s。20 秒时 wrapper 先把子进程杀了，helm 来不及把 release
    #    标记成 failed，history 里留下一个尴尬的 pending-upgrade ——
    #    既不是干净的失败也不是成功，后续回滚还要多绕一步。
    timeout = "45s"
    cmd = (
        f"helm upgrade {RELEASE_NAME} {CHART_DIR} -n {NAMESPACE} "
        f"--reuse-values --set readinessProbe.httpGet.path=/bad-path "
        f"--wait --timeout {timeout}"
    )
    helm(
        "upgrade", RELEASE_NAME, str(CHART_DIR), "-n", NAMESPACE,
        "--reuse-values",
        "--set", "readinessProbe.httpGet.path=/bad-path",
        "--wait", "--timeout", timeout,
        timeout=90,   # 45s 的 helm 超时 + 余量
    )
    return {
        "summary": "坏版本已下发（--wait 未等到就绪）",
        "commands": [cmd],
        "watchFor": "Pod 卡片墙出现一个新 Pod（0/1，RESTARTS 仍为 0）；"
                    "EndpointSlice 数量不变，服务照常响应。"
                    "45 秒后 helm 报错、revision 记为 failed —— 然后点「回滚到上一个可用版本」。",
    }


@action(
    "helm-rollback",
    "回滚到上一个可用版本",
    "helm rollback 到最近一个**成功发布过**的 revision"
    "（status 是 deployed 或 superseded；自动跳过 failed，"
    "也不会被回滚后改写成 superseded 的历史骗到）。",
    scope="chart",
)
def act_helm_rollback() -> dict:
    current = _current_revision()
    target = _last_deployed_revision(current)
    if not target:
        raise RuntimeError(
            f"release {RELEASE_NAME} 的历史里找不到可回滚的版本"
            f"（当前 revision {current}）"
        )
    helm("rollback", RELEASE_NAME, str(target), "-n", NAMESPACE, "--wait",
         timeout=90)
    return {
        "summary": f"已从 revision {current} 回滚到 revision {target}",
        "commands": [
            f"helm rollback {RELEASE_NAME} {target} -n {NAMESPACE} --wait",
        ],
        "watchFor": "坏 Pod 被回收，Pod 卡片墙恢复全部就绪；"
                    "helm status 变回 deployed，history 里多出一条 Rollback 记录。",
    }


@action(
    "scale-zero",
    "把副本数缩到 0",
    "把 Deployment 的 replicas 设为 0。所有 Pod 被删除，EndpointSlice 变空。"
    "用来验证「没有后端时，Service 是什么状态」—— 它不会报错，只是没有可用端点。",
    danger=True,
)
def act_scale_zero() -> dict:
    cmd = _patch_deployment({"spec": {"replicas": 0}})
    return {
        "summary": "replicas 已设为 0",
        "commands": [cmd],
        "watchFor": "两个 Pod 都会被终止，卡片墙清空，就绪端点数变 0。"
                    "点「恢复默认清单」把 replicas 拉回 2。",
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

# 操作日志。前端会轮询它，用来画「操作时间线」。
# 内存里保留最近 100 条 —— 这是演示工具，不需要持久化。
_action_log: list[dict] = []
_log_lock = threading.Lock()


def _log(entry: dict) -> None:
    with _log_lock:
        _action_log.insert(0, entry)
        del _action_log[100:]


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMOpsObservatory/1.0"

    # --- 工具 -----------------------------------------------------------------

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静音默认访问日志，太吵
        pass

    # --- 路由 -----------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return

        if path == "/api/state":
            # 兜住所有异常，而不是只兜 KubectlError。
            # 理由：这是每 1.5 秒被调一次的轮询接口，
            # 任何一种未预料的异常（比如 K8s 某个字段是 null 导致的 TypeError）
            # 都会让连接被直接掐断 —— 页面上表现为「连接失败」，但根因是后端 500。
            # 把异常转成 **结构化 JSON 错误**，前端就能把真实原因显示出来。
            try:
                self._send_json(collect_state())
            except KubectlError as exc:
                self._send_json({"error": exc.friendly()}, status=500)
            except Exception as exc:  # noqa: BLE001 — 最后一道防线，必须宽
                self._send_json(
                    {"error": f"{type(exc).__name__}: {exc}"}, status=500
                )
            return

        if path == "/api/actions":
            # 按当前目标的作用域过滤按钮：
            # 切到 Helm 目标时不会冒出「恢复默认清单」（它 apply 的是裸 YAML，
            # 对 Helm 托管的目标毫无意义），切到 W1 目标时也不会冒出 helm 按钮。
            visible = [
                {"name": a["name"], "label": a["label"],
                 "explain": a["explain"], "danger": a["danger"]}
                for a in ACTIONS.values()
                if a.get("scope", "all") in ("all", TARGET_SOURCE)
            ]
            self._send_json({
                "actions": visible,
                "log": _action_log[:20],
            })
            return

        if path == "/api/targets":
            self._send_json({"targets": discover_targets()})
            return

        if path == "/api/releases":
            # helm list -A + 每个 release 的 history。
            # 这是 W2 D6 的直接成果：revision / status / description
            # 能让「1 deployed → 2 failed → 3 Rollback to 1」这条审计链
            # 一眼看见，只看 Deployment 是看不到发布历史的。
            try:
                rels = helm_json("list", "-A", "-o", "json")
                rels = rels if isinstance(rels, list) else []
            except KubectlError as exc:
                self._send_json({"error": exc.friendly()}, status=500)
                return
            for r in rels:
                try:
                    hist = helm_json("history", r["name"], "-n", r["namespace"], "-o", "json")
                    r["history"] = hist if isinstance(hist, list) else []
                except KubectlError:
                    r["history"] = []
            self._send_json({"releases": rels})
            return

        if path == "/api/health":
            self._send_json({"ok": True, "kubectl": KUBECTL, "helm": HELM})
            return

        self.send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == "/api/target":
            # 切换观测目标。body: {"ns": "...", "deployment": "...", ...}
            # 故意做得很薄：真正的目标清单由 /api/targets 给出，
            # 这里只负责「用户从前端选的那一项」。
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "body 不是合法 JSON"}, status=400)
                return
            if not body.get("ns") or not body.get("deployment"):
                self._send_json({"error": "缺少 ns 或 deployment"}, status=400)
                return
            applied = set_target(body)
            self._send_json({"ok": True, "target": {
                "ns": applied["ns"], "deployment": applied["deployment"],
                "container": applied["container"], "source": applied["source"],
                "release": applied["release"], "chart": applied["chart"],
            }})
            return

        if not path.startswith("/api/action/"):
            self.send_error(404, "not found")
            return

        name = path[len("/api/action/"):]
        spec = ACTIONS.get(name)
        if spec is None:
            self._send_json({"error": f"未知操作：{name}"}, status=400)
            return

        started = time.time()
        entry: dict | None = None
        try:
            result = spec["fn"]()
            entry = {
                "action": name,
                "label": spec["label"],
                "at": datetime.now().strftime("%H:%M:%S"),
                "ok": True,
                "summary": result.get("summary", ""),
                "commands": result.get("commands", []),
                "watchFor": result.get("watchFor", ""),
                "elapsedMs": int((time.time() - started) * 1000),
            }
            self._send_json(entry)
        except (KubectlError, RuntimeError) as exc:
            entry = {
                "action": name,
                "label": spec["label"],
                "at": datetime.now().strftime("%H:%M:%S"),
                "ok": False,
                "summary": str(exc),
                "commands": [],
                "watchFor": "",
                "elapsedMs": int((time.time() - started) * 1000),
            }
            self._send_json(entry, status=500)
        except Exception as exc:  # noqa: BLE001 — 兜底：意外也必须以 JSON 告诉前端
            # ⚠️ 曾经的坑：这里只捕 KubectlError/RuntimeError。别的异常
            #    （比如 AttributeError）会逃到 finally 时因 entry 未绑定
            #    再抛一个 NameError 把真实原因盖掉 —— 用户端只收到非 JSON
            #    的 500，时间线显示 ✕ 却看不到任何原因，服务端也没日志。
            entry = {
                "action": name,
                "label": spec["label"],
                "at": datetime.now().strftime("%H:%M:%S"),
                "ok": False,
                "summary": f"{type(exc).__name__}: {exc}",
                "commands": [],
                "watchFor": "",
                "elapsedMs": int((time.time() - started) * 1000),
            }
            self._send_json(entry, status=500)
        finally:
            if entry is not None:
                _log(entry)


def main() -> None:
    parser = argparse.ArgumentParser(description="K8s 观测台本地代理")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认只绑本机，不要改成 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8899, help="监听端口")
    args = parser.parse_args()

    # 启动前先自检：kubectl 在不在、集群通不通。
    # 早失败比晚失败好 —— 否则用户打开网页只看到一片空白，不知道为什么。
    print(f"kubectl: {KUBECTL}")
    print(f"helm:    {HELM}")
    try:
        ctx = kubectl("config", "current-context").strip()
        print(f"当前 context: {ctx}")
        if not ctx.startswith("kind-"):
            print(f"⚠️  当前 context 不是 kind-*，请确认 kubectl 指向的是 kind 集群")
        pods = kubectl_json("-n", NAMESPACE, "get", "pods",
                            "-l", f"app={DEPLOYMENT}", "-o", "json")
        print(f"找到 {len(pods.get('items', []))} 个 {DEPLOYMENT} Pod")
    except KubectlError as exc:
        print(f"❌ 集群自检失败：{exc}")
        print("   请确认 kind 集群在运行：kind get clusters")
        raise SystemExit(1)

    # 扫一遍可观测目标。启动时就打印，用户一眼能看出「有几个环境可切」。
    try:
        targets = discover_targets()
        print(f"\n发现 {len(targets)} 个观测目标：")
        for t in targets:
            tag = "Helm" if t["source"] == "chart" else "裸清单"
            extra = (f"  release={t['release']} rev={t['revision']} {t['status']}"
                     if t["source"] == "chart" else "")
            print(f"  · {t['ns']}/{t['deployment']}  [{tag}]  Pod {t['podsReady']}{extra}")
        print(f"默认目标：{NAMESPACE}/{DEPLOYMENT}（页面上可切换）")
    except KubectlError as exc:
        print(f"⚠️  扫描观测目标失败：{exc}")

    url = f"http://{args.host}:{args.port}"
    print(f"\n观测台已就绪 → {url}\n按 Ctrl+C 停止。\n")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
