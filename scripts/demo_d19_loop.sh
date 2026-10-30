#!/usr/bin/env bash
# =============================================================================
# W3 D19 · 闭环演示脚本 —— 一条链跑通
#   改 prompt（git）→ 实验评审（MLflow）→ 发布（label 拨动）
#   → 可追溯（Langfuse trace）→ 回滚（label 拨回）
#
# 一句话口径：这条链上**只有「发布」是指令，其余四步都是证据**。
# 所以回滚只要动 label，前面四步自动跟着变。
#
# 用法：
#   bash scripts/demo_d19_loop.sh          # 交互式（每阶段停顿，方便开 UI 看）
#   AUTO=1 bash scripts/demo_d19_loop.sh   # 全自动无停顿（冒烟测试用）
#
# 环境前提（脚本会自动补齐，不用手动起）：
#   · MLflow tracking server 127.0.0.1:5000 —— ⚠️ 必须在【课程仓】起！
#     mlflow.db 与 qa-prompt-eval 实验都在 /e/Projects/Dev/mlops-zoomcamp，
#     在别处起会得到第二个空库，UI 里怎么刷都是空的（D14 口径 2）。
#   · API 服务 127.0.0.1:8000（app/uvicorn，.env 已灌入）
#   · Langfuse 127.0.0.1:3000（外部依赖，只检查不启动）
#
# 安全性：启动时记录 production label 的当前指向；任何退出路径（正常结束 /
#   Ctrl+C / 报错）都会核对指针，不在原值就自动拨回，不会把线上留在 v2。
#
# 耗时约 5–8 分钟：两次 65s 的 prompt 缓存等待 + 22 次真实模型调用
#   （LLM_MODE=openai，小额 API 花费；首包约 29s 是网关冷启动，正常现象）。
# =============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
# GBK 终端（中文 Windows 默认 936 代码页）无法编码 MLflow 结束时打印的 🏃 emoji，
# 会抛 UnicodeEncodeError 把脚本整段中止（2026-10-24 实测踩坑）。
# 开启 Python UTF-8 模式让 stdout/stderr 走 UTF-8，emoji 与中文都不炸。
export PYTHONUTF8=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COURSE_REPO="${COURSE_REPO:-/e/Projects/Dev/mlops-zoomcamp}"
AUTO="${AUTO:-0}"

# ---- Python：托管 venv 优先（依赖齐全），课程 .venv 次之，最后系统 python ----
PY="${PY:-C:/Users/lile2/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe}"
[ -x "$PY" ] || PY="python"
PY_MLFLOW="$PY"
[ -x "$COURSE_REPO/.venv/Scripts/python.exe" ] && PY_MLFLOW="$COURSE_REPO/.venv/Scripts/python.exe"

# ---- 代理：本机代理会截胡 127.0.0.1（表现为 502），全程绕开 ------------------
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
export NO_PROXY='127.0.0.1,localhost' no_proxy='127.0.0.1,localhost'

LOG_DIR="${TMPDIR:-/tmp}/d19_demo"; mkdir -p "$LOG_DIR"

OK="[OK]"; WARN="[!!]"; ERR="[XX]"   # GBK 控制台下 emoji 会乱码，用 ASCII 符号

cd "$REPO_ROOT" || exit 1
[ -f .env ] || { echo "$ERR 缺少 .env（从 .env.example 复制并填好 LLM_* / LANGFUSE_*）"; exit 1; }
# app 代码不自己读 .env，本地必须显式 source。
# ⚠️ 先 tr -d '\r' 剥 CRLF：本机 .env 是 CRLF 存的，直接 source 会满屏
#    `./.env: line N: $'\r': command not found`，且值尾带 \r（"openai\r"）。
ENV_TMP="/tmp/llmops_env.$$"
trap 'rm -f "$ENV_TMP"' EXIT
tr -d '\r' < ./.env > "$ENV_TMP"
set -a; . "$ENV_TMP"; set +a
rm -f "$ENV_TMP"

banner() { echo; echo "======== $* ========"; }
pause() {
  [ "$AUTO" = "1" ] && return 0
  echo
  read -r -p "── 按回车继续（Ctrl+C 可中止，label 会自动拨回原值）……" _
}

# 依赖自检：解释器选错时早失败、说清楚（比等到第 3 步报 ModuleNotFound 强）
"$PY" -c "import uvicorn, fastapi, langfuse, mlflow" 2>/dev/null || {
  echo "$ERR 解释器 $PY 缺少依赖（uvicorn / fastapi / langfuse / mlflow 之一）"
  echo "      换一个：PY=<你的 python 路径> bash scripts/demo_d19_loop.sh"
  exit 1
}

