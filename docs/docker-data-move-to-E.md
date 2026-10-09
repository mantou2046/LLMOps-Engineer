# 把 Docker 数据盘迁到 E 盘（C 盘瘦身）

> ✅ **已完成（2026-10-09 实测）**：C 盘 **37G → 48G（腾出 11 GB）**，
> E 盘 105G → 76G。kind 集群**原样恢复无需重建**，`verify_d4.sh` **19 项全绿**，
> postgres 数据完好。C 盘旧 vhdx 由 Docker Desktop **自动清理**（12 GB → 9.1 MB）。

> 背景：之前 C 盘 220G 已用 184G、**只剩 37G（84%）**；
> 而 Docker 的**全部**数据（镜像 / 容器 / 卷 / kind 节点）都落在
> `C:\Users\lile2\AppData\Local\Docker\wsl\disk\docker_data.vhdx`（**11.5 GB**）。
> 现迁到 **`E:\Projects\DockerData`**。

## 为什么目标位置不在项目目录里

**Docker 数据盘是全局的，不属于任何单个项目** —— 它装着所有项目共用的镜像、
所有容器、所有卷。所以：

- ✅ 放 `E:\Projects\DockerData`（与 `AIModel` / `FileStorages` 并列）：
  **语义正确**、备份/同步项目目录时不会连带拷 12GB、不被 IDE 的
  "clean project" 之类操作误伤。
- ❌ 放 `E:\Projects\LLMOps-Engineer\.docker`（**已否决**）：
  会让一个 618KB 的代码仓库看起来有 12GB；`.docker/` 听起来像
  "本项目专用的 Docker 配置"，但它其实是全机共享的数据盘。

## 为什么要迁（关键认知）

**项目源码在 E 盘 ≠ 数据在 E 盘。**

`E:\Projects\LLMOps-Engineer` 本身只占 **618 KB**（纯源码）。
但 kind 的「节点」本质是 **Docker 容器**，它的整个文件系统、每个镜像层、
postgres 的 PVC 数据卷，**全部写在那个 vhdx 里**。
所以：项目挂在 E 盘，数据却实打实地吃掉 C 盘 11.5 GB。

## 迁移前该知道的三件事

1. **vhdx 只涨不缩**。删了镜像（`docker rmi`）只是标记为可回收，
   文件本身**不会自动变小**。要真正还空间必须「压缩」或「迁移」。
2. **迁移 = 重建**。Docker Desktop 把数据盘换位置时，会在新位置建新 vhdx
   并把内容搬过去。**期间所有容器会停**，kind 集群要重新起。
3. **迁移前必须动的东西要先备份**。我们这个项目的 postgres 数据已备份在
   `.local-backup/llmops-backup-20261009.sql`。

## 操作步骤

### 第 0 步：确认已清理（省迁移量）

```bash
docker system df                # 看 RECLAIMABLE 列
docker builder prune -f         # 构建缓存
docker rmi llmops-api:naive     # 反面教材镜像，verify_d2.sh 可随时重建
```

> 2026-10-09 已做：镜像 4.58GB → **2.66GB**（省 1.93GB），构建缓存回收 1.03GB。

### 第 1 步：停止 Docker 并备份 kind 集群配置

```bash
# 导出集群里所有资源（万一迁移后要重建）
cd /e/Projects/LLMOps-Engineer
kubectl get all,cm,secret,pvc,ingress -A -o yaml > .local-backup/k8s-all-resources-20261009.yaml

# 备份 postgres 数据
kubectl exec postgres-0 -- pg_dump -U llmops -d llmops > .local-backup/llmops-backup-20261009.sql
```

### 第 2 步：在 Docker Desktop 界面里改数据盘位置

