# 本机环境与已知坑

> 最后实测：**2026-10-07 17:51**
> ⚠️ 本页结论**带实测时间戳**。代理端口会变、镜像源会挂，**用之前先重跑一遍验证命令**，不要照抄。

---

## 1. 代理

本机有全局代理，**端口会变**，必须现查：

```bash
env | grep -i proxy
```

| 来源 | 说明 |
| --- | --- |
| 环境变量 `http_proxy` / `https_proxy` | **Python `urllib.getproxies()` 优先取这个** |
| 注册表 `HKCU\...\Internet Settings\ProxyServer` | 计划任务等不继承会话 env，走这个 |

⚠️ **同一天测两次就不一样** —— 这就是为什么必须现查，不能把端口写死进脚本：

| 实测时刻 | env 端口 | 注册表端口 | 代理是否可用 |
| --- | --- | --- | --- |
| 2026-10-07 10:48 | 14395 | 9527 | ✅ 通 |
| 2026-10-07 13:37 | **8016** | 9527 | ❌ **死端口**（连接超时） |
| 2026-10-07 17:51 | **3315** | 9527 | ❌ **死端口**；**注册表 9527 仍活**（走它打 github.com → 200） |

**⚠️ 结论：env 端口不可信，注册表那个 9527 才是稳的。**

两者不一致是**正常现象**，别以为是配错了。**注册表端口稳定、env 端口每次会话可能不同** —— 脚本里要读端口，读 `getproxies()`（即 env）而不是注册表。

> [!warning] ⚠️ 2026-10-07 13:37 实测：**env 里的代理端口是死的**
> `http_proxy=http://127.0.0.1:8016` 连不通（`curl -x` 5s 超时、`http_code=000`），
> 导致 **pip 挂起不动**（它老老实实去连这个死代理）。
>
> **绕过死代理**（⚠️ 见下方更正）：
> ```bash
> unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
> python -m pip install -r requirements.txt
> ```
> 直连实测：`pypi.org` 200、`mirrors.aliyun.com` 200、`files.pythonhosted.org` 可达。
>
> > [!danger] ⚠️ 2026-10-07 17:40 更正：**`unset` 环境变量并不能真正禁用代理**
> > 实测 `urllib.request.getproxies()` 在**所有代理环境变量都已 unset** 的情况下，**仍然返回**：
> > `{'http': 'http://127.0.0.1:9527', 'https': 'http://127.0.0.1:9527', 'ftp': 'http://127.0.0.1:9527'}`
> > —— 因为 urllib / pip 在环境变量为空时会**回退读 Windows 注册表**
> > （`HKCU\...\Internet Settings\ProxyServer`，本机 = 9527）。
> >
> > **含义**：`unset` 只是把「env 代理」这一层去掉，pip 仍会走**注册表那个代理**。
> > 想**确认**当前到底走不走代理、走哪个端口，跑：
> > ```bash
> > python -c "import urllib.request; print(urllib.request.getproxies())"
> > ```
> > **真正**绕过代理的办法（未全部实测，按可靠性排序）：`curl` 用 `--noproxy '*'`；
> > pip 用 `--proxy ""` 或设置 `no_proxy`；彻底关掉需改注册表（要管理员权限）。
> > ⚠️ **别把「unset 了就直连了」当成结论** —— 先打印 `getproxies()` 确认。
>
> **判据**：pip「卡住不报错」优先怀疑**代理端口死了**，而不是网络不通 —— 先 `curl -x <代理> --max-time 5 https://pypi.org/simple/` 探一下代理本身。
> **教训**：`env` 里有代理变量 ≠ 代理能用。**「配了」和「通」是两回事。**

### 1.1 ⚠️ `git push` 必须走代理，且 env 端口常是死的（2026-10-07 17:51 实测）

**症状**：`git push` 报 `schannel: server closed abruptly (missing close_notify)`，
或 `Failed to connect to github.com:443 after 21154 ms`。