# ---- API 端口：8000 可能被 Windows 保留段圈走（WinError 10013）→ 自动换 -------
# 2026-10-23 实测：`netsh interface ipv4 show excludedportrange protocol=tcp` 里
# 出现 7908-8007（Hyper-V/WSL/Docker 的动态保留），此时绑 8000 必失败，
# 且 netstat 看不到任何进程占着 —— 极容易误判成「起得慢」。这里先探能不能绑。
port_bindable() {
  "$PY" -c "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',int(sys.argv[1]))); s.close()" "$1" 2>/dev/null
}
API_PORT="${API_PORT:-}"
if [ -z "$API_PORT" ]; then
  for _p in 8000 8010 8020 8030 8080 8090; do
    port_bindable "$_p" && { API_PORT="$_p"; break; }
  done
fi
[ -n "$API_PORT" ] || { echo "$ERR 8000/8010/8020/8030/8080/8090 都绑不上，先查保留段：netsh interface ipv4 show excludedportrange protocol=tcp"; exit 1; }
if [ "$API_PORT" != "8000" ]; then
  echo "[info] 8000 绑不上（多因 Windows 保留端口段），本次改用 :$API_PORT"
fi
http_ok() {  # $1=端口 $2=健康路径；只认 200
  local code
  code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$1$2" 2>/dev/null)"
  [ "${code:-000}" = "200" ]
}
port_up() { http_ok "$1" /health; }   # mlflow 与 app 都有 /health

show_prompt_header() {  # 打一发 /chat，只显示 X-Prompt-* 响应头
  # ⚠️ 不要写成 `curl … | grep -i x-prompt || echo 没看到`：pipefail 下 curl 的退出码
  #    会把整条管道判失败，于是**头已经打出来了还会误报「没看到」**（2026-10-23 实测）。
  #    先捕获到变量再判断，退出码不参与判断。
  local hdr
  hdr="$(curl --noproxy '*' -s -D - -o /dev/null -X POST "http://127.0.0.1:$API_PORT/chat" \
    -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"ping"}],"structured":true}' 2>/dev/null \
    | grep -i 'x-prompt' || true)"
  if [ -n "$hdr" ]; then
    echo "$hdr"
  else
    echo "$WARN 没看到 X-Prompt-Version 头（服务不是 D16 之后的版本？或 structured 没传 true）"
  fi
}

wait_port() {  # $1=端口 $2=最长等待秒数 $3=名字；每 2s 探一次，带进度点
  local port="$1" limit="${2:-180}" name="${3:-服务}" waited=0
  while [ "$waited" -lt "$limit" ]; do
    port_up "$port" && { [ "$waited" -gt 0 ] && echo; return 0; }
    sleep 2; waited=$((waited + 2)); printf '.'
  done
  echo
  return 1
}

# ---- 原始指针记录 + 退出时自动回拨 ------------------------------------------
registry_pointer() {  # 打印 production 当前指向的版本号（registry 才是真指针）
  "$PY" - <<'PY'
import os, sys
sys.path.insert(0, "app")
from dotenv import load_dotenv
load_dotenv(".env")
from langfuse import Langfuse
lf = Langfuse(public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
              secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
              host=os.getenv("LANGFUSE_HOST"))
p = lf.get_prompt("structured_json", label="production", cache_ttl_seconds=0)
print(p.version)
PY
}

ORIG_PTR="$(registry_pointer 2>/dev/null || echo unknown)"
echo "[info] 演示前 production 指向 v${ORIG_PTR}"

restore_pointer() {
  [[ "$ORIG_PTR" =~ ^[0-9]+$ ]] || { echo "$WARN 原始指针未取到（v${ORIG_PTR}），跳过自动回拨"; return; }
  local cur
  cur="$(registry_pointer 2>/dev/null || echo unknown)"
  if [ "$cur" != "$ORIG_PTR" ]; then
    echo
    echo "$WARN 指针不在原值（现 v${cur}，应回 v${ORIG_PTR}），自动拨回中…"
    if "$PY" scripts/register_prompts.py --name structured_json --version "$ORIG_PTR" \
         --label production --move >/dev/null 2>&1; then
      echo "$OK 已拨回 v${ORIG_PTR}"
    else
      echo "$ERR 自动回拨失败！请手动执行："
      echo "      $PY scripts/register_prompts.py --name structured_json --version $ORIG_PTR --label production --move"
    fi
  fi
}
trap restore_pointer EXIT
trap 'exit 130' INT TERM   # 交给 EXIT trap 统一清理

