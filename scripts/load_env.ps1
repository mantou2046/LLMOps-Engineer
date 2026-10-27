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

# ---------------------------------------------------------------------------
# ⚠️ 必须开 Python UTF-8 模式（2026-10-27 · W4 D5 实测）
# LiteLLM 读 config.yaml 用的是 `yaml.safe_load(open(path))` —— **没指定 encoding**，
# 于是走系统默认编码。中文 Windows 下那是 cp936(GBK)，而 config.yaml 含中文注释（UTF-8）
# → UnicodeDecodeError: 'gbk' codec can't decode byte 0x80 in position 107
# 实测：utf8_mode=0 / preferred=cp936 必崩；设 PYTHONUTF8=1（utf8_mode=1）即正常。
# 这一行顺带也让其它 Python 工具不再被 GBK 咬（本项目 .md/.json 全是 UTF-8）。
$env:PYTHONUTF8 = "1"
# ---------------------------------------------------------------------------

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