**根因**：
- **`github.com` 直连不通**（`curl --noproxy '*'` → **000 超时**）
- 而 git 默认读 **env** 里的代理端口 —— 今天那个是 **3315，已经死了**
- **注册表里的 `9527` 是活的**（走它打 `github.com` → **200**）

**修法：显式把代理指到注册表端口**

```bash
git -c http.proxy=http://127.0.0.1:9527 -c https.proxy=http://127.0.0.1:9527 push origin main
```

> 💡 想一劳永逸，写进本仓库配置（**只影响这个仓库**）：
> ```bash
> git config http.proxy http://127.0.0.1:9527
> git config https.proxy http://127.0.0.1:9527
> ```
> ⚠️ 但**端口哪天再变就得改**，所以更稳的是「推之前先探活」。

**探活一行**（推之前跑，确认端口活着）：

```bash
curl -x http://127.0.0.1:9527 --max-time 5 -sS -o /dev/null -w "%{http_code}\n" https://github.com
```

**⚠️ 一个反直觉点**：`api.github.com` **直连可达**（200），但 `github.com` **直连不通**。
所以「`gh` 命令能用」**不代表**「`git push` 能用」—— 两者走的是不同域名。

---

## 2. pip 源（⚠️ 旧结论已失效，必须实测）

**历史结论「官方源经代理必超时 → 加清华源」在 2026-10-07 实测中已被推翻。**

实测结果：

> [!warning] ⚠️ 2026-10-07 17:40 复核：**上一条「清华源不可用」的结论是错的**
> 真因是**清华源 WAF 拦截了 pip 在 Python 3.13.14 下发出的 User-Agent** —— **与镜像本身无关**。
> 同一台机器、同一时刻、同一个网络，**换个 Python 版本结论就反了**：
>
> | 解释器 | Python | 清华源 |
> | --- | --- | --- |
> | 沙箱托管 Python | **3.13.14** | ❌ `No matching distribution found for six` |
> | 系统 Python `D:\python314` | **3.14.8** | ✅ 正常（列出全部版本） |
>
> **触发条件（二分实测，已精确到子串）**：UA 里含 **`"version":"3.13.14"`** → 403。
> 换成 `3.13.13` / `3.13.15` / `3.13.0` / `3.14.0` / `3.12.0`，或删掉该片段 → 200。
> ⚠️ **只拦清华** —— 同一条 UA 打官方 / 阿里云 / 腾讯云 / 中科大**全部 200**。
> 403 响应体是清华自己的中文拦截页（`Server: nginx/1.22.1`，「抱歉，您目前无法访问此页面」）。
>
> **已逐一排除**：代理（走 / 不走代理都 403）、请求头（`Accept` / `Accept-Encoding` / `Cache-Control` 逐组测过）、
> `pip.ini`（不存在）、TLS 中间人（证书是真的 Let's Encrypt）、限流（拦截与放行在测试中交错出现）。
>
> ⚠️ **为什么会有这条规则：无法从外部确知**（最可能是该 UA 曾被标记过，属**临时**规则）。
> → 所以**别把「清华源不能用」写死**，结论必须带**版本号 + 时间戳**。

**实测结果（2026-10-07 17:40，托管 Python 3.13.14 / 系统 Python 3.14.8 双跑）**

| 源 | 3.13.14 | 3.14.8 |
| --- | --- | --- |
| PyPI 官方 | ✅ 1.17.0 | ✅ 1.17.0 |
| 清华 tuna | ❌ **403**（WAF 拦 UA） | ✅ 1.17.0 |
| 阿里云 | ✅ 1.17.0 | ✅ 1.17.0 |
| 腾讯云 | ✅ 1.17.0 | ✅ 1.17.0 |
| 中科大 ustc | ✅ 1.17.0 | ✅ 1.17.0 |