# =============================================================================
banner "第 0 步 · 前置自检（服务没起就自动起）"
# =============================================================================
if port_up 5000; then
  echo "$OK MLflow :5000 已在跑"
else
  echo "[info] MLflow 没起 —— 从课程仓起（mlflow.db + qa-prompt-eval 在那儿）"
  pushd "$COURSE_REPO" >/dev/null || exit 1
  nohup "$PY_MLFLOW" -m mlflow server \
    --backend-store-uri sqlite:///mlflow.db \
    --host 127.0.0.1 --port 5000 >"$LOG_DIR/mlflow.log" 2>&1 &
  MLFLOW_PID=$!
  popd >/dev/null || exit 1
  echo "[info] 已启动（PID $MLFLOW_PID）；等 MLflow 起来（最长 180s；冷启动 + 后台限流时实测要 2 分钟以上）"
  if wait_port 5000 180 MLflow; then
    echo "$OK MLflow :5000 已起来（日志 $LOG_DIR/mlflow.log）"
  else
    echo "$ERR MLflow 180s 内没起来，日志最后 20 行："
    tail -20 "$LOG_DIR/mlflow.log" 2>/dev/null || echo "（日志文件为空）"
    echo "      手动起：cd $COURSE_REPO && $PY_MLFLOW -m mlflow server --backend-store-uri sqlite:///mlflow.db --host 127.0.0.1 --port 5000"
    exit 1
  fi
fi

if port_up "$API_PORT"; then
  echo "$OK API :$API_PORT 已在跑"
else
  echo "[info] API 没起 —— 用项目自带的 scripts/dev_app.sh 起（它会灌 .env + 清代理 + cd app）"
  # ⚠️ 不要用 `( cd app && nohup ... & )`：交互式终端里子 shell 退出时后台任务会被回收，
  #    表现为日志 0 字节、端口一直不通（2026-10-23 实测）。顶层 nohup + 现成启动器最稳。
  PORT="$API_PORT" nohup bash "$REPO_ROOT/scripts/dev_app.sh" >"$LOG_DIR/app.log" 2>&1 &
  APP_PID=$!
  echo "[info] 已启动（PID $APP_PID），端口 :$API_PORT，日志 $LOG_DIR/app.log；等 API 起来（最长 180s，实测首启可到 100s+）"
  # 早死检测：进程几秒内就没了 → 立刻报，别让人对着点阵空等 3 分钟
  for _ in 1 2 3 4 5 6; do
    sleep 1
    port_up "$API_PORT" && break
    if ! kill -0 "$APP_PID" 2>/dev/null; then
      echo "$ERR 启动进程已退出（PID $APP_PID），日志最后 10 行："
      tail -10 "$LOG_DIR/app.log" 2>/dev/null || echo "（日志为空）"
      echo "      → 换一种起法：另开一个终端跑 PORT=$API_PORT bash $REPO_ROOT/scripts/dev_app.sh"
      echo "        确认看到 Uvicorn running 后，回到这里加 API_PORT=$API_PORT 重跑本脚本"
      exit 1
    fi
  done
  if wait_port "$API_PORT" 180 API; then
    echo "$OK API :$API_PORT 已起来（日志 $LOG_DIR/app.log）"
  else
    echo "$ERR API 120s 内没起来，日志最后 20 行："
    tail -20 "$LOG_DIR/app.log" 2>/dev/null || echo "（日志文件为空）"
    echo "      手动起：另开一个终端跑 bash $REPO_ROOT/scripts/dev_app.sh（前台，Ctrl+C 退出）"
    exit 1
  fi
fi

if http_ok 3000 /api/public/health; then
  echo "$OK Langfuse :3000 在跑（trace 落这儿）"
else
  echo "$WARN Langfuse :3000 没响应（探活 /api/public/health）—— 第 5 步的 trace 验收会缺证据"
fi

# 「代码版本」只关心已跟踪文件的改动；未跟踪的新文件（如本脚本自己）不算 dirty
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$WARN git 有已跟踪文件的改动 —— 快照五件套的「代码版本」会带 dirty 标记："
  git status --porcelain --untracked-files=no | head -5
else
  echo "$OK git 工作区干净（HEAD=$(git rev-parse --short HEAD)）"
fi
if [ -n "$(git ls-files --others --exclude-standard | head -3)" ]; then
  echo "[info] 未跟踪的新文件（不影响代码版本快照）：$(git ls-files --others --exclude-standard | head -3 | tr '\n' ' ')"
fi
pause

