# K8s 观测台

**一个能让你「看见」和「亲手按」的 K8s 控制台。**

不是教程的截图，不是日志粘贴 —— 是一个真的连着你本地 kind 集群的网页。
你在上面点的每一个按钮，都会**真的**改 Kubernetes 里的东西；
屏幕上卡片的变化，就是集群**真实**的反应。你可以随时另开一个终端用
`kubectl` 去核对，看到的应该完全一致。

---

## 为什么要有这个东西

学 K8s 最大的障碍不是概念难，而是**没有反馈**：

- 你改了 YAML，`kubectl apply` 返回 `configured` —— 然后呢？发生了什么？
- 教程说「readiness 失败会被摘流量」—— 摘流量长什么样？看不见。
- 我说「29 项验收全绿」—— 但那是我写的脚本、我判的分数，你凭什么信？

这个观测台解决的就是这件事：**把证据从「我的断言」换成「系统自己的行为」。**

---

## 怎么用

### 启动

双击 `observatory/start.bat`，浏览器会自动打开 `http://127.0.0.1:8899`。

或者手动：

```bash
python observatory/server.py            # 默认 127.0.0.1:8899
python observatory/server.py --port 9000
```

**前置条件**：kind 集群在运行，且 `kubectl config current-context` 是 `kind-*`。
启动脚本会自己检查并提示。

### 屏幕上的三块

| 位置 | 是什么 |
|---|---|
| **左上 · Pod 卡片墙** | 每个 Pod 一张卡。绿边 = 就绪，红边 = 不接流量，橙边 = Pending。每 1.5 秒自动刷新。 |
| **左下 · EndpointSlice** | Service **实际**会把流量发给哪些 IP。这是「接不接流量」的权威判据。 |
| **右侧 · 操作区 + 时间线** | 按钮点了什么、执行了哪条 kubectl、接下来该看哪里。 |

---

## 五个演示，建议按顺序做一遍

每个演示大约 1 分钟。**做完一个记得点「恢复默认清单」再试下一个。**

### 1. 删掉一个 Pod —— 看自愈

> 点 **删掉一个 Pod**

- **做什么**：`kubectl delete pod <名字>`
- **看什么**：被删的卡片消失 → 几秒内出现一张新卡（名字不同）→ 它走完启动流程变绿
- **要理解的**：你删的不是「服务」，是「一个实例」。Deployment 控制器发现实际数量
  少于期望数量，会自动补一个。**这就是声明式 API 的价值 —— 你说「我要 2 个」，
  K8s 负责维持这个状态，而不是你说「帮我启动第 3 个」。**
- **自己核对**：`kubectl get pods -w` 另开一个窗口盯着

### 2. 把 readiness 路径改坏 —— 最重要的一个

> 点 **把 readinessProbe 路径改坏**

- **做什么**：把探针路径从 `/ready` 改成 `/readiness`（不存在的路径）
- **看什么**：约 6 秒后，新 Pod 变 **红边**，`READY 0/1`，但 **`STATUS` 仍是 `Running`**，
  **`RESTARTS` 仍是 `0`**，同时 EndpointSlice 里的就绪条目减少
- **要理解的**：这是 D5 点名的头号坑，也是最容易误判的一种故障 ——
  你 `kubectl get pods` 看到一片 `Running`，以为一切正常，但服务就是没响应。
  **探针失败卡片上会显示 `readiness = 22` 而 `liveness = 0`**，这两个数字的对比
  就是答案：readiness 失败只摘流量，**从不重启**容器。
- **自己核对**：`kubectl get endpointslice -l kubernetes.io/service-name=llmops-api`

### 3. 把 liveness 路径改坏 —— 和上一个对照

> 点 **把 livenessProbe 路径改坏**

- **看什么**：约 30 秒后，`RESTARTS` 开始 **持续增长**，卡片反复闪红
- **要理解的**：**同样是「探针失败」，后果完全不同。**
  readiness → 摘流量（代价低，所以可以灵敏）；
  liveness → 杀容器重启（代价高，所以必须保守）。
  这个对比是这个观测台最想让你记住的一件事。
- ⚠️ 这个演示会一直重启下去（因为路径永远是错的），记得点恢复

### 4. 模拟慢启动 —— 看 startupProbe 的价值

> 点 **模拟慢启动（30 秒）**

- **做什么**：给应用注入 `STARTUP_DELAY_SECONDS=30`，它会先睡 30 秒再监听端口
- **看什么**：新 Pod 会经历大约 30 秒的 `READY 0/1`，卡片上的
  **`startup` 失败次数持续增长（3 → 7 → 12 → 16 → 18）**，
  但 **`liveness` 始终是 0，`RESTARTS` 始终是 0** —— 然后突然变绿