> ⚠️ **旧结论「清华源返回的索引页疑似被截断」是误判**：curl 拿到的页面**结构完整**（48 个链接、含 `six-1.17.0` 的 wheel 与 sdist、有 `</html>` 与清华的 `<!--SERIAL-->` 页脚）。
> pip 当时**根本没拿到页面** —— 它在 HTTP 层就被 403 挡了。**「页面看起来不完整」和「客户端没拿到页面」是两回事，别混。**


**怎么测（推荐：一条命令跑完所有源）**

```bash
# ⚠️ 体检脚本在本机 YYDS 库里，**不在本仓库内**（本页整体都是「这台机器」的环境记录，天然不可移植）
python "E:/Projects/Obsidian/YYDS/.workbuddy-ai/scripts/check_pip_sources.py"
python "E:/Projects/Obsidian/YYDS/.workbuddy-ai/scripts/check_pip_sources.py" --ua   # 顺带打印 pip 的 UA（这次就是靠它抓到根因）
```

脚本对 5 个源逐个跑 `pip index versions`（**冷缓存**），输出「看到的最高版本」——
**列不出最新版 = 该源对你不生效**。

**手动单源验证**（**`--no-cache-dir` 必加**，否则会命中本地缓存、结论失真）：

```bash
# 官方源（默认）
python -m pip download --no-deps --no-cache-dir -d /tmp/piptest six
# 换源（改 -i）
python -m pip download --no-deps --no-cache-dir -d /tmp/piptest -i <url> six
```

**当前结论**：**五个源都能用**，默认直接用官方源即可；某源报错时先跑一遍 `check_pip_sources.py`，
并**记下当前的 Python 版本**—— 这次的坑正是「换 Python 版本结论就反」。

**复核记录**

| 日期 | 环境 | 结果 |
| --- | --- | --- |
| 2026-10-07 13:53 | 沙箱（**unset 代理**） | ✅ **`pip install --dry-run --no-cache-dir -r requirements.txt` 成功**，冷缓存解析 29 个包，`EXIT=0`。落地版本：`fastapi 0.142.2` / `uvicorn 0.54.0` / `pydantic 2.13.5` / `pydantic-settings 2.15.0` / `httpx 0.28.1` / `openai 1.109.1` / `python-dotenv 1.2.4` |
| 2026-10-07 13:37 | 沙箱（走代理） | ⚠️ **env 代理端口 8016 是死的** → pip 挂起不动；**`unset` 代理后直连全通**（pypi / 阿里云均 200，单次请求 10–14s 偏慢） |
| 2026-10-07 11:05 | 用户真实终端 | ✅ 官方源成功（six-1.17.0）⚠️ 但日志显示 `Using cached`，**命中本地缓存、未真正走网络** |
| 2026-10-07 10:48 | 沙箱 | 官方 ✅ / 阿里云 ✅ / 腾讯云 ✅ / 清华 ❌ |

> ⚠️ **沙箱的网络时通时不通**（10:48 代理通、13:37 代理死但直连通）→ **凡涉及下载的结论，一律以用户真实终端为准**，沙箱结果只作参考。
> ⚠️ **pip 变慢的原因**：本机 pip 的**默认超时 15s**，而直连单次请求要 10–14s —— 余量极小，偶发重试就会 `ReadTimeout`。**建议在 `pip.ini` 里调大**：`[global]` 段加 `timeout = 120`。

> ✅ **已定论（2026-10-07 17:40）**
> ① **官方源在冷缓存下确实能下载** —— 13:53 `pip install --dry-run --no-cache-dir -r requirements.txt`（unset 代理后）成功解析全部 29 个包，`EXIT=0`。
> ② **清华源在真实解释器下也能用** —— 系统 Python **3.14.8** 跑 `check_pip_sources.py` 得 **5/5 全通**；失败只出现在 **3.13.14**，且是 WAF 拦 UA，与镜像无关。**「仍未定论」项已关闭。**

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