1. 打开 **Docker Desktop** → 右上角齿轮 **Settings**
2. 左侧选 **Resources** → 展开 **Advanced**
3. 找到 **Disk image location**，点 **Browse**
4. 选择 `E:\Projects\DockerData`
   （目录已建好；Docker 会在其下自己建 `DockerDesktopWSL\` 子目录）
5. 点 **Apply & restart**
6. 等待迁移完成（11.5GB，实测 2~3 分钟）

> § 实测更正（2026-10-09）：
> - 子目录名是 **`DockerDesktopWSL\`**（不是文档早期猜的 `wsl\`），
>   结构为 `DockerData\DockerDesktopWSL\disk\docker_data.vhdx`。
> - ⚠️ **原 C 盘的 vhdx 会被 Docker Desktop 自动删除！** 不需要手工清理
>   （早期版本会保留，我们这次实测是自动删的）。所以 **第 4 步只在发现
>   残留时才需要**；保险起见迁移前先确认 `.local-backup/` 里的备份是新的。

### 第 3 步：验证

```bash
# 数据盘应该出现在 E 盘
ls -la "/e/Projects/DockerData/DockerDesktopWSL"

# Docker 功能正常
docker info | grep -i "docker root dir"
docker images          # 镜像数量应与迁移前一致

# ✅ 实测：kind 集群**无需重建**，Docker Desktop 重启后容器会自动恢复
cd /e/Projects/LLMOps-Engineer
kubectl get pods,svc,ingress,pvc
bash scripts/verify_d4.sh        # 一条命令验完（19 项）
```

> § 实测结果（2026-10-09）：迁移后 kind 集群**原样恢复**（Pod Running、Ingress 在、
> PVC Bound、postgres 数据完好），`verify_d4.sh` **19 项全绿**，零退化。
> 只需 Pod 会 `RESTARTS=1`（重启导致，正常）。

### 第 4 步：确认 C 盘无残留（通常已自动清理）

⚠️ **先确认新位置一切正常，再动 C 盘。**

```bash
# ① 检查 C 盘是否还有残留（先查再删）
find "/c/Users/lile2/AppData/Local/Docker" -name "*.vhdx" 2>/dev/null
du -sh "/c/Users/lile2/AppData/Local/Docker"
```

> § 实测（2026-10-09）：**Docker Desktop 已自动清理，无需手工删**。
> C 盘 `AppData\Local\Docker` 从 **12 GB → 9.1 MB**，`wsl\disk\` 目录为空。
>
> 若确实有残留（早期版本会出现），再手工处理（**Docker Desktop 需已完全退出**）：
> ```bash
> # 保守做法：先改名观察，确认没问题再彻底删
> mv "C:/Users/lile2/AppData/Local/Docker/wsl/disk" "C:/Users/lile2/AppData/Local/Docker/wsl/disk.bak"
> ```

## 如果不想迁移 —— 备选方案

### 备选 A：只压缩 vhdx（临时缓解）

在 Docker Desktop → Settings → Resources → Advanced 里，或退出 Docker 后：

```powershell
# ⚠️ 需要 Hyper-V 模块；且 Docker Desktop 必须完全退出
Optimize-VHD -Path "C:\Users\lile2\AppData\Local\Docker\wsl\disk\docker_data.vhdx" -Mode Full
```

> 实测本机 PowerShell 受限，`Add-Type` 被拦，这条**可能跑不通**。
> 而且只能回收「已删除但未释放」的块，上限就是 `docker system df` 里的 RECLAIMABLE。

### 备选 B：让 kind 少占空间

```bash
# 不用时删掉整个集群（节点镜像 1.34GB + 所有负载都释放）
kind delete cluster --name llmops
```

需要时 `scripts/verify_d3.sh` 一条命令重建。

## 迁移后的目录布局

```
E:\Projects\
├── DockerData\           # ⭐ Docker 数据盘（11+ GB）—— 全局共享，不属于某个项目
│   └── wsl\disk\docker_data.vhdx
├── AIModel\              # 本地 AI 模型（同级的独立数据目录）
├── FileStorages\
└── LLMOps-Engineer\      # 本项目：纯源码，618 KB，备份/同步都很轻
    ├── .local-backup\    # 本地备份（已 gitignore）
    ├── app\ infra\ scripts\ ...
    └── .gitignore
```

💡 **为什么这样分**：`E:\Projects\` 下混放两种东西 —— **代码仓库**（该进 Git、
该被频繁备份）和**大数据目录**（不该进 Git、体积大、只在本地）。
`DockerData` / `AIModel` / `FileStorages` 属于后者，与代码仓库平级而不是嵌套。
