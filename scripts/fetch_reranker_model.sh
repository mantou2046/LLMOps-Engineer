#!/usr/bin/env bash
# =============================================================================
# 取本地 rerank 模型（W6 D2 起用）
# -----------------------------------------------------------------------------
# 为什么不用 huggingface_hub：本机 HuggingFace 三个入口全不通（见
# fetch_embedding_model.sh 头注释）。ModelScope 直连可用，走它。
#
# 模型：BAAI/bge-reranker-base（cross-encoder，XLM-RoBERTa-base 架构，中文可用）
# 权重约 1060 MB。⚠️ 大包下载容易残缺：curl -C - 断点续传 + 落盘后校验体积。
#
# 用法：
#     bash scripts/fetch_reranker_model.sh
#     bash scripts/fetch_reranker_model.sh /custom/dir
#
# 不用 FlagEmbedding：transformers 的 AutoModelForSequenceClassification 直接能载，
# 少一个依赖、少一次版本冲突。
# =============================================================================
set -euo pipefail

DEST="${1:-models/bge-reranker-base}"
BASE="https://modelscope.cn/models/BAAI/bge-reranker-base/resolve/master"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIN_SAFETENSORS=$((1000 * 1024 * 1024))   # 1.06 GB，低于 1 GB 判定为残缺

cd "$REPO_ROOT"
mkdir -p "$DEST"

FILES=(
  config.json
  special_tokens_map.json
  tokenizer_config.json
  tokenizer.json
  sentencepiece.bpe.model
  model.safetensors
)

for f in "${FILES[@]}"; do
  printf '  %-30s' "$f"
  # -C - 续传；--noproxy '*' 绕开宿主代理；-m 900 单文件最长 15 分钟
  curl -sL --noproxy '*' -m 900 -C - -o "$DEST/$f" "$BASE/$f" || true
  printf '%s bytes\n' "$(wc -c < "$DEST/$f" | tr -d ' ')"
done

SIZE=$(wc -c < "$DEST/model.safetensors" | tr -d ' ')
if [ "$SIZE" -lt "$MIN_SAFETENSORS" ]; then
  echo "ERROR: model.safetensors 只有 $SIZE 字节，下载不完整（重跑本脚本会续传）" >&2
  exit 1
fi

echo
echo "OK -> $DEST"
echo "自检："
echo "  HF_HUB_OFFLINE=1 ./.venv-eval/Scripts/python.exe -c \"from transformers import AutoTokenizer, AutoModelForSequenceClassification; t=AutoTokenizer.from_pretrained('$DEST'); m=AutoModelForSequenceClassification.from_pretrained('$DEST'); print(m.config.num_labels, m.config.max_position_embeddings)\""
