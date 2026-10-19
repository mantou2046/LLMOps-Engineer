# K8s 概念补齐 —— 6 个「一句话定义 + 一个反例」

> W2 D12 产出（2026-10-16）。来源：Kubernetes 官方中文文档 + 本项目 kind 集群**现场实测**。  
> **验收标准**：每个概念能**一句话说清**，且能举出一个「**以为是这样、其实不是**」的反例。  
> ⚠️ 反例不是装饰 —— 只背定义的人举不出反例；举得出反例，说明你真撞过它、或想通过它为什么这么设计。
>
> **实测状态**：6 条里 **5 条已在 kind 上现场复现**（NetworkPolicy 于 10-15 实测，其余 4 条于 10-16 实测）；  
> 只有 HPA 的「稳定窗口」一项是引用 D11 的历史实测数据（本次未重跑压测）。每条末尾都给了可复现命令。

## 实测速览（kind · 2026-10-16）

| # | 概念                  | 现场结论                                                                                                          |
| - | ------------------- | ------------------------------------------------------------------------------------------------------------- |
| 1 | `volumeBindingMode` | `standard` = `rancher.io/local-path` + **`WaitForFirstConsumer`**（默认 SC）                                      |
| 2 | `IngressClass`      | `nginx` 类的 **DEFAULT 为空**；不带 `ingressClassName` 的 Ingress → `CLASS=<none>`、`ADDRESS=<none>`                   |
| 3 | headless DNS        | `postgres` → **Pod IP**（`<POD-IP>`）；`llmops-api` → **ClusterIP**（`<CLUSTER-IP>`）；2 副本 headless → **2 条 A 记录** |
| 4 | RBAC                | 自己的 ConfigMap `yes`、别人的 `no`、`secrets` `no`、跨命名空间 `no`、`list pods` `yes`                                      |
| 5 | NetworkPolicy       | 开着 netpol 跨命名空间**超时**，关掉立即 **200**（10-15）                                                                     |
| 6 | HPA                 | `describe hpa` 直接写 **"as a percentage of request"**：`6% (3m) / 50%`                                           |

## 总览

| # | 概念                               | 一句话                                     | 反例（以为 → 其实）                                                                               |
| - | -------------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------- |
| 1 | StorageClass `volumeBindingMode` | 决定 PVC **何时**绑定 / 制备 PV                 | 以为 `Immediate` 是默认值所以更优 → 拓扑受限时把 PV 建到 Pod 去不了的节点，Pod 永久 Pending                          |
| 2 | IngressClass 与控制器的绑定             | `Ingress → IngressClass → 控制器` 三级引用     | 以为「装了 ingress-nginx 所有 Ingress 就生效」→ 没指定 `ingressClassName` 就**没有控制器认领**，ADDRESS 空、访问 404 |
| 3 | headless Service 的 DNS           | 不分配 VIP，DNS 直接返回**各 Pod 的 A/AAAA 记录**   | 以为它也返回「一个稳定 IP」→ 返回的是**一串 IP**，负载均衡要客户端自己做                                                |
| 4 | RBAC 三层模型                        | 权限集合（Role/ClusterRole）× 绑定（Binding）= 授权 | 以为「ClusterRole 只能被 ClusterRoleBinding 引用」→ **RoleBinding 也能引用 ClusterRole**，效果被限制在本命名空间   |
| 5 | NetworkPolicy 默认行为               | 没配 = 全通；**被选中 = 默认拒绝**                  | 以为「写了 allow 就是白名单、其余照旧通」→ 其余全拒，**连 DNS 都拦**                                               |
| 6 | HPA 扩缩算法                         | 期望副本 = ⌈当前副本 × 当前指标 ÷ 期望指标⌉             | 以为按**节点 CPU 百分比**扩缩 → 实际按 **Pod 实际用量 ÷ requests**                                         |

---

## 1. StorageClass 的 `volumeBindingMode`

**一句话定义**  
它决定「PVC 什么时候变成已绑定的 PV」：`Immediate`（**未设置时的默认值**）在 **PVC 一创建**就绑定并动态制备；`WaitForFirstConsumer` 则**推迟到第一个使用该 PVC 的 Pod 被创建**时，好让调度器把 Pod 的拓扑约束（资源需求 / nodeSelector / 亲和反亲和 / 污点容忍）算进去。

