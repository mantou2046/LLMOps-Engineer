# W7 D4 · LoRA 微调实践记录（2026-11-09）

> [!warning] 先读这一段：本机**没有跑成真微调**
> 2026-11-09 实测：本机 CPU-only（`nvidia-smi` 不存在），`torch` / `peft` / `transformers` / `datasets` / `accelerate` 全部未安装。任务卡 11/19 也写明「本机无 GPU，别硬扛」。
> 本轮交付的是：① **纯 numpy 的线性层低秩适配最小实跑**（真实梯度下降、可核对的前后数字）；② **留给 Colab / 云 GPU 的真微调脚本骨架**（`notebooks/w7d4_lora_colab.py`，本机未执行）。
> 因此按任务卡验收线，D4 判**部分完成、待云端补跑**，不标整项完成。

## 1. 为什么还要写这份记录

任务卡 11/19 降级后的目标是「跑通一个小 demo + 能讲清微调 / RAG / Prompt 的取舍」。本机跑不了真训练，但**流水线本身可以验证**：把「数据 → 冻结基线 → 只训练 adapter → 留出集评估 → 产物」每一步做成可调用的函数，跑出真实数字，就能把 LoRA 到底改了什么讲清楚，而不必先租 GPU。

`app/lora_demo.py` 是一个 64 维线性层上的数学等价演示：

- 冻结基础权重 `W₀`（对应「预训练模型不改」）。
- 真实需要的增量 `ΔW_target = U@V` 刻意构造成**真秩 4**（对应「LoRA 的核心假设：微调更新是低秩的」）。
- 全量微调训练整个 `ΔW`（64×64 = 4096 参数）；LoRA 只训练 `ΔW = B@A`（`B` 64×r、`A` r×64）。
- 200 条训练样本、50 条留出样本、固定种子 `20261119`，统一 400 轮，用真实梯度下降（链式法则手推，不调自动微分框架）。

> [!note]- 这个演示不是什么
> 没有 Transformer、没有注意力层、没有分词、没有量化（所以不是 QLoRA）、没有真实语言数据。目标是**刻意做成低秩**的，所以 r=4 能追平全量微调 —— 真实任务不保证更新低秩，这些数字不能外推到 LLM。

## 2. 实跑结果

`scripts/w7d4_lora_demo.py`，明细 `evals/out/w7d4/lora_demo.json`（本地产物，不入库）：

| 方法 | 可训练参数 | 学习率 | 训练 MSE 前→后 | 留出 MSE 前→后 | 耗时 | 产物字节 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 全量微调（整个 ΔW） | 4096 | 0.05 | 297.332 → 0.000068 | 308.070 → 0.000146 | 0.056 s | 83585 |
| LoRA r=4（只训练 B@A） | **512** | 0.002 | 297.332 → 0.000095 | 308.070 → **0.000106** | 0.066 s | **10709** |
| LoRA r=1（容量不足对照） | 128 | 0.002 | 297.332 → 152.586 | 308.070 → 190.111 | 0.068 s | 2781 |

- r=4 只用了 **12.5%** 的可训练参数、产物体积 **12.8%**，留出误差与全量微调同一量级。
- r=1 装不下真秩 4 的目标，留出 MSE 停在 190.111 —— **秩是容量，不是随便调小都行**。
- r=4 的留出误差（0.000106）甚至略好于全量（0.000146），不是「LoRA 普遍更强」，是因为目标本来就是秩 4 的低秩矩阵，LoRA 相当于加了正确的归纳偏置。

5 项断言全过：两路都真收敛、LoRA 参数与产物更小、r 太小确实学不全。单测 `tests/test_lora_demo.py` 8 项，全量 pytest **104 项**通过。

## 3. 踩到的一个坑：学习率不能照抄

