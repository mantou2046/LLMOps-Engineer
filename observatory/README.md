# K8s 观测台

**一个能让你「看见」和「亲手按」的 K8s 控制台。**

覆盖三个观测目标：`default`（W1 裸清单）、`dev` / `prod`（W2 D6 的 Helm release）。

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

### 屏幕上的五块

| 位置 | 是什么 |
|---|---|
| **顶部 · 观测目标切换栏** | 三个目标：`default`（W1 裸清单）、`dev` / `prod`（Helm release）。点一下切换，下面所有面板都换成那个命名空间里的东西。 |
| **左上 · Pod 卡片墙** | 每个 Pod 一张卡。绿边 = 就绪，红边 = 不接流量，橙边 = Pending。每 1.5 秒自动刷新。 |
| **左下 · EndpointSlice** | Service **实际**会把流量发给哪些 IP。这是「接不接流量」的权威判据。 |
| **左中 · Helm 发布历史** | `helm history` 的网页版：每次 upgrade / rollback 都留一条 revision 记录，`failed` 标红、`Rollback to N` 看得见。切到 Helm 目标才出现。 |
| **右侧 · 操作区 + 时间线** | 按钮点了什么、执行了哪条 helm / kubectl、接下来该看哪里。按钮按目标来源过滤，不会出现「对 Helm 目标按恢复清单」这种错配。 |

---

## 五个基础演示，建议按顺序做一遍

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

## 两个 Helm 演示（W2 D6 新增，先切到 `dev` 目标再按）

前五个演示针对 `default` 目标（W1 的裸清单）。下面两个走 Helm——
**先在顶部切换栏点 `dev/llmops-dev-llmops-chart`**，操作区会多出两个按钮
（裸清单目标上的「恢复默认清单」在这里会消失，反之亦然——
按钮是按目标来源过滤的，`restore` 只对裸清单可见，两个 Helm 按钮只对 Helm 目标可见）。

⚠️ 这两个演示会真的动 `dev` 命名空间里的 release。跑完记得把状态点回干净
（回滚按钮自己会恢复到就绪状态）。

### 6. 发一个坏版本 —— 看探针怎么保住服务

> 点 **发一个坏版本（readiness 探针改坏）**

- **做什么**：`helm upgrade llmops-dev <chart> -n dev --reuse-values --set readinessProbe.httpGet.path=/bad-path --wait --timeout 45s`
- **看什么**：Pod 卡片墙出现一个新 Pod（`0/1`，但 `RESTARTS` 仍是 `0`）；
  **EndpointSlice 一个都没变**，`/health` 连打照样 200；
  约 45 秒后操作时间线显示 `UPGRADE FAILED`，左侧发布历史多一条 **failed** 记录
- **要理解的**：这是「Pod Running ≠ 能接流量」最贵的一课。就绪探针不过的 Pod
  **根本不会进 EndpointSlice**，所以滚动更新期间旧实例一直在扛流量——
  **服务零中断不是运气好，是探针在兜底。** 而 helm 的 `--wait` 会在超时后把这次发布
  标记为 failed，不会把坏版本留在 `deployed` 状态里骗人。
- **自己核对**：等按钮跑完的 45 秒里，另开终端 `kubectl get endpointslice -n dev`
  和 `curl http://localhost:30080/health`

### 7. 回滚到上一个可用版本

> 点 **回滚到上一个可用版本**

- **做什么**：从发布历史里挑最近一个**成功发布过**的 revision
  （`deployed` 或 `superseded`；自动跳过 `failed`），
  `helm rollback llmops-dev <rev> -n dev --wait`
- **看什么**：坏 Pod 被回收，卡片墙恢复全部就绪；发布历史多一条 `Rollback to N`，
  当前状态回到 `deployed`
- **要理解的**：回滚不是「退回到上一个 revision」这么简单——上一次发布可能就是坏的。
  这个按钮的挑法在后端 `_last_deployed_revision()`：**排除 `failed`，
  把 `superseded` 算可用**（helm 回滚后会把旧版本改写成 `superseded`，
  它是「成功发布过、后来被替换」，不是坏状态）。
- **自己核对**：`helm history llmops-dev -n dev`，对着页面上的表格逐行对

---

## 边界（这个工具不做什么）

| 不做 | 为什么 |
|---|---|
| 不模拟、不 mock | 所有操作走真实 kubectl / helm。假的对你没有价值。 |
| 不监听 `0.0.0.0` | 只绑 `127.0.0.1`。它持有你集群的写权限，不该暴露到局域网。 |
| 不做鉴权 | 它假设你在本机、可信。**不要部署到服务器上。** |
| 不持久化 | 操作历史只存在内存里，重启就没了。这是演示工具，不是审计系统。 |
| 不覆盖 W1/W2 以外的知识 | 只围绕已装的三个目标：探针 / 自愈 / 副本（裸清单）+ Helm 发布 / 回滚（dev、prod）。HPA、节点资源水位还没接。 |

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