# =============================================================================
banner "第 1 步 · 证据一：prompt 是文件（git 侧）"
# =============================================================================
echo "证什么：prompt 的「内容」归 git 管 —— 可 diff、可回滚、可评审。"
git log --oneline -3 -- prompts/
echo
"$PY" - <<'PY'
import hashlib, sys, yaml
from pathlib import Path
d = Path("prompts/structured_json")
for v in (1, 2):
    tpl = yaml.safe_load((d / f"v{v}.yaml").read_text(encoding="utf-8"))["template"]
    print(f"  v{v}.yaml  hash={hashlib.sha256(tpl.encode()).hexdigest()[:8]}")
labels = yaml.safe_load((d / "labels.yaml").read_text(encoding="utf-8"))
print(f"  labels.yaml（git 镜像）: {labels}")
print("  注意：registry（Langfuse）才是真指针，labels.yaml 只是一面镜子")
PY
pause

# =============================================================================
banner "第 2 步 · 证据二：线上现在用的是哪一版（拨 label 之前）"
# =============================================================================
echo "证什么：服务每个请求都回显 prompt 版本（响应头 X-Prompt-Version）。"
# 前置守卫：API 中途挂掉时，别让 curl 静默失败变成「没看到响应头」的误导信息
if ! port_up "$API_PORT"; then
  echo "$ERR API :$API_PORT 不通 —— 另开一个终端跑 PORT=$API_PORT bash $REPO_ROOT/scripts/dev_app.sh，再回到这里重跑"
  exit 1
fi
echo "--- curl /chat 响应头 ---"
show_prompt_header
echo "注：请求体刻意用 ASCII —— GBK 控制台会把中文 payload 转码成乱码（commit 664ce33）"
pause

# =============================================================================
banner "第 3 步 · 证据三：两个候选各跑 10 条（MLflow 并排）"
# =============================================================================
cat <<'EOM'
[!!] 命名错位警告（本次演示最大的坑 —— 认 hash 不认名字）：
     track_prompt_run.py 的【内联表】v0/v1 是 D14 的旧编号：
       · 脚本 v0 = yaml v1（线上现状）  hash d7b79571
       · 脚本 v1 = yaml v2（发布候选）  hash 009c95a5
     下面用 --tag 把 yaml 身份显式写进 run，UI 里按 hash 对齐，别按名字。
EOM
echo "--- 跑「线上现状」（yaml v1 / d7b79571）---"
"$PY" scripts/track_prompt_run.py --prompt-version v0 --max-tokens 1024 \
  --tag "yaml_identity=structured_json@v1:d7b79571" --tag demo=d19 \
  || { echo "$ERR v0 跑挂，看上面输出（常见原因：max_tokens 太小被截断 / 上游故障）"; exit 1; }
echo
echo "[info] 等 70s 让上游免费额度限流窗口（20 次/分钟）回落，避免 v1 评估被 429 打挂（2026-10-24 实测：v0+v1 共 22 次连发超 20/min 上限）"
sleep 70
echo "--- 跑「发布候选」（yaml v2 / 009c95a5）---"
"$PY" scripts/track_prompt_run.py --prompt-version v1 --max-tokens 1024 \
  --tag "yaml_identity=structured_json@v2:009c95a5" --tag demo=d19 \
  || { echo "$ERR v1 跑挂，看上面输出（常见原因：max_tokens 太小被截断 / 上游故障）"; exit 1; }
echo
echo "$OK 两个 run 已落 qa-prompt-eval（mlflow.db 在课程仓）："
echo "    http://127.0.0.1:5000/#/experiments/  ->  qa-prompt-eval"
echo "    看点：refusal_rate（0.0 -> 0.2）与 format_ok_rate（双 1.0），血缘四项一致"
pause

# =============================================================================
banner "第 4 步 · 发布：production label 拨到 v2"
# =============================================================================
echo "证什么：发布 = 拨指针，一条命令，不重新构建任何东西。"
"$PY" scripts/register_prompts.py --name structured_json --version 2 --label production --move \
  || { echo "$ERR 拨 label 失败"; exit 1; }
echo "--- labels.yaml 镜像（git 侧的指针快照）---"
cat prompts/structured_json/labels.yaml
echo
echo "[info] 等 65s 让服务的 prompt 缓存过期（PROMPT_CACHE_TTL=60，SDK 侧缓存）…"
sleep 65
# ⚠️ SDK 是「先给旧值、后台刷新」（stale-while-revalidate）：TTL 到期后的**第 1 发**返回旧值
#    并触发刷新，**第 2 发**才是新值。所以换版生效 = TTL + 1 次请求（2026-10-23 实测确认）。
echo "[info] 先打一发预热（这一发会返回旧值，专门用来触发刷新）"
show_prompt_header >/dev/null
echo "--- 拨完之后再打一发（这一发才是新值）---"
show_prompt_header
echo "预期：X-Prompt-Version: structured_json@v2 / X-Prompt-Hash: 009c95a5"
pause