首轮把全量微调的 `lr=0.05` 直接套到 LoRA，训练直接 `NaN`（`overflow encountered in square`）。原因是 `ΔW = B@A` 是**双线性**参数化，梯度经 `A` / `B` 互相放大；全量微调的梯度直接作用在 `ΔW` 上，同样是 0.05 却很稳。

扫过的区间（400 轮，r=4）：`0.02 / 0.01 / 0.005` 全部 NaN，`0.002` 收敛到 9.5e-05，`0.001` 类似，`0.0005` 只到 0.0068。反过来全量微调在 `lr=0.002` 下 400 轮只到 16.78，**根本没收敛**。

所以两路各取「能稳定收敛的步长」、统一 400 轮再比最终误差，并把 lr 明写进报告 —— 不是同超参对照，比的是参数量与最终误差。这条已留成单测断言（`test_lora_learning_rate_too_large_diverges`）。

## 4. 一句话回答任务卡：这次微调解决了什么，RAG 为什么解决不了

> 这次「微调」学的是一种**稳定的输出变换/行为**（把输入映射成固定形式的输出），不是往模型里塞新事实；RAG 是回答时临时递上最新资料，改变不了权重里的行为映射，所以「格式/行为总不稳定」该靠示例训练，「知识每周更新」该靠检索。

⚠️ 这句话在本轮只是**玩具任务上的类比**，真要写进面试材料，得等云端补跑拿到真实的「训练前后格式符合率」。

## 5. 云端补跑（待办，本节目前为空）

`notebooks/w7d4_lora_colab.py` 是留给 Colab T4 / 短时云 GPU 的可执行骨架：

- `preflight()` 先查 GPU 与五个依赖，缺任一项就打印缺口并以**退出码 2** 停下 —— 在本机实测输出「缺少依赖：torch, transformers, peft, datasets, accelerate」「这不是失败，是环境边界」，不会用 mock 冒充跑过。
- 玩具指令数据 4 条，学「字段=…；值=…；依据=…」这种固定格式；留出集 3 条，只评**格式符合率**，不评内容对错。
- 跑完会写出 `evals/out/w7d4/adapter/run_record.json`（基座、r/alpha、耗时、显存、训练前后格式符合率、adapter 路径）。

**待补**：真实训练耗时与显存、训练前后格式符合率、adapter 实际体积、以及「这次微调解决了什么」的真机证据。拿到后回填本节，并把库内 W7 D4 笔记与任务卡 11/19 的判定从「部分完成」改为完成。

## 6. 复现

```bash
cd /e/Projects/LLMOps-Engineer
/d/python314/python.exe scripts/w7d4_lora_demo.py
/d/python314/python.exe -m pytest tests/test_lora_demo.py -q
/d/python314/python.exe -m ruff check app/lora_demo.py scripts/w7d4_lora_demo.py tests/test_lora_demo.py
# 云端骨架在本机会明确停下（预期退出码 2）：
/d/python314/python.exe notebooks/w7d4_lora_colab.py
```

```powershell
cd E:\Projects\LLMOps-Engineer
D:\python314\python.exe scripts\w7d4_lora_demo.py
D:\python314\python.exe -m pytest tests\test_lora_demo.py -q
D:\python314\python.exe -m ruff check app\lora_demo.py scripts\w7d4_lora_demo.py tests\test_lora_demo.py
D:\python314\python.exe notebooks\w7d4_lora_colab.py
```

## 7. 落点

| 文件 | 作用 |
| --- | --- |
| `app/lora_demo.py` | 数据集 / 全量微调 / LoRA 训练 / 评估 / 产物体积（纯 numpy） |
| `scripts/w7d4_lora_demo.py` | 三组对照实跑 + 5 项断言 |
| `notebooks/w7d4_lora_colab.py` | 云端真微调骨架（**未执行**） |
| `tests/test_lora_demo.py` | 8 项单测，含「lr 过大会发散」与「骨架拒绝假跑」 |
| `evals/out/w7d4/lora_demo.json` | 明细（本地产物，不入库） |
