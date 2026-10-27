# =============================================================================
# 把仓库根的 .env 加载进当前 PowerShell 会话
# -----------------------------------------------------------------------------
# 为什么需要它：LiteLLM CLI **不会**自动读 .env，config.yaml 里的
#   os.environ/XXX 只认「进程环境变量」。所以起网关前必须先 dot-source 本脚本。
#
# 用法（注意前面有个点 + 空格，这是 dot-source，变量才会留在当前会话）：
#   . .\scripts\load_env.ps1
#
# 只加载值非空的行；空值跳过（否则会把已有的值覆盖成空串）。
# =============================================================================

$repoRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $repoRoot ".env"

if (-not (Test-Path $envPath)) {
    Write-Error "找不到 $envPath"
    return
}

$loaded = 0
Get-Content $envPath -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $k = $Matches[1]
        $v = $Matches[2].Trim()
        if ($v -ne '') {
            Set-Item -Path "Env:$k" -Value $v
            $loaded++
        }
    }
}

Write-Host "已从 .env 加载 $loaded 个非空变量到当前会话"