**⚠️ 反例（以为是这样、其实不是）**  
以为「`Immediate` 是默认值，说明它更快更好，本地盘也该用它」。实际相反：本地盘、云盘这类**只在部分节点可达**的存储，用 `Immediate` 会在「还不知道 Pod 会调度到哪」的时候就随便挑一个节点把 PV 建好 —— Pod 后来被调度到别的节点，就**永远 Pending**（事件里是 `node(s) had volume node affinity conflict`）。

**为什么**  
绑定 PV 需要知道「卷在哪」；调度 Pod 又需要知道「卷在哪」才能挑节点 —— 这是个**鸡生蛋问题**。`Immediate` 单方面先绑，等于替调度器做了它做不了的决定；`WaitForFirstConsumer` 把决定权交回调度器，顺序才对。

**两个衍生坑**

- **默认值是 `Immediate`**，所以本地存储的 StorageClass 必须**显式**写 `volumeBindingMode: WaitForFirstConsumer`。
- 用了 `WaitForFirstConsumer` 就**别在 Pod 里写 `nodeName`** —— 那会绕过调度器，PVC 直接卡 `pending`。要指定节点用 `nodeSelector: kubernetes.io/hostname`。

**本项目落点 + 本地实测（2026-10-16）**  
`infra/k8s/postgres.yaml` 的 `volumeClaimTemplates` 没写 `storageClassName`，走的是 kind 自带的默认 SC。现场查：

```
$ kubectl get sc -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,BINDMODE:.volumeBindingMode,DEFAULT:...
standard   rancher.io/local-path   WaitForFirstConsumer   Delete   true
```

—— 默认 SC 的 BINDMODE 就是 **`WaitForFirstConsumer`**，这就是为什么在 kind 上从没踩到上面那个坑（换成云上 Immediate 的 SC，同一份 YAML 就可能挂）。

**一句话判据**  
`kubectl get sc -o custom-columns=NAME:.metadata.name,BINDMODE:.volumeBindingMode` —— 本地/云盘 SC 的 BINDMODE 一栏应该是 `WaitForFirstConsumer`。

---

## 2. `IngressClass` 与控制器的绑定

**一句话定义**  
`IngressClass` 是一个**集群级**资源，代表「一类 Ingress 由哪个控制器实现」；Ingress 通过 `spec.ingressClassName` **引用**它，它再用 `spec.controller`（如 `k8s.io/ingress-nginx`）指向真正的控制器 —— 即 **`Ingress → IngressClass → 控制器`** 三级引用。

**⚠️ 反例（以为是这样、其实不是）**  
以为「集群里装了 ingress-nginx，我写的 Ingress 就会自动生效」。实际：**Ingress 必须指定一个类**，否则没有控制器会认领它 —— `kubectl get ingress` 的 `ADDRESS` 一栏是空的，外部访问直接 404 / 连不上。这也是本项目 `infra/k8s/ingress.yaml` 里专门写了一句注释的原因。

**三个容易混的点**

- **`ingressClassName` 不是控制器名**，是 **IngressClass 资源的名字**（控制器名在 `IngressClass.spec.controller` 里）。名字对不上照样不生效。
- **默认类**：给某个 IngressClass 打注解 `ingressclass.kubernetes.io/is-default-class: "true"`，未指定 `ingressClassName` 的新 Ingress 才会被赋上它。**同一时刻只允许一个默认类**，否则准入控制器会拦掉新建的 Ingress。
- **旧注解 `kubernetes.io/ingress.class` 已废弃**，且与 `ingressClassName` **不完全等价**（旧注解直接写控制器名；新字段引用的是带额外配置的 IngressClass 资源）。本仓库统一用新字段。

**本项目落点 + 本地实测（2026-10-16）**  
`infra/k8s/llmops-chart/templates/ingress.yaml` 里 `ingressClassName: {{ .Values.ingress.className }}` —— **className 为空时整行不渲染**，于是就没有类，Ingress 静默失效。

现场把这条反例做了一遍：

```
# 现有 Ingress（values 里配了 className: nginx）
$ kubectl get ingress -A
NAMESPACE  NAME                      CLASS  HOSTS              ADDRESS    PORTS
default    llmops-api                nginx  llmops.local       localhost  80
dev        llmops-dev-llmops-chart   nginx  llmops-dev.local   localhost  80

# 临时建一个不带 ingressClassName 的 Ingress，等 10s
$ kubectl get ingress no-class-demo -o custom-columns=NAME:.metadata.name,CLASS:.spec.ingressClassName,ADDRESS:.status.loadBalancer.ingress
NAME            CLASS    ADDRESS
no-class-demo   <none>   <none>          ← 没有任何控制器认领
```