# =============================================================================
banner "第 5 步 · 证据四：trace 侧可追溯（Langfuse）"
# =============================================================================
echo "证什么：每次请求都落成 trace，事后能回答「哪版 prompt 被用了多少次」。"
echo "（版本回显的机器可读证据是响应头 X-Prompt-Version —— 第 2/4/6 步已取；"
echo "  trace 里的 prompt_version 元数据在 UI 详情可见，公共 API 不返回，见下）"
"$PY" - <<'PY'
import base64, json, os, urllib.request
host = os.getenv("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
tok = base64.b64encode(
    f'{os.getenv("LANGFUSE_PUBLIC_KEY")}:{os.getenv("LANGFUSE_SECRET_KEY")}'.encode()
).decode()
req = urllib.request.Request(f"{host}/api/public/v2/observations?limit=50")
req.add_header("Authorization", f"Basic {tok}")
try:
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
except Exception as e:
    print(f"  [!!] observations 查询失败：{e}")
    print("  -> 改用 UI 目检（见下）")
else:
    chats = [o for o in data.get("data", []) if o.get("name") == "chat"]
    print(f"  面板数据源可见 chat span {len(chats)} 条，最新 3 条：")
    for o in chats[:3]:
        print(f"    {o.get('startTime','')[:19]}  trace={o.get('traceId','')}")
PY
echo
echo "UI 目检（浏览器开 ${LANGFUSE_HOST:-http://localhost:3000}）："
echo "  1. Tracing -> 最新一条 trace（就是刚才拨到 v2 后那发请求），头部 chip"
echo "     = prompt: structured_json · v2；详情里 metadata.prompt_version = v2"
echo "  2. Prompt Name 列有值；Linked Generations 能反查这版被哪几次请求用过"
echo "  3. [!!] 原生关联 != 可按版本筛选 —— Tracing 查询维度是另一套索引（本周薄弱点）"
echo "  4. [注] v4 events_only 口径：公共 REST 查询 API 不返回 metadata（D9 实测），"
echo "     所以脚本只列 trace 存在性，元数据以 UI 详情为准"
pause

# =============================================================================
banner "第 6 步 · 回滚：production label 拨回 v1"
# =============================================================================
echo "证什么：回滚 = 同一条命令反方向，不重新构建镜像。"
"$PY" scripts/register_prompts.py --name structured_json --version 1 --label production --move \
  || { echo "$ERR 拨 label 失败"; exit 1; }
echo "[info] 再等 65s 缓存过期…"
sleep 65
echo "[info] 先打一发预热（返回旧值 v2，触发刷新）"
show_prompt_header >/dev/null
echo "--- 回滚后再打一发（这一发应回到 v1）---"
show_prompt_header
echo "预期：X-Prompt-Version: structured_json@v1 / X-Prompt-Hash: d7b79571（回到旧版本）"
pause

# =============================================================================
banner "第 7 步 · 收尾自检"
# =============================================================================
CUR_PTR="$(registry_pointer 2>/dev/null || echo unknown)"
if [ "$CUR_PTR" = "$ORIG_PTR" ]; then
  echo "$OK 指针已回到演示前状态（production -> v${CUR_PTR}）"
else
  echo "$WARN 指针不在原值（v${CUR_PTR} vs v${ORIG_PTR}）"
fi
if [ -z "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$OK git 已跟踪文件无改动（labels.yaml 内容复原 -> 无 diff）"
else
  echo "$WARN git 有已跟踪文件的改动："
  git status --porcelain --untracked-files=no
fi
echo
echo "======== 演示口径（五步里只有第 4 步是指令，其余四步是证据）========"
cat <<'EOM'
  1. git 里 prompt 是文件      -> 改一个词就是一次提交，可 diff 可回滚
  2. 响应头带版本              -> 线上用的是哪版，curl -i 一眼可见
  3. MLflow 两个 run 并排     -> 同代码同题，只有 prompt 不同；refusal 0 -> 0.2
  4. 拨 label = 发布          -> 不重新构建，下一个请求就换版本
  5. trace 带 prompt_version  -> 事后能归因「这版被用了多少次」
  回滚 = 第 4 步反方向        -> 前面四步自动跟着变
EOM
echo "$OK 演示完成。"