- **要理解的**：这 30 秒里 kubelet 每 2 秒探一次、一直失败，**但它没有杀掉容器**。
  因为配置了 startupProbe（容忍 60 秒），它成功之前 liveness 根本不会执行。
  **没有 startupProbe 的话，同样的慢启动会被 liveness 判定为死锁而反复杀掉** ——
  这就是 startupProbe 存在的唯一理由。
- **自己核对**：`kubectl describe pod <新pod> | grep -A2 Events` ——
  你会看到一串 `Startup probe failed`，**一条 `Liveness probe failed` 都没有**

### 5. 触发 OOMKilled —— 认识退出码 137

> 点 **触发 OOMKilled**

- **做什么**：把内存 limit 降到 24Mi（低于应用实际需求）
- **看什么**：容器迅速被杀，卡片显示红色 `OOMKilled`，`RESTARTS` 增长，
  并在 `OOMKilled` 与 `CrashLoopBackOff` 之间**来回切换**
- **要理解的**：`137 = 128 + 9`，9 是 SIGKILL。看到 137 就该先怀疑内存 limit。
  另外注意 **`CrashLoopBackOff` 不是一个错误，而是一种「等待策略」** ——
  K8s 在两次重启之间指数退避，避免疯狂重启拖垮节点。
- **自己核对**：`kubectl get pod <名字> -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'`

---

## 边界（这个工具不做什么）

| 不做 | 为什么 |
|---|---|
| 不模拟、不 mock | 所有操作走真实 kubectl。假的对你没有价值。 |
| 不监听 `0.0.0.0` | 只绑 `127.0.0.1`。它持有你集群的写权限，不该暴露到局域网。 |
| 不做鉴权 | 它假设你在本机、可信。**不要部署到服务器上。** |
| 不持久化 | 操作历史只存在内存里，重启就没了。这是演示工具，不是审计系统。 |
| 不覆盖 W1 以外的知识 | 目前只围绕 `llmops-api` 这一个 Deployment（探针 / 自愈 / 副本）。 |

---

## 已知的坑（都是实测踩出来的）

写这个工具的过程本身踩了不少坑，都记在代码注释里了，这里列几个最有价值的：

1. **K8s 的「未设置」和「显式 null」是两种 JSON 形状。**
   副本缩到 0 时，EndpointSlice 的 `endpoints` 字段是 `null` 而不是 `[]`。
   Python 的 `.get("endpoints", [])` 只在**键不存在**时给默认值，
   键存在且为 `null` 时照样返回 `None` → 迭代 `None` 直接 500，页面变砖。
   修法：取值后再 `or []`。这个坑在 K8s API 里非常常见。

2. **事件消息里没有容器名。**
   我一开始写了「消息里必须包含容器名」的过滤，结果所有探针失败事件
   都被判为「与容器无关」丢弃，页面上失败计数恒为 0 ——
   和肉眼可见的 `0/1` 状态自相矛盾。K8s 事件是按 **Pod** 维度聚合的。

3. **要读事件的 `count` 字段，不能每次 +1。**
   K8s 会把重复事件聚合成一条带 `count` 的记录。不读 `count`
   会严重低估失败次数（实测差 22 倍）。

4. **降低 memory limit 时必须同时降 requests。**
   只改 limit 会被拒绝：`requests: Invalid value: 128Mi: must be less than
   or equal to memory limit of 24Mi`。因为 requests 是调度预留，
   语义上不能大于 limit。

5. **`curl http://127.0.0.1:...` 在本机会被沙箱代理拦。**
   测本地服务要加 `--noproxy '*'`。（浏览器不受影响。）

---

## 文件结构

```
observatory/
├── server.py          # 后端：kubectl 封装 + 状态采集 + 7 个操作
├── start.bat          # 一键启动（GBK + CRLF 编码，改的时候别存成 UTF-8）
├── static/
│   ├── index.html     # 页面骨架
│   ├── app.css        # 样式（状态语义色：绿=健康，红=异常）
│   └── app.js         # 轮询 + 渲染 + diff 动画
└── README.md          # 本文件
```

后端只用 Python 标准库，没有第三方依赖。

---

## 和 W1 的关系

这个观测台是 W1（D3–D5）知识的**可视化外壳**：

| W1 学过 | 观测台里的哪个部分 |
|---|---|
| D3 · Deployment / Service | Pod 卡片墙 + EndpointSlice 面板 |
| D3 · 自愈 | 「删掉一个 Pod」 |
| D4 · Ingress | （外部访问走 `llmops.local`，本工具不涉及） |
| D5 · 三种探针 | 探针配置栏 + 「改坏 readiness / liveness」「慢启动」 |
| D5 · 排错 | 退出码提示 + OOM 演示 + CrashLoopBackOff |
| D5 · 退出码语义 | 卡片上的 `137`/`143` 注解 |

W2 之后可以继续往上挂：Helm 版本、HPA 副本曲线、节点资源水位……