同时集群里的 `nginx` IngressClass **没有** `is-default-class` 注解（`DEFAULT` 一栏为空）—— 所以「省略 className 靠默认类兜底」这条路在这个集群里也是不通的。（演示资源已删除。）

**一句话判据**  
`kubectl get ingress -A` 的 `ADDRESS` 不为空、`CLASS` 一栏有值，才算被控制器认领。

---

## 3. headless Service 的 DNS 解析形态

**一句话定义**  
`spec.clusterIP: None` 的 Service 叫**无头 Service**：它**不分配 ClusterIP**、`kube-proxy` 不处理它、平台不提供负载均衡或路由 —— 取而代之的是**内部 DNS 直接返回它后端各 Pod 的 A/AAAA 记录**（即一组 Pod IP），让客户端自己决定连哪一个。

**⚠️ 反例（以为是这样、其实不是）**  
以为「headless Service 也是 Service，所以也返回一个固定的稳定 IP」。实际它返回的是**一串 Pod IP**（每个就绪 Pod 一条 A 记录），Pod 增减记录就变 —— 客户端要自己做轮询 / 客户端侧负载均衡，否则永远连第一个。它换来的是「**能看见每个后端**」而不是「一个 VIP」。

**三个衍生点**

- **无头 ≠ StatefulSet 专属**：普通 Deployment 也能用无头 Service（只是没有稳定身份）。反过来，**StatefulSet 的 `spec.serviceName` 必须指向一个 headless Service** —— 这是它「稳定网络标识」的前提（配合 `hostname` + `subdomain`，每个 Pod 才有 `<pod>.<svc>.<ns>.svc.cluster.local` 这种稳定域名）。
- **无选择算符的无头 Service**：需要自己管 Endpoints，且 `port` 必须与 `targetPort` 一致。
- **`publishNotReadyAddresses: true`** 时，**未就绪**的 Pod 也会被解析到 —— 对「先组集群再变就绪」的 StatefulSet 是必需的，对普通服务则是坑。

**本项目落点 + 本地实测（2026-10-16）**  
`infra/k8s/postgres.yaml` 里 `clusterIP: None` 就是无头 —— 因为它是 StatefulSet 的 `serviceName`，要的就是「每个 Pod 一个稳定域名」而不是一个 VIP。

现场三种解析对比（在 `default/llmops-api` Pod 里用 Python `getaddrinfo` 查）：

```
$ kubectl get svc -A | grep -E "postgres|llmops-api"
default   llmops-api   ClusterIP   <CLUSTER-IP>   ...   ← 普通 Service：一个 VIP
default   postgres     ClusterIP   None           ...   ← 无头

# 无头：解析到 Pod IP
$ nslookup postgres        → <POD-IP>            （= postgres-0 的 Pod IP，非 VIP）
# 普通：解析到 ClusterIP
$ nslookup llmops-api      → <CLUSTER-IP>        （= Service 的 VIP）

# 临时起一个 2 副本的 headless Service，验证「A 记录列表」
$ nslookup dnstest-headless → <POD-IP-1>
                              <POD-IP-2>         （= 两个 Pod 的 IP，两条 A 记录）
```

—— 最后那步是关键：**副本数 = A 记录条数**，客户端拿到的是「所有后端」，均衡得自己来。（演示资源已删除。）

**一句话判据**  
`kubectl run -it --rm dnstest --image=busybox -- nslookup <svc>` —— 普通 Service 返回**一个** ClusterIP，headless 返回**多条** Pod IP（有几条 = 有几个就绪 Pod）。

---

## 4. RBAC 的三层模型

**一句话定义**  
RBAC 就是「**谁**（Subject）**能对什么**（Resource）**做什么**（Verb）」：**Role / ClusterRole 定义权限集合**，**RoleBinding / ClusterRoleBinding 把权限授予主体**（User / Group / ServiceAccount）；Role 与 RoleBinding 是**命名空间级**，ClusterRole 与 ClusterRoleBinding 是**集群级**。

