"""pytest 启动钩子：把 app/ 加进 sys.path。

仓库代码用裸 import（app/*.py 互相 import），测试要能 `import routing`、
`import prompts`，就得让 app/ 在 sys.path 上。放 conftest 里一次性解决，
不用每个测试文件都 sys.path.insert。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# 测试用 mock 上游（不依赖真实 API Key、不联网）
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("MOCK_FAIL_N_TIMES", "0")
# 关掉观测上报：单测/集成测不往 Langfuse 写 trace
os.environ.setdefault("OBSERVABILITY", "off")
