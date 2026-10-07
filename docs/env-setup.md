# 本机环境与已知坑

> 最后实测：**2026-10-07 10:48**
> ⚠️ 本页结论**带实测时间戳**。代理端口会变、镜像源会挂，**用之前先重跑一遍验证命令**，不要照抄。

---

## 1. 代理

本机有全局代理，**端口会变**，必须现查：

```bash
env | grep -i proxy
```

| 来源 | 2026-10-07 实测值 | 说明 |
| --- | --- | --- |
| 环境变量 `http_proxy` / `https_proxy` | `http://127.0.0.1:14395` | **Python `urllib.getproxies()` 优先取这个** |
| 注册表 `HKCU\...\Internet Settings\ProxyServer` | `127.0.0.1:9527` | 计划任务等不继承会话 env，走这个 |

两者不一致是**正常现象**，别以为是配错了。

---

## 2. pip 源（⚠️ 旧结论已失效，必须实测）

**历史结论「官方源经代理必超时 → 加清华源」在 2026-10-07 实测中已被推翻。**

实测结果：

| 源 | 结果 |
| --- | --- |
| PyPI 官方（经代理） | ✅ **可正常下载** |
| 清华 tuna | ❌ pip 报 `from versions: none` |
| 阿里云 | ✅ 可正常下载 |
| 腾讯云 | ✅ 可正常下载 |

清华源的失败原因：**返回的索引页疑似被截断**（只列到 `six-1.9.0.tar.gz`，而阿里云同一页面有 `six-1.17.0.whl`；两者 `Content-Length` 都约 11.5 KB、`Last-Modified` 同为 2024-12-04）。

**验证命令**（PowerShell；**`--no-cache-dir` 必加**，否则会命中本地缓存、结论失真）：

```powershell
# 官方源（默认）
python -m pip download --no-deps --no-cache-dir -d $env:TEMP\piptest six
# 换源（改 --index-url）
python -m pip download --no-deps --no-cache-dir -d $env:TEMP\piptest -i <url> six
```

**当前结论**：默认直接用官方源；若超时，优先换**阿里云**或**腾讯云**，别默认清华。

**复核记录**

| 日期 | 环境 | 结果 |
| --- | --- | --- |
| 2026-10-07 11:05 | 用户真实终端 | ✅ 官方源成功（six-1.17.0）⚠️ 但日志显示 `Using cached`，**命中本地缓存、未真正走网络** |
| 2026-10-07 10:48 | 沙箱 | 官方 ✅ / 阿里云 ✅ / 腾讯云 ✅ / 清华 ❌ |

> ⚠️ **仍未定论两点**：① 官方源在**冷缓存**下是否真能下载；② 清华源在真实终端是否也失败。
> 两个镜像的索引页 `Content-Length` 都约 11.5 KB 且 `Last-Modified` 同为 2024-12-04，**疑似响应被截断**，沙箱因素未排除。
> 上面那条 `--no-cache-dir` 命令可一次定论。

```bash
# 阿里云
pip install <pkg> -i https://mirrors.aliyun.com/pypi/simple
# 腾讯云
pip install <pkg> -i https://mirrors.cloud.tencent.com/pypi/simple
```

---

## 3. HuggingFace 镜像（✅ 2026-10-07 已配置）

`HF_ENDPOINT` **已写入用户级环境变量**（注册表 `HKCU\Environment` 已核实）：

```
HF_ENDPOINT = https://hf-mirror.com
```

⚠️ **当前已开的终端不会继承** —— `setx` 只对新进程生效，**新开一个终端**再用。

连通性实测：`https://hf-mirror.com` 返回 **307**（正常跳转），绕代理与走代理均可。

如需重设或撤销：

```bash
setx HF_ENDPOINT "https://hf-mirror.com"     # 设置
reg delete HKCU\Environment /v HF_ENDPOINT /f  # 撤销（reg.exe 在本会话可能被拦）
```

已知坑：
- 用 `urllib` 直连 hf-mirror 会 **403**，要加 `--noproxy '*'` 绕开代理
- `snapshot_download` 可能被钩子打断 → 改用 `curl` 逐文件取
- Windows 下 `huggingface_hub` 建符号链接常失败，`snapshots/` 会留 **0 字节空文件**（`blobs/` 完整、`du` 看不出）→ 加载报 `File model.bin is incomplete`。**别重下**，用硬链接修复

---

## 4. 本机算力现实

- **无 GPU**（CPU-only）。Intel Core Ultra 7 155H / Intel AI Boost NPU（**非 Copilot+**）
- 22 逻辑核 / 31.5 GB 内存，**实际可用约 18 GB**
- 影响：**vLLM 与微调在本机只能小规模跑或根本跑不动**
  → 用「托管 API 为主 + 本地小模型（0.5B–1.5B 量化）演示流程」的组合
  → 微调租云 GPU 按小时算，比本机折腾便宜

---

## 5. Windows 专项

- 容器内换行符：`.gitattributes` 已设 `* text=auto eol=lf`，避免 BOM / CRLF 污染镜像
- 会话是**普通用户**，`sc.exe` / `reg.exe` / `tasklist.exe` / `schtasks.exe` 会被拦
- PowerShell 工具不返回 stdout → 用 Bash + Python `winreg`