**⚠️ 反例（以为是这样、其实不是）**  
以为「ClusterRole 必须配 ClusterRoleBinding，Role 必须配 RoleBinding，两边不能交叉」。实际 **RoleBinding 可以引用 ClusterRole** —— 此时那套权限**只在 RoleBinding 所在的命名空间生效**，这是复用一套权限模板给多个命名空间的常规手法。**反过来不行**：ClusterRoleBinding 不能引用 Role（Role 是命名空间内的，绑不到集群范围）。

**两个衍生点**

- **绑定决定作用域，角色决定内容** —— 同一份 ClusterRole 被 RoleBinding 引用 = 只在本命名空间生效；被 ClusterRoleBinding 引用 = 全集群生效。
- **「零 Role」不等于「零权限」**：ServiceAccount 天生带 cluster-scoped 的只读 API discovery 权限（`/api/*`、`/healthz`、`/version`）。所谓「最小权限」是**收敛到内置基线**，不是收敛到零。（D7 双路实测：容器内用自带 token 打 API Server，读自己那一个 ConfigMap 200，读同命名空间别人的 403。）

**本项目落点 + 本地实测（2026-10-16）**  
`infra/k8s/llmops-chart/templates/rbac.yaml`（Role + RoleBinding）—— 应用用专属 ServiceAccount 跑。dev 里那份 Role 的实际规则只有两条：

```json
[{"apiGroups":[""],"resourceNames":["llmops-dev-llmops-chart-config"],"resources":["configmaps"],"verbs":["get"]},
 {"apiGroups":[""],"resources":["pods"],"verbs":["get","list"]}]
```

用 `auth can-i` 逐条打（`--as=system:serviceaccount:dev:llmops-dev-llmops-chart`）：

| 请求                                                            | 结果      |
| ------------------------------------------------------------- | ------- |
| `get configmap/llmops-dev-llmops-chart-config -n dev`（自己的）    | **yes** |
| `get configmap/kube-root-ca.crt -n dev`（同命名空间别人的）             | **no**  |
| `get secrets -n dev`                                          | **no**  |
| `get configmap/llmops-dev-llmops-chart-config -n prod`（跨命名空间） | **no**  |
| `list pods -n dev`                                            | **yes** |

—— 边界正好卡在「**这一个** ConfigMap + 读 Pod」，多一格都不给。注意这里用的是 **Role + RoleBinding**；如果把同一份规则写成 **ClusterRole**、再用 **RoleBinding** 引用，效果完全一样（作用域由 Binding 决定），这就是上面反例说的情况。

**一句话判据**  
`kubectl auth can-i get configmap/xxx --as=system:serviceaccount:<ns>:<sa> -n <ns>` —— 自己那一个 yes，别人的 / Secret / 跨命名空间都是 no。

---

## 5. NetworkPolicy 的默认行为

**一句话定义**  
它描述「哪些 Pod 允许和哪些 Pod 通信」；语义是**声明式的**：**没配任何策略 = 全通**；**一旦有策略选中某个 Pod = 该 Pod 默认拒绝**，只有策略里**显式 allow** 的方向才放行。

**⚠️ 反例（以为是这样、其实不是）**  
以为「配了 NetworkPolicy 就是加了个白名单，没提到的照旧通」。实际**恰好相反** —— 策略一旦选中某组 Pod，就把它切成**默认拒绝**：你只写了 ingress 的 allow，**egress 就全断（连 DNS 都出不去）**。所以必须显式放行 `53/UDP+TCP`，否则 Pod 会「莫名」连不上任何东西。


**另一个反例（环境层面）**  
以为「kind 用的 kindnetd 不支持 NetworkPolicy，所以本地测不了」。**实测是支持的**（kindnet v20260820）：开着 netpol 时从 `default` 命名空间的 Pod 访问 dev 的 Service → **超时**；关掉后同一测试 → 立即 200。对比明确，策略真的在拦流量。

**本项目落点**  
`infra/k8s/llmops-chart/templates/networkpolicy.yaml` + values 配置段；`helm lint` 0 failed 并部署到 dev（REVISION 13）。egress 显式放行了 DNS。

**一句话判据**  
开着 netpol：跨命名空间访问超时；关掉：立即 200。两者必须都测，只看一边会误判。

---

## 6. HPA 的扩缩算法

**一句话定义**  
HPA 按周期（默认 15s）读指标，算 **期望副本数 = ⌈当前副本数 × 当前指标 ÷ 期望指标⌉**，比值落在**容差 ±10%**（默认 `0.1`）内就不动；缩容还有 **300s 稳定窗口**（扩容默认 0s）。

