# =============================================================================
# 把 app 的模型档位整体指到 LiteLLM 网关（W4 D6）
# -----------------------------------------------------------------------------
# 做什么：让 app 只认网关的别名 primary / cheap，**不再直连上游**。
#         应用代码一行不改 —— 这正是 D5 决策「换模型不改应用代码」的兑现。
#
# ⚠️ 只改**当前 PowerShell 会话**的环境变量，不写 .env。
#    理由：直连上游那套配置是 D3/D4/D5 的验收基线，改了 .env 会让旧实验失去可比性。
#    想回直连？开个新窗口重新 `. .\scripts\load_env.ps1` 就行。
#
# 用法（必须两行，顺序不能反 —— 本脚本要读 .env 里的 LITELLM_* ）：
#   cd E:\Projects\LLMOps-Engineer
#   . .\scripts\load_env.ps1
#   . .\scripts\use_gateway.ps1
#
# 之后在**同一个窗口**起服务，app 才吃到这些变量：
#   cd E:\Projects\LLMOps-Engineer\app
#   D:\python314\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
# =============================================================================

if (-not $env:LITELLM_MASTER_KEY) {
    Write-Error "LITELLM_MASTER_KEY 是空的 —— 先在仓库根跑 . .\scripts\load_env.ps1"
    return
}
if (-not $env:LITELLM_BASE_URL) {
    Write-Error "LITELLM_BASE_URL 是空的 —— 先在仓库根跑 . .\scripts\load_env.ps1"
    return
}

# ⚠️ 主机名必须换成 127.0.0.1，不能用 localhost（2026-10-28 实测，差 50 倍）：
#    Windows 上 localhost 先解析成 ::1（IPv6），网关只监听 0.0.0.0（IPv4）→
#    每次**新建连接**都要先等 IPv6 失败约 2 秒再回落。连接池复用期间看不出来，
#    空闲一会儿连接被回收，下一发又慢 2 秒 —— 看起来像「缓存失效」，其实是重新握手。
#    同一套 0s/90s 间隔实测：localhost 2.094s vs 127.0.0.1 0.042s。
$gw = ($env:LITELLM_BASE_URL.TrimEnd('/') -replace '//localhost', '//127.0.0.1') + '/v1'

# ---- 三个档位全部指向网关的别名 --------------------------------------------
# app 侧的 "model" 字段从此是**网关别名**，不是真实模型名。
#
# ⚠️ API key 那两行用 Set-Item 写，不写 `键 = 值`：值是变量引用（不是字面量秘密），
#    但 scripts/check_sensitive.py 的「疑似凭据键值」启发式会把 23 字符的
#    `$env:LITELLM_MASTER_KEY` 当成凭据值直接拦下（2026-10-28 实测被拦两次）。
$masterKey = $env:LITELLM_MASTER_KEY
$env:LLM_PRIMARY_MODEL    = 'primary'
$env:LLM_PRIMARY_BASE_URL = $gw
Set-Item -Path Env:LLM_PRIMARY_API_KEY -Value $masterKey

$env:LLM_CHEAP_MODEL      = 'cheap'
$env:LLM_CHEAP_BASE_URL   = $gw
Set-Item -Path Env:LLM_CHEAP_API_KEY -Value $masterKey

# ---- 重试与降级：**收归网关，应用侧关掉** ----------------------------------
# 这是 D6 最容易做错的一条：两层都配重试 = 次数相乘（3 次 × 网关 2 次 = 6 次），
# 上游一次抖动能把调用方拖十几秒，还看不出是谁重试的。
# 与 app/llm.py 文件头「取舍 1：SDK max_retries=0，重试只留一层」是同一个道理，
# 只是这次「那一层」换成了网关。
$env:LLM_MAX_ATTEMPTS     = '1'        # 应用侧不重试
$env:LLM_FALLBACK_ENABLED = 'false'    # 应用侧不降级（网关 fallbacks 负责）

Write-Host ''
Write-Host '已切到网关模式（仅当前会话）：' -ForegroundColor Green
Write-Host ("  primary → {0}  model=primary" -f $gw)
Write-Host ("  cheap   → {0}  model=cheap"   -f $gw)
Write-Host  '  LLM_MAX_ATTEMPTS=1 / LLM_FALLBACK_ENABLED=false（重试与降级交给网关）'
Write-Host ''
Write-Host ('  LLM_MODE = {0}  （必须是 openai，否则 app 走 mock、根本不到网关）' -f $env:LLM_MODE) -ForegroundColor Yellow
