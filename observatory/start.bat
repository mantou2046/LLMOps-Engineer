@echo off
REM ============================================================================
REM  K8s 观测台 · 一键启动
REM  ---------------------------------------------------------------------------
REM  双击即可。会做三件事：
REM    1. 检查 kind 集群是否在运行
REM    2. 起本地观测台服务（后台窗口）
REM    3. 打开浏览器
REM
REM  [!] 本文件必须存为 **ANSI(GBK)** 编码 + CRLF 换行。
REM     存成 UTF-8 中文会乱码（cmd.exe 控制台代码页是 936）。
REM ============================================================================

setlocal
cd /d "%~dp0.."

echo.
echo   K8s 观测台
echo   ================================
echo.

REM --- 1. 检查集群 ---
echo   [1/3] 检查 kind 集群...
kubectl config current-context 2>nul | findstr /B "kind-" >nul
if errorlevel 1 (
    echo.
    echo   [!] 当前 kubectl 没有指向 kind 集群。
    echo       先运行: kind get clusters
    echo       如果集群没起，参考 README 的 W1 D3 章节。
    echo.
    pause
    exit /b 1
)
echo         集群 OK

REM --- 2. 检查 python ---
echo   [2/3] 查找 Python...
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" (
    if exist "C:\Users\lile2\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" (
        set PY=C:\Users\lile2\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe
    )
)
if "%PY%"=="" (
    echo.
    echo   [!] 找不到 python。请先装 Python 3.10+ 并加入 PATH。
    echo.
    pause
    exit /b 1
)
echo         使用: %PY%

REM --- 3. 启动服务并打开浏览器 ---
echo   [3/3] 启动观测台...
start "K8s 观测台服务" cmd /k "%PY%" observatory\server.py --port 8899

REM 等服务就绪再开浏览器（否则会看到「无法连接」）
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8899

echo.
echo   已启动。浏览器应该自动打开了。
echo   地址: http://127.0.0.1:8899
echo.
echo   关闭那个标题为「K8s 观测台服务」的窗口即可停止。
echo.
pause
