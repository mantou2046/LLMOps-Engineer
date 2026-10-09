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
DEPLOYMENT = "llmops-api"
NAMESPACE = "default"
CONTAINER = "api"
APP_YAML = REPO_ROOT / "infra" / "k8s" / "app.yaml"

# kubectl 可执行文件。Windows 上通常是 kubectl.exe，shutil.which 会自动处理。
KUBECTL = shutil.which("kubectl") or "kubectl"

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


def _read_deployment_spec() -> tuple[dict, dict]:
    """读 Deployment 的一次性信息：探针配置 + 副本数等。

    抽出来的原因：这些是 **Deployment 层级**的属性，不随 Pod 变。
    早期版本把它写在 Pod 循环里，导致「N 个 Pod 就查 N 次 Deployment」——
    既慢又浪费。这里查一次，全循环复用。
    """
    probes: dict = {}
    dep_status: dict = {}
    try:
        dep = kubectl_json("-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json")
    except KubectlError:
        return probes, dep_status

    try:
        # 一路 `or {}` 兜底：K8s 在「字段未设置」和「字段是 null」两种情况下的
        # JSON 形状不同，用 `.get(k, {})` 只能挡住前者。下面的写法两者都能挡。
        tmpl = (dep.get("spec") or {}).get("template") or {}
        tmpl_containers = (tmpl.get("spec") or {}).get("containers") or []
        for tc in tmpl_containers:
            if (tc.get("name") or "") != CONTAINER:
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
            # 滚动更新卡住时，这两个值不相等 —— 是「为什么一直不收敛」的关键线索
            "rolloutStuck": (
                ((dep.get("status") or {}).get("updatedReplicas", 0))
                != ((dep.get("spec") or {}).get("replicas"))
            ),
        }
    except (KeyError, TypeError):
        pass

    return probes, dep_status


def collect_state() -> dict:
    """采集一次完整快照。这是前端每次轮询调用的东西。"""
    pods_raw = kubectl_json(
        "-n", NAMESPACE, "get", "pods",
        "-l", f"app={DEPLOYMENT}",
        "-o", "json",
    )

    probes, dep_status = _read_deployment_spec()

    pods = []
    for pod in pods_raw.get("items") or []:
        # 统一 `or {}` 兜底 —— 见 _read_deployment_spec 里的说明：
        # K8s 的「未设置」与「显式 null」是两种 JSON 形状，必须都挡住。
        meta = pod.get("metadata") or {}
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        cs = _container_status(pod, CONTAINER)

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
            "probeFailures": _probe_failures(pod, CONTAINER),
            # 探针配置放在 Pod 层级 —— 前端每个卡片都能显示「它被什么参数约束着」。
            # 这是本轮改动里最容易漏的一环：只显示「失败了」不够，
            # 还要显示「按什么节奏判定失败」，用户才能自己算出容忍窗口。
            "probes": probes,
        })

    # EndpointSlice —— 这是 readiness 的直接证据。
    # Pod 卡在 0/1 时，这里会变少；这是「不接流量」的权威判据。
    endpoints = []
    try:
        eps = kubectl_json(
            "-n", NAMESPACE, "get", "endpointslice",
            "-l", f"kubernetes.io/service-name={DEPLOYMENT}",
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
    }


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


def action(name: str, label: str, explain: str, danger: bool = False):
    """把函数注册成可被网页调用的操作。"""
    def wrap(fn):
        ACTIONS[name] = {"name": name, "label": label, "explain": explain,
                         "danger": danger, "fn": fn}
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
    pods = kubectl_json(
        "-n", NAMESPACE, "get", "pods", "-l", f"app={DEPLOYMENT}", "-o", "json"
    )["items"]
    if not pods:
        raise RuntimeError("没有找到任何 Pod，先确认 Deployment 在运行")
    # 挑最新创建的那个删 —— 删掉最近的那个，观察窗口里剩下的那个还活着，
    # 能顺带证明「Service 不会因为少一个 Pod 就整体挂掉」。
    target = sorted(pods, key=lambda p: p["metadata"]["creationTimestamp"])[-1]
    name = target["metadata"]["name"]
    kubectl("-n", NAMESPACE, "delete", "pod", name, "--wait=false")
    return {
        "summary": f"已删除 Pod {name}",
        "commands": [f"kubectl -n {NAMESPACE} delete pod {name}"],
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
            self._send_json({
                "actions": [
                    {"name": a["name"], "label": a["label"],
                     "explain": a["explain"], "danger": a["danger"]}
                    for a in ACTIONS.values()
                ],
                "log": _action_log[:20],
            })
            return

        if path == "/api/health":
            self._send_json({"ok": True, "kubectl": KUBECTL})
            return

        self.send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/action/"):
            self.send_error(404, "not found")
            return

        name = path[len("/api/action/"):]
        spec = ACTIONS.get(name)
        if spec is None:
            self._send_json({"error": f"未知操作：{name}"}, status=400)
            return

        started = time.time()
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
        finally:
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
