# Docker Desktop 安装手册（本机 · Windows 11 26H2）

> 生成时间：**2026-10-08** ｜ 最近更新：**2026-10-08 10:48**
> 本机实测：Windows 11 26H2 / build **26300**.9457 / Pro / AMD64
> ⚠️ 本页结论**带实测时间戳**。代理端口会变，**动手前先重跑探活命令**。

---

## 🚦 进度看板

| 步骤 | 状态 | 时间 |
| --- | --- | --- |
| Step 1 · 装 WSL2 | ✅ **已完成** | 2026-10-08 10:47（已重启） |
| Step 2 · 下载安装包 | ✅ **已完成** | `D:\Software\开发\Docker Desktop Installer.exe`（606.1 MB，与官方字节数一致） |
| Step 3 · 安装 | ✅ **已完成** | 2026-10-08 10:52 · Docker Desktop **4.94.0**（装在用户目录，非 Program Files） |
| Step 4a · WSL 集成 | ✅ **已验证生效** | `IntegratedWslDistros=['Ubuntu']`；`/mnt/wsl/docker-desktop` 挂载点存在 |
| Step 4b · 镜像加速 | ⏳ **进行中** | 实测可用源见 Step 4b（**原来给的 3 个里有 2 个是死的**） |
| Step 4c · 资源限制 | ⏳ 待办 | 引擎已分配到 **16.4 GB**（= WSL2 默认吃一半物理内存） |
| Step 5 · 跑 D2 验收 | ⏳ 待办 | `bash scripts/verify_d2.sh` |

**引擎实测**（2026-10-08 11:05，daemon 已连通）：
- Client **29.8.2** / Server Docker Desktop **4.94.0** / Engine **29.8.2**（API 1.56）
- Server OS/Arch：`linux/amd64`，containerd `v2.3.6`
- **22 CPU / 16,479,584,256 B（≈16.4 GB）内存** ← WSL2 默认配额，见 Step 4c
- 未登录（无 auth 相关配置键）；条款已接受（`LicenseTermsVersion=2`）
- ✅ **直连 Docker Hub 实测可拉**（`docker pull alpine:latest` 成功）
- ⚠️ 用完整路径调用 `docker.exe` 时，CLI 会去 **PATH** 找 `docker-credential-desktop`，
  找不到就报 `error getting credentials` → **把 `...\DockerDesktop\resources\bin` 加进 PATH** 即可。
  （用户的用户级 PATH **已包含**该目录，新开终端直接用 `docker` 没问题。）


**WSL 实测状态**（2026-10-08 10:47 注册表核实）：
- `WslService` 存在且 **Start=2（自动）** —— 新版 WSL 服务名，取代了旧的 `LxssManager`
- **Ubuntu 已注册，Version=2（WSL2）**，`DefaultUid=1000`（普通用户，非 root）
- `\\wsl$\Ubuntu` 文件系统可正常访问
- 尚未配置 `.wslconfig`（走默认 NAT 网络）

---

## 0. 先看清本机现状（已实测确认）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| OS | ✅ Windows 11 26H2 (build 26300) | **注意**：注册表 `ProductName` 仍写「Windows 10」，那是微软遗留值，**判据是 build ≥ 22000** |
| CPU 虚拟化 | ✅ 齐全 | `systeminfo` 报「基本的虚拟化支持 / APIC 虚拟化」 |
| Hyper-V 平台 | ✅ **已启用** | `systeminfo` 报「已检测到虚拟机监控程序」 |
| `vmcompute` / `HvHost` | ✅ 存在（手动启动，正常） | 容器/VM 计算服务 |
| WSL | ✅ **已装** | `WslService` 自动启动；Ubuntu (WSL2) 已注册 |
| Docker Desktop | ❌ **未装** | 无 `C:\Program Files\Docker`，PATH 里无 docker |
| podman / rancher / nerdctl | ❌ 均无 | 已全盘扫过 |

**结论**：虚拟化底座 + WSL2 均已就绪，**只差装 Docker Desktop 本体**。

---

## 1. 为什么必须你手动做