6. **wrapper 的超时必须大于 helm 自己的 `--timeout`。**
   一开始「发坏版本」按钮 20 秒就被 wrapper 掐死，而传给 helm 的是
   `--wait --timeout 45s`。子进程被杀，helm 来不及把 release 标记成 failed，
   history 里留下一个 `pending-upgrade`——既不是干净的成功也不是干净的失败，
   后续回滚都要多绕一步。修法：这个按钮给子进程 90 秒。

7. **`helm history` 的 revision 是正序，不是倒序。**
   我按倒序写了 `hist[0].revision` 取「当前版本」，实测（helm v4.3.0）它返回 1，
   也就是最早那条。连带后果：「回滚」按钮算出的目标是当前版本本身，点下去是空操作。
   修法：取 `max(revs)`，不依赖任何排序假设。

8. **helm 回滚后，旧版本的 status 会被改写成 `superseded`。**
   它是「成功发布过、后来被替换」的意思，是**可用**版本。挑回滚目标时只认
   `status == "deployed"` 会一个都选不出来（实测 rev1 superseded、rev2 failed、
   rev3 deployed 的现场）。判据应该是**排除坏状态**（`failed` / `pending-*`），
   而不是只留好状态。

9. **多目标切换是有竞态的（服务端是 `ThreadingHTTPServer`）。**
   目标是一组全局量（namespace / deployment / container / release…），
   **写侧**两个 `/api/target` 请求交错赋值，会留下「ns=dev 但 deployment=prod」的
   错配组合，之后所有 kubectl 查无此物；**读侧** `collect_state` 全程要读七八次全局量、
   耗时 1~2 秒，中途切一次目标就产出「顶栏写 dev、卡片墙是 prod」的混合快照。
   修法两条：写侧加锁 + 锁内整组落盘；读侧在入口锁内做一次快照，全程用局部变量。
   这类「一组全局量必须作为一个整体读/写」的坑，是并发服务里最常见也最难查的一类。

10. **异常处理要覆盖「意外」。**
    action 路由原来只捕 `KubectlError` / `RuntimeError`；别的异常逃到
    `finally: _log(entry)` 时 `entry` 还没绑定，又抛一个 NameError 把真实原因盖掉 ——
    前端只收到非 JSON 的 500，时间线只有一个 ✕、看不到原因，服务端也没记录。
    修法：补一个 `except Exception` 兜底，并且 `finally` 里判空。

---

## 文件结构

```
observatory/
├── server.py           # 后端：kubectl/helm 封装 + 状态采集 + 9 个操作（按目标来源过滤）
├── start.bat           # 一键启动（GBK + CRLF 编码，改的时候别存成 UTF-8）
├── selftest_targets.py # 非破坏性自检：目标发现 / 切换 / 按钮作用域，21 项断言
├── e2e_buttons.py      # 端到端：从 API 点「发坏版本 → 回滚」验证闭环（只在 dev 上跑）
├── static/
│   ├── index.html      # 页面骨架（目标切换栏 + Helm 发布历史面板）
│   ├── app.css         # 样式（状态语义色：绿=健康，红=异常）
│   └── app.js          # 轮询 + 渲染 + diff 动画 + 目标切换
├── screens/            # 联调脚本（Playwright 浏览器验证 + 竞态压力测试）
│   ├── verify.js       # 浏览器端 18 项断言：按钮切换 / 发布面板 / 时间线去重 / 删 Pod
│   ├── final_shots.js  # 拍三个目标的交付截图
│   └── race_test2.py   # 并发切换 + 并发轮询，校验每份快照内部自相一致（纯标准库）
└── README.md           # 本文件
```

后端只用 Python 标准库，没有第三方依赖。

---

## 和 W1 / W2 的关系

这个观测台先是 W1（D3–D5）知识的**可视化外壳**，W2 D6 起又长出了 Helm 那一半：

| 学过 | 观测台里的哪个部分 |
|---|---|
| D3 · Deployment / Service | Pod 卡片墙 + EndpointSlice 面板 |
| D3 · 自愈 | 「删掉一个 Pod」 |
| D4 · Ingress | （外部访问走 `llmops.local`，本工具不涉及） |
| D5 · 三种探针 | 探针配置栏 + 「改坏 readiness / liveness」「慢启动」 |
| D5 · 排错 | 退出码提示 + OOM 演示 + CrashLoopBackOff |
| D5 · 退出码语义 | 卡片上的 `137`/`143` 注解 |
| W2 D6 · Chart 化 / 双环境 | 顶部目标切换栏（default / dev / prod 三个目标） |
| W2 D6 · Secret 闸门 | dev / prod 两边 Secret 各自独立，切目标即可对比 |
| W2 D6 · 探针保住服务 + 回滚 | 「发坏版本 → 回滚」两个按钮 + Helm 发布历史面板 |

W2 之后可以继续往上挂：HPA 副本曲线、节点资源水位、ConfigMap 热更新……