**⚠️ 反例（以为是这样、其实不是）**  
以为「HPA 看的是**节点 CPU 百分比**」。实际是按 `targetAverageUtilization` 算的 **Pod 实际用量 ÷ Pod 的 `requests.cpu`** —— `requests` 声明的是 50m 而实测每 Pod 用到 90–135m，利用率就是 180–270%，**永远降不回 50%**，HPA 只能一路顶到 `maxReplicas`、毫无中档。**上 HPA 前必须先按实测用量校准 requests。**（D7 实测踩到）

**第二个反例**  
以为「停压就该立刻缩回」。实际缩容有 **5 分钟稳定窗口**：算法取窗口内**所有历史建议值的最大值**，所以要连续 5 分钟都建议缩小才真缩。（D7 实测：CPU 100 秒就掉到 6%，副本数死守 5 到第 360 秒。）

**四个衍生点**

- **多指标**：每个指标各算一遍，**取最大值**作为最终期望副本数。
- **扩容无稳定窗口**（默认 0s）—— 设计意图是「**扩容要果断、缩容要犹豫**」：扩容慢会丢流量，缩容慢只是多花点钱。
- **容差**（`behavior.scaleUp/scaleDown.tolerance`，v1.37 GA）防止指标小幅抖动引发反复扩缩；集群级默认由 `--horizontal-pod-autoscaler-tolerance` 控制。


- **`maxReplicas` 是硬顶，且 HPA 不改变单 Pod 的 `limits`** —— 本项目 D11 实测峰值只有 1.8 核（8 核的 22%），因为每 Pod `limits.cpu=500m` 是硬顶，5 副本合计上限才 2.5 核。

**本项目落点 + 本地实测（2026-10-16）**  
`infra/k8s/llmops-chart/templates/hpa.yaml`：dev 1–5@50%、prod 2–6@60%。

现场 `describe` 直接把这句反例写在输出里：

```
$ kubectl describe hpa -n dev llmops-dev-llmops-chart
Metrics:  ( current / target )
  resource cpu on pods  (as a percentage of request):  6% (3m) / 50%
Min replicas: 1     Max replicas: 5
Conditions: ScalingActive  True  ValidMetricFound  ...
```

—— 关键就是 `**as a percentage of request**` 这半句：分子 `3m` 是 Pod 的实际 CPU 用量，分母是 Pod 的 `requests.cpu`（50m），**6% = 3m ÷ 50m**。跟节点有多少核、节点用了百分之几**完全无关**。这也正是 D7 踩坑的机理：requests 定得比实际用量小，利用率就被系统性放大，HPA 一路顶到 `maxReplicas`。

> 扩容/缩容的**稳定窗口**一项本次没重跑压测，引用 D11 的历史实测：CPU 100 秒就掉到 6%，副本数**死守 5 到第 360 秒**（= 300s 缩容窗口 + 采样延迟）。

**一句话判据**  
`kubectl describe hpa <名>` 看 `Metrics` 一栏的 `xx% (值) / yy%` —— 分子是「实际用量」，分母是「requests」，不是节点容量。

---

## 参考

| 概念                | 官方文档                                                                                                    |
| ----------------- | ------------------------------------------------------------------------------------------------------- |
| volumeBindingMode | [存储类 StorageClass](https://kubernetes.io/zh-cn/docs/concepts/storage/storage-classes/)                  |
| IngressClass      | [Ingress](https://kubernetes.io/zh-cn/docs/concepts/services-networking/ingress/)                       |
| headless Service  | [Service（含无头 Service）](https://kubernetes.io/zh-cn/docs/concepts/services-networking/service/)          |
| RBAC              | [RBAC 鉴权](https://kubernetes.io/zh-cn/docs/reference/access-authn-authz/rbac/)                          |
| NetworkPolicy     | [网络策略](https://kubernetes.io/zh-cn/docs/concepts/services-networking/network-policies/)                 |
| HPA 算法            | [Pod 水平自动扩缩](https://kubernetes.io/zh-cn/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/) |

> ⚠️ 官方文档站现为 K8s 1.3x 版本，`Ingress` API 已冻结（官方推荐转向 Gateway API），本项目 `templates/httproute.yaml` 是同一件事的 Gateway 写法。
