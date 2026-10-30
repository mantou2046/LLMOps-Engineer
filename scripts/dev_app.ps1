# =============================================================================
# 本地起 app —— PowerShell 原生版（不经 Docker / K8s）
# -----------------------------------------------------------------------------
# 为什么要有这个文件（2026-10-30）：原来只有 dev_app.sh，而本机 PowerShell 里敲
# `bash` 进的很可能是 **WSL**，不是 Git Bash。WSL 里 `C:/...`、`/c/...`、`D:/...`
# 这些 Windows 路径**全部不存在**，表现为一串「未找到解释器 / .env: $'\r'」报错。
# PowerShell 原生版彻底绕开这个岔路口：不经过 bash，也不关心 bash 是哪个。
#
# 做三件事：
#   1. 加载 .env（复用 load_env.ps1，它顺带设 PYTHONUTF8=1）
#   2. 找一个**能真正跑起来**的解释器：逐个候选验证能 import 依赖，
#      横幅第一行打印实际用的那个 —— 排障先看这一行
#   3. 清掉代理后进 app/ 起 uvicorn（main.py 用平级导入，必须在 app/ 下）
#
# 用法（在仓库根，或任何目录都行 —— 路径基于脚本位置解析）：
#   .\scripts\dev_app.ps1
#   .\scripts\dev_app.ps1 -Port 8010          # 换端口
#   .\scripts\dev_app.ps1 -Mock               # 走本地 mock 上游，不花上游的钱
#   .\scripts\dev_app.ps1 -PyOverride D:\python314\python.exe
#
# ⚠️ 若被执行策略拦住（"无法加载...因为在此系统上禁止运行脚本"）：
#   powershell -ExecutionPolicy Bypass -File scripts\dev_app.ps1
# =============================================================================

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$HostAddr = "127.0.0.1",
    [string]$PyOverride = "",
    [switch]$Mock,
    [switch]$LoadEndpoint
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# 让 Python 的 UTF-8 日志在控制台正常显示。
# 本机控制台默认代码页是 936(GBK)，而 load_env.ps1 会把 Python 设成 UTF-8 输出 ——
# 两边不一致时，应用日志里的中文会显示成「涓婃父妯″紡」这种乱码。
# 这是**显示**问题不是业务问题，但排障时会让人以为日志坏了。
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch { }

if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
    Write-Host "❌ 缺少 $RepoRoot\.env —— 先从 .env.example 复制并填好 LLM_* / LANGFUSE_*" -ForegroundColor Red
    exit 1
}

# ---- 1) 加载 .env ------------------------------------------------------------
. (Join-Path $PSScriptRoot "load_env.ps1")

if ($Mock) {
    # 演示用：不连真实上游，也不往 Langfuse 发 trace。
    # 必须放在 load_env 之后 —— 那个脚本会用 .env 的值覆盖会话变量。
    $env:LLM_MODE = "mock"
    $env:OBSERVABILITY = "off"
}

if ($LoadEndpoint) {
    # 打开 /work CPU 压测端点（W5 D1 的负载发生器需要一个「可控的慢端点」）。
    # ⚠️ 默认关闭是对的：一个「帮忙烧 CPU」的端点在生产里就是现成的 DoS 放大器，
    #    演示/压测时才显式打开（app/main.py 的 ENABLE_LOAD_ENDPOINT 旋钮）。
    $env:ENABLE_LOAD_ENDPOINT = "1"
}

# ---- 2) 选解释器 ------------------------------------------------------------
$needs = "import fastapi, uvicorn, prometheus_client"
$candidates = @()
if ($PyOverride) { $candidates += $PyOverride }
$candidates += @(
    "$env:USERPROFILE\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe",
    "D:\python314\python.exe"
)

$py = $null
foreach ($c in $candidates) {
    if (-not (Test-Path $c -PathType Leaf)) { continue }
    & $c -c $needs 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
    Write-Warning "跳过 $c（文件在，但缺 fastapi / uvicorn / prometheus_client）"
}

if (-not $py) {
    foreach ($name in @("python3", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cmd.Source -c $needs 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $cmd.Source; break }
    }
}

if (-not $py) {
    Write-Host "❌ 找不到可用的 Python（要求能 import fastapi / uvicorn / prometheus_client）" -ForegroundColor Red
    Write-Host "   试过这些："
    foreach ($c in $candidates) { Write-Host "     - $c" }
    Write-Host "     - PATH 里的 python3 / python"
    Write-Host "   两种解法："
    Write-Host "     ① 指定解释器：.\scripts\dev_app.ps1 -PyOverride D:\python314\python.exe"
    Write-Host "     ② 补依赖：    <解释器> -m pip install fastapi uvicorn prometheus_client"
    exit 1
}

# ---- 3) 清代理 + 进 app/ 起服务 --------------------------------------------
# ⚠️ 本机代理会把连 127.0.0.1 的请求也转出去；设空字符串不管用，必须删变量。
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = $env:NO_PROXY
Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
Remove-Item Env:https_proxy -ErrorAction SilentlyContinue

Set-Location (Join-Path $RepoRoot "app")

Write-Host "──────────────────────────────────────────────"
Write-Host (" 解释器              = {0}" -f $py)
Write-Host (" LLM_MODE            = {0}" -f $env:LLM_MODE)
Write-Host (" LLM_PRIMARY_MODEL   = {0}" -f $env:LLM_PRIMARY_MODEL)
Write-Host (" LLM_PRIMARY_BASE_URL= {0}" -f $env:LLM_PRIMARY_BASE_URL)
Write-Host (" OBSERVABILITY       = {0}" -f $env:OBSERVABILITY)
Write-Host (" 监听                = http://{0}:{1}" -f $HostAddr, $Port)
Write-Host (" 交互文档            = http://{0}:{1}/docs" -f $HostAddr, $Port)
Write-Host (" 指标端点            = http://{0}:{1}/metrics" -f $HostAddr, $Port)
Write-Host "──────────────────────────────────────────────"
Write-Host "（Ctrl+C 停止；这个窗口会被应用占住，查询请另开一个窗口）" -ForegroundColor DarkGray

& $py -m uvicorn main:app --host $HostAddr --port $Port
exit $LASTEXITCODE
