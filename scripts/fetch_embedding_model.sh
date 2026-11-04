#!/usr/bin/env bash
# =============================================================================
# 取本地 embedding 模型（W6 起用）
# -----------------------------------------------------------------------------
# 为什么不用 huggingface_hub：
#   本机 HuggingFace 三个入口全不通 —— 直连 huggingface.co 000、hf-mirror.com
#   308 跳回 huggingface.co、走系统代理仍 000。ModelScope 可直连，故走它。
#
# 模型：BAAI/bge-small-zh-v1.5（中文语义，512 维，权重约 96MB）
# ⚠️ 维度是 512，不是 384：rag_chunks.embedding 现为 vector(384)，换模型前须迁移表。
#
# 用法（Git Bash / WSL）：
#     bash scripts/fetch_embedding_model.sh
#     bash scripts/fetch_embedding_model.sh /custom/dir      # 指定落盘目录
#
# 落盘后设 .env：RAGAS_EMBEDDING_MODEL=models/bge-small-zh-v1.5
# =============================================================================
set -euo pipefail

DEST="${1:-models/bge-small-zh-v1.5}"
BASE="https://modelscope.cn/models/BAAI/bge-small-zh-v1.5/resolve/master"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

FILES=(
  config.json
  config_sentence_transformers.json
  modules.json
  sentence_bert_config.json
  special_tokens_map.json
  tokenizer.json
  tokenizer_config.json
  vocab.txt
  1_Pooling/config.json
  model.safetensors
)

cd "$REPO_ROOT"
mkdir -p "$DEST/1_Pooling"

for f in "${FILES[@]}"; do
  printf '  %-34s' "$f"
  curl -sL --noproxy '*' -m 600 -o "$DEST/$f" "$BASE/$f"
  printf '%s bytes\n' "$(wc -c < "$DEST/$f" | tr -d ' ')"
done

# 权重约 95.8 MB；明显偏小说明下到的是错误页
SIZE=$(wc -c < "$DEST/model.safetensors" | tr -d ' ')
if [ "$SIZE" -lt 90000000 ]; then
  echo "ERROR: model.safetensors 只有 $SIZE 字节，下载不完整" >&2
  exit 1
fi

echo
echo "OK -> $DEST"
echo "自检（应在 0.85 以上 / 0.3 左右 / 512 维）："
echo "  HF_HUB_OFFLINE=1 python -c \"from sentence_transformers import SentenceTransformer as S; m=S(r'$DEST'); v=m.encode(['银企直联的日终对账分哪几步','银企直联日终对账的流程步骤','今天天气不错'],normalize_embeddings=True); print(v.shape, v[0]@v[1], v[0]@v[2])\""