以下三步**都需要管理员权限**，当前 AI 会话是普通用户，无法代劳：

| 步骤 | 为什么需要提权 | 状态 |
| --- | --- | --- |
| `wsl --install` | 要开 Windows 可选特性（Microsoft-Windows-Subsystem-Linux / VirtualMachinePlatform） | ✅ 你已完成 |
| 安装 Docker Desktop | 安装器要写 `Program Files`、注册系统服务、加防火墙规则 | ⏳ 进行中 |
| 重启电脑 | 特性生效必须重启 | ✅ 已完成一次 |

⚠️ 另外：本会话的沙箱**明确拦截 `wsl.exe`**（Program Blacklist），我连查状态都做不到（上面 WSL 状态是**读注册表**得来的）。
如果你以后想让 AI 能帮你操作 WSL，需在 **安全中心 → 命令安全 → 程序黑名单** 里移除 `wsl.exe`。

---

## 2. 安装步骤（预计 60–80 分钟，其中下载占大头）

### Step 1 · 装 WSL2 ✅ 已完成

**以管理员身份**打开 PowerShell（Win + X → 「终端(管理员)」或「Windows PowerShell(管理员)」）：

```powershell
wsl --install
```

Win11 上这一条命令会自动完成三件事：
1. 启用 `Microsoft-Windows-Subsystem-Linux` 与 `VirtualMachinePlatform` 特性
2. 下载安装 WSL2 内核
3. 安装默认发行版（Ubuntu）

**装完必须重启电脑。**

重启后验证：

```powershell
wsl --status
wsl -l -v          # 应看到 Ubuntu，VERSION 为 2
```

> 若 `wsl --install` 报错说不认识该参数，说明系统太旧 —— 但你 build 26300 远高于要求（Win10 2004+ / Win11 全支持），不会遇到。

### Step 2 · 下载 Docker Desktop ✅ 已完成

官方下载地址（**本机实测走代理 9527 可下，2.1MB/10s ≈ 215KB/s**）：

```
https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe
```

**已下载**：`D:\Software\开发\Docker Desktop Installer.exe`
**完整性核对**（2026-10-08 10:48）：

| 项 | 值 |
| --- | --- |
| 实际大小 | 635,493,296 字节（606.1 MB） |
| 官方大小 | 635,493,296 字节（606.1 MB） |
| 差异 | **+0 字节 → ✅ 完全一致** |
| 文件头 | `MZ` → ✅ 合法 Windows 可执行 |

<details>
<summary>（保留）原下载命令，供以后重下参考</summary>

**方式 A · 浏览器直接点（最简单）**

在浏览器里打开上面的链接。如果你的浏览器已配代理，直接下即可。

**方式 B · 命令行下（可断点续传，推荐 —— 中途断了不用重来）**

在普通 PowerShell 里跑：

```powershell
# 探活代理（确认 9527 还活着）
curl.exe -x http://127.0.0.1:9527 --max-time 8 -sS -o NUL -w "%{http_code}\n" https://desktop.docker.com

# 下载（-C - 断点续传；断了就重跑同一条命令，会接着下）
cd $env:USERPROFILE\Downloads
curl.exe -x http://127.0.0.1:9527 -L -C - --retry 5 --retry-delay 3 `
  -o "DockerDesktopInstaller.exe" `
  "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
```

> ⚠️ **代理端口可能变**。若 9527 不通，先查当前端口：
> ```powershell
> Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' | Select ProxyServer,ProxyEnable
> ```
> 拿到的端口替换上面命令里的 9527。
>
> 💡 **速度慢怎么办**：215 KB/s 是实测值，600MB 约需 45–55 分钟。可以挂着下，期间去做别的。
> 国内镜像站（阿里云 / 清华）**实测没有 Docker Desktop 安装包**，只有 2017–2021 年的 docker-ce 老二进制，别浪费时间找。

</details>

### Step 3 · 安装 ⏳ 进行中

双击 `D:\Software\开发\Docker Desktop Installer.exe`。

**安装选项注意**（⚠️ 这是最容易选错的一步）：

