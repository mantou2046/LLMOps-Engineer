# kubectl Cheatsheet

> W1 D3「记」的产出物 —— 常用命令速查 + 排错路径。
> 来源：TechWorld with Nana《Kubernetes Crash Course for Absolute Beginners》+ 本项目实操。
> 学习笔记：`00笔记/K8s 起手学习（Nana 速成课）.md`

---

## 0. 动手前第一件事：确认上下文

装了 kind / minikube / Docker Desktop 之后，`~/.kube/config` 里会有**多个 context** —— 不对准集群操作是新手最常见的翻车点。

```bash
kubectl config get-contexts        # 列出所有 context，* 号是当前
kubectl config current-context     # 确认现在对着哪个集群
kubectl config use-context kind-kind   # 切换
```

> 配置文件位置：`~/.kube/config`（Windows：`C:\Users\<用户名>\.kube\config`）。
> **context = (cluster + user + namespace) 的命名组合** —— 切换它 = 换集群 / 换身份 / 换默认命名空间。

---

## 1. 看（get 看列表，describe 看详情）

```bash
kubectl get pods                     # 最常用
kubectl get pods -o wide             # 多两列：Pod IP / 所在节点
kubectl get pods -w                  # watch 实时刷新（改 YAML 时开着最直观）
kubectl get pods -A                  # 所有 namespace
kubectl get all                      # 一把看 pod/svc/deploy/rs
kubectl get deploy,rs,pod            # 按资源类型列
kubectl get pod <名> -o yaml         # 导出完整定义（含运行时填充的字段）

kubectl describe pod <名>            # ⭐ 排错主力，末尾的 Events 段是关键
kubectl get events --sort-by=.lastTimestamp   # 集群事件流，排错第二主力
kubectl top pod                      # 资源占用（需 metrics-server）
```

---

## 2. 日志与进容器

```bash
kubectl logs <pod>                   # 看日志
kubectl logs -f <pod>                # 跟踪（tail -f）
kubectl logs <pod> -c <容器名>        # 多容器 Pod 必须指定
kubectl logs <pod> --previous        # ⭐ 上一次崩溃前的日志（排查重启原因）
kubectl logs <pod> --tail=100        # 只看最后 100 行

kubectl exec -it <pod> -- sh         # 进容器 ⚠️ alpine 无 bash，用 sh；-- 不能少
kubectl exec -it <pod> -- ls /data   # 不进容器执行单条命令
```

---

## 3. 端口映射（本地验证服务最快的方式）

```bash
kubectl port-forward pod/<pod> 8080:80     # 本机 8080 → Pod 80
kubectl port-forward svc/<svc> 8080:80     # 也支持 svc
kubectl port-forward deploy/<名> 8080:80
```

> `port-forward` 走的是 apiserver 隧道，**只在执行期间有效**，Ctrl+C 即断。
> 与 NodePort 的区别：`port-forward` 不依赖节点端口、不需改 Service 类型，**临时调试用**；NodePort 是持久对外暴露。

---

## 4. 创建 / 更新 / 删除

```bash
kubectl apply -f app.yaml            # ⭐ 声明式创建或更新（首选）
kubectl delete -f app.yaml           # 按文件删
kubectl delete pod <名>               # 删 Pod —— Deployment 管的会立刻重建（验证自愈）
kubectl create deployment nginx --image=nginx --replicas=3   # 命令式（快速试验用）
kubectl expose deploy nginx --port=80 --type=LoadBalancer    # 快速建 Service
```

---

## 5. 扩缩 / 滚动更新 / 回滚

```bash
kubectl scale deploy/<名> --replicas=3
kubectl set image deploy/<名> <容器名>=<新镜像tag>   # 触发滚动更新
kubectl rollout status deploy/<名>    # 看滚动更新进度
kubectl rollout history deploy/<名>   # 看历史版本
kubectl rollout undo deploy/<名>      # 回滚到上一版
kubectl autoscale deploy/<名> --cpu-percent=50 --min=1 --max=10   # HPA
```

---

## 6. 排错路径 ⭐

**第一原则：`get` → `describe` → `logs`，三步走。**

```bash
kubectl get pods                      # ① 看 STATUS
kubectl describe pod <名>             # ② 拉到最底看 Events（90% 的答案在这）
kubectl logs <pod> --previous         # ③ 看上一条容器的日志
```

### 常见状态对照

| STATUS | 含义 | 先查什么 |
| --- | --- | --- |
| `Pending` | 没被调度到节点 | `describe` 看 Events：资源不足？节点没有？PVC 没绑上？ |
| `ContainerCreating` | 正在拉镜像 / 挂卷 | 镜像大？Volume 挂载慢？ |
| `ImagePullBackOff` | **拉不到镜像** | 镜像名/tag 拼错？私有仓库缺 `imagePullSecrets`？⚠️ kind 本地镜像：**`latest` + `imagePullPolicy: Always` 会去远端拉** → 用具体 tag 或 `IfNotPresent` |
| `CrashLoopBackOff` | 容器起来又崩，反复重启 | `logs --previous` 看崩因；探针配错也会导致反复重启 |
| `Running` 但访问不到 | Pod 正常、流量不通 | Service 的 `selector` 是否**真的匹配 Pod 的 labels**？`Endpoints` 是否为空？ |
| `OOMKilled`（describe 里看） | 超内存 limit 被杀 | `resources.limits.memory` 太小 |

### 一张图定位「Pod 起不来」

```
get pods → STATUS?
├─ Pending        → scheduler 没选节点   → describe 看 Events
├─ ImagePullBackOff → kubelet 拉镜像失败 → 查镜像名/tag/pullPolicy
├─ CrashLoopBackOff → 起来了但崩/探针失败 → logs --previous
├─ Terminating 卡住 → finalizer / 挂载卷 → describe 看 Events
└─ Running 但不通   → Service/Ingress 层   → 查 selector 与 Endpoints
```

---

## 7. 其他常用

```bash
kubectl api-resources                # 列出所有资源类型及简写
kubectl explain pod.spec             # 内置文档，查字段含义
kubectl apply -f -  <<EOF ... EOF    # 从 stdin 应用
kubectl cluster-info                 # 集群地址与组件状态
kubectl version                      # 客户端/服务端版本
```

---

## 8. 本项目踩坑记录

- ⚠️ **`latest` 标签 + `imagePullPolicy: Always`**：kind load 进集群的本地镜像也拉不到 → 用**具体 tag**（如 `app:1.0.0`）或 `IfNotPresent`。
- ⚠️ **`kubectl exec` 的 `--` 不能少**：少了会被当成 kubectl 自己的参数。
- ⚠️ **kind 依赖 WSL 集成**；`wsl --shutdown` 会**连带停掉 Docker 的 WSL 后端** → 引擎报 `rpc error ... EOF`，重启 Docker Desktop 恢复。
- ⚠️ alpine 镜像**没有 bash**，`exec` 进容器用 `sh`。
