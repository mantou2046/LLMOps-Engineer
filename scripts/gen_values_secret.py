# -*- coding: utf-8 -*-
"""
从 .env 生成 Helm 用的 values-secret.yaml（供 -f 注入）。

设计要点：
  - 只取需要的 3 个键，不整包搬运（缩小暴露面）
  - 输出 YAML 用双引号包裹值，YAML 特殊字符（冒号/井号/引号）自动转义
  - **不打印任何值**，只回报「键名 + 长度 + 是否非空」
"""
import os
import sys

REPO = r"E:\Projects\LLMOps-Engineer"
ENV_FILE = os.path.join(REPO, ".env")
OUT_FILE = os.path.join(REPO, "infra", "k8s", "llmops-chart", "values-secret.yaml")

NEEDED = ["POSTGRES_PASSWORD", "LLM_PRIMARY_API_KEY", "LITELLM_MASTER_KEY"]


def parse_env(path):
    """解析 .env。<键>=<值>，忽略注释与空行；值去掉外层引号。"""
    data = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # 去掉成对引号
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            data[k] = v
    return data


def yaml_quote(s):
    """用双引号包成合法 YAML 标量（转义 \\ 和 "）。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    if not os.path.isfile(ENV_FILE):
        print("ERROR: .env 不存在: " + ENV_FILE)
        return 1

    env = parse_env(ENV_FILE)
    missing = [k for k in NEEDED if k not in env]
    empty = [k for k in NEEDED if k in env and not env[k]]

    print("=== 读取结果（仅键名与长度，不显示值） ===")
    for k in NEEDED:
        v = env.get(k)
        if v is None:
            print("  {:<22} MISSING".format(k))
        else:
            print("  {:<22} len={:<4} nonempty={}".format(k, len(v), bool(v)))

    if missing:
        print("\nWARN: 以下键在 .env 中不存在，将写空值：" + ", ".join(missing))
    if empty:
        print("WARN: 以下键在 .env 中为空，将写空值：" + ", ".join(empty))

    lines = [
        "# 本文件由 scripts 从 .env 生成，含真值 —— 已被 .gitignore 挡住，切勿提交",
        "# 重新生成：见 infra/k8s/llmops-chart/README 或 D6 笔记",
        "",
        "secret:",
        "  data:",
    ]
    for k in NEEDED:
        lines.append("    {}: {}".format(k, yaml_quote(env.get(k, ""))))
    content = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    print("\n=== 已写出 ===")
    print("  " + OUT_FILE)
    print("  字节数: " + str(len(content.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