| 选项 | 建议 | 原因 |
| --- | --- | --- |
| Use WSL 2 instead of Hyper-V | ✅ **保持勾选**（默认） | WSL2 后端启动快、资源占用低 |
| Add shortcut to desktop | 随意 | — |
| **Install required Windows components for WSL 2** | ❌ **不要勾** | **WSL2 你已装好**，重复勾会再触发一次特性操作、甚至要求再重启一次 |

安装完**它可能要求你重启**，照做（若提示）。

> 💡 **安装过程中如果卡住**：Docker Desktop 安装器会在最后阶段拉取 WSL2 内核组件，这步要走网络。
> 若卡在 "Installing..." 超过 5 分钟，检查代理是否还活着（见 Step 2 的探活命令）。

### Step 4 · 首次启动与配置

1. 启动 Docker Desktop，接受服务条款
2. 若弹「sign in」—— **可跳过**（Skip / Continue without signing in）
3. 首次启动会拉取 WSL2 内核，等左下角小鲸鱼图标变绿（Engine running）

#### 4a. ⚠️ 开启 WSL 集成（关键，容易漏）

Docker Desktop → **Settings → Resources → WSL Integration**

| 项 | 设置 |
| --- | --- |
| Enable integration with my default WSL distro | ✅ 开 |
| **Ubuntu**（发行版列表里的开关） | ✅ **开** |

**为什么必须做**：不开的话，Windows 侧能用 `docker`，但 **WSL 里用不了**。
而八周计划里 W1 D3 起要进 WSL 里操作 kind/minikube，那时才发现就晚了。

验证：在 WSL 终端里跑 `docker --version`，能出版本号即成功。

#### 4b. 配置国内镜像加速（强烈建议，否则拉镜像奇慢）

**路径**：Docker Desktop → Settings（齿轮）→ **Docker Engine**（左侧菜单）。

页面里是一个 JSON 编辑器，把你现有的配置**改成下面这样**（保留原有键，只加 `registry-mirrors`）：

```json
{
  "builder": {
    "gc": {
      "defaultKeepStorage": "20GB",
      "enabled": true
    }
  },
  "experimental": false,
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://hub.rat.dev"
  ]
}
```

然后点 **Apply & restart**。

> ⚠️ **页面顶部有黄条警告**「This can prevent Docker from starting. Use at your own risk.」——
> 那是**常驻提示**，只要 JSON 合法就不用怕。**写错 JSON（比如少个逗号）才会真的起不来**，届时删掉新增行、点 Apply 即可恢复。

##### 镜像源实测（2026-10-08 11:05 · 走完整 registry 协议：拿 token → 拉 manifest）

| 源 | 结果 |
| --- | --- |
| `https://docker.1ms.run` | ✅ **可用**（manifest 返回 16 个平台条目） |
| `https://docker.m.daocloud.io` | ✅ **可用** |
| `https://hub.rat.dev` | ✅ **可用** |
| `https://docker.xuanyuan.me` | ❌ 无 `WWW-Authenticate` 头（非标准 registry） |
| `https://dockerproxy.net` | ❌ 同上 |

> ⚠️ **别照抄网上的源列表** —— 本手册初版给的三个里就有两个是死的。
> 判据不是「返回 401」：Docker Registry API 的 `/v2/` **本就要求认证，401 = 服务活着**，
> 真正要测的是「能否走完 token → manifest 流程」。
>
> 💡 失效时换源：见 [腾讯云 2026 最新镜像源列表](https://cloud.tencent.com/developer/article/2485043)（每月更新）。
> 另注：本机**直连 Docker Hub 实测也能拉**（`alpine:latest` 成功），镜像源属于**加速**而非必需。


#### 4c. 资源调整（本机内存偏紧，建议做）

**Docker Desktop 侧**：Settings → Resources
- CPUs：给 6–8 核（本机 22 逻辑核）
- Memory：给 4–6 GB（⚠️ 本机 32GB 物理内存，**可用仅约 14.6 GB**，别给太多）

**WSL2 侧**：新建 `C:\Users\lile2\.wslconfig`（本机目前**没有**这个文件，走默认配置）

```ini
[wsl2]
# 不限制的话 WSL2 默认可吃掉一半物理内存（约 16GB），
# 与 Docker Desktop 抢内存，容易触发 OOM
memory=8GB
processors=8
swap=2GB
```

改完在 PowerShell 里 `wsl --shutdown` 再重启 WSL 生效。

> 💡 **本机内存现实**：32,213 MB 物理内存，但**可用只有 14,619 MB**（其他进程占着）。
> 给 WSL 8GB + Docker 4GB 是留了余地的保守值。跑大模型推理时要另行调整。

---

## 3. 验证安装成功

**新开一个** PowerShell（让 PATH 生效），逐条跑：

```powershell
docker --version                    # 应输出 Docker version 2x.x.x
docker compose version              # 应输出 Docker Compose version v2.x.x
docker run --rm hello-world         # 应打印 "Hello from Docker!"
docker info --format "{{.OSType}}"  # 应输出 linux
```

四条都过 = 安装成功。

**再验 WSL 侧**（Step 4a 的成果）：

```bash
wsl -d Ubuntu -- docker --version   # 在 Windows 侧直接验 WSL 里的 docker
# 或进 WSL 后： docker run --rm hello-world
```

> 💡 **Git Bash 里 `docker` 找不到命令**？因为 Docker Desktop 装在 `C:\Program Files\Docker\Docker\resources\bin`，
> 而当前 Git Bash 会话的 PATH 是启动时快照的。**新开一个终端**即可（本手册写就时实测：本机 Git Bash 里 `docker` 不存在，属正常）。
>
> ⚠️ 但注意：**AI 会话的沙箱会拦截 `wsl.exe`**。若想让 AI 帮你跑 WSL 里的命令，需先在
> 安全中心 → 命令安全 → 程序黑名单 里移除 `wsl.exe`。目前 AI 只能读 `\\wsl$\Ubuntu` 文件系统。

---

## 4. 装好后 → 回到 D2 动手项

D2 三条动手项的产出物**已经写好**（见下表），装完 Docker 直接跑验收：

```bash
cd /e/Projects/LLMOps-Engineer

# 0) 准备环境变量
cp .env.example .env
# 编辑 .env，至少填 POSTGRES_PASSWORD（随便给个本地密码）

# 1) 起数据库
docker compose -f infra/compose.yml up -d
docker compose -f infra/compose.yml ps        # 等 postgres 显示 (healthy)

# 2) 一条命令跑完 D2 三条验收
bash scripts/verify_d2.sh
```

| 验收项 | 标准 | 谁来验 |
| --- | --- | --- |
| 多阶段镜像体积 | < 200 MB，且比朴素镜像小 | `verify_d2.sh` 第 1 段 |
| 容器内非 root | `whoami` ≠ root | `verify_d2.sh` 第 2 段 |
| pgvector 可用 | 能写入并检索到向量 | `verify_d2.sh` 第 3 段 |

---

## 5. 踩坑预案

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `wsl --install` 后 wsl 命令仍不可用 | 未重启 | 重启电脑 |
| Docker Desktop 卡在 "Starting the Docker Engine" | WSL2 内核未装好 | 管理员跑 `wsl --update`，再重启 |
| `docker run` 报 `error during connect` | Engine 没起来 / 代理配置错误 | 看右下角鲸鱼图标；检查 Docker Engine JSON 语法 |
| 拉镜像超时 | 没配镜像加速 | 见 Step 4 的 `registry-mirrors` |
| 端口 5432 被占 | 本机已有 Postgres | 改 `.env` 里 `POSTGRES_PORT=5433` |
| Git Bash 里 `docker` 无命令 | PATH 快照 | **新开终端**；或用完整路径 `"/c/Program Files/Docker/Docker/resources/bin/docker.exe"` |

---

## 6. 装完记得回报

装好后把下面三条的输出贴给我，我帮你核对并继续 D2：

```powershell
docker --version
docker compose version
docker info --format "{{.OSType}} / {{.ServerVersion}}"
```
