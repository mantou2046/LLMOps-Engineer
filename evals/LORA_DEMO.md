# W7 D4 · LoRA 微调实践记录（2026-11-09 本机 / 2026-11-10 云端补跑）

> [!warning] 先读这一段：本文分两个时点
> 2026-11-09 本机实测 CPU-only（`nvidia-smi` 不存在），`torch` / `peft` / `transformers` / `datasets` / `accelerate` 全部未安装。当天交付的是纯 numpy 线性层低秩适配实跑与留给云 GPU 的 `notebooks/w7d4_lora_colab.py` 骨架（本机未执行），按任务卡验收线判**部分完成、待云端补跑**。
> **2026-11-10 已在 Kaggle T4 上补跑真 LoRA 训练并回填 §5，D4 整项改判完成。** §1–§4 与 §6 的数字仍是 11-09 本机那一次的记录，读的时候别和 §5 的真机数字混在一起。

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

⚠️ 11-09 时这句话只是玩具任务上的类比。2026-11-10 云端补跑已拿到真机的训练前后格式符合率（0% → 100%，见 §5），可以写进面试材料，但必须同时说明那是 40 条合成小样本的格式学习证据，不是业务效果提升。

## 5. 云端补跑（2026-11-10，Kaggle T4）

`notebooks/w7d4_lora_colab.py` 是留给 Colab / 云 GPU 的可执行骨架：`preflight()` 先查 GPU 与五个依赖，缺任一项就打印缺口并以退出码 2 停下，本机实测输出「缺少依赖：torch, transformers, peft, datasets, accelerate」，不会用 mock 冒充跑过。

实际补跑由土豆把骨架按本笔记规格改成 Kaggle 版（原稿只在 PC 仓、库内无副本，属重建版而非逐字转换），John 点名单次例外、用 t4 主号代跑。环境 Tesla T4（设置 x2、实际用 1 张）、联网开、免费配额内。第一轮撞到环境坑：Kaggle 预装 `torchao` 0.10.0 与现行 `peft`（要求 >0.16.0）冲突，`get_peft_model` 直接 ImportError；本 demo 不用量化，SETUP 格加一行 `pip uninstall -y torchao` 后通过，训练代码未动。

| 轮次 | 基座 | 可训练 / 总参数 | 训练耗时 | 峰值显存 | 格式符合率（留出 20） | 分类准确率 | adapter 权重 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 | `sshleifer/tiny-gpt2` | 128 / 102,714 | 2.3 s | 1011 MB | 0% → 0% | 0% → 0% | 128 个参数（目录 3,565,572 B 大头是 tokenizer） |
| v2 | `Qwen/Qwen2.5-0.5B` | 540,672 / 494,032,768（0.1%） | 8.2 s | 2890 MB | **0% → 100%** | **0% → 100%** | 2,175,168 B |

v2 超参：r=8、alpha=16、`target_modules=q_proj,v_proj`、lr=1e-3、epochs=5、batch=8、40 条训练 / 20 条留出、词不重叠、种子 42、只对回答段算 loss。分步 loss 1.01 → 0.02 → 0.0044，最终 train loss 0.3457。v2 原始结果留在 `evals/out/w7d4/w7d4_lora_result_v2_qwen.json` 与同名 `.md`（本地产物，不入库），字段与上表逐项对应。

两条教训：

- v1 学不动不是流水线错。`tiny-gpt2` 隐藏维只有 2、总参数 10 万，是随机初始化的单元测试模型，没有语言能力（样本输出是 `stairs` 之类的乱码重复）。选基座先确认它是真预训练模型，骨架原稿拿它当 demo 这个坑留在这里给后面的人。
- v2 的前后样本就是 §4 那句话的真机证据。训练前「菠萝」自由续写、拼出键不对的近似 JSON；训练后稳定输出 `{"类别": "水果"}`，熊猫 → `{"类别": "动物"}`、消防车 → `{"类别": "交通工具"}`，三条都是训练里没出现过的留出词。学到的是稳定的输出格式与行为，RAG 递最新资料改不了这种行为映射，示例训练可以。

### 边界

- 云端 v2 是真 Transformer 微调，但只有 40 条合成训练样本、留出 20 条，100% 只证明「格式行为可被 LoRA 学会」这一件事。不外推到真实业务效果，`docs/W7_D5_DECISION.md` 的 `E-FT-2` 因此改记为「小样本格式证据已量、业务效果未量」，不写收益数字。
- 本机 §2–§4 的 numpy 演示仍是线性层玩具，和云端数字不能互相外推。
- adapter 本体还在 Kaggle 笔记本的 Output 里，会话结束即清空；要长期保存需在 Kaggle 里 Save Version。
- 这次是单次代跑，没有重复实验、没有置信区间，也没有和全量微调 / Prompt / RAG 做同条件对照。

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
| `notebooks/w7d4_lora_colab.py` | 云端真微调骨架（本机未执行；Kaggle 重建版于 2026-11-10 跑通，见 §5） |
| `tests/test_lora_demo.py` | 8 项单测，含「lr 过大会发散」与「骨架拒绝假跑」 |
| `evals/out/w7d4/lora_demo.json` | 本机数学实跑明细（本地产物，不入库） |
| `evals/out/w7d4/w7d4_lora_result_v2_qwen.json` / `.md` | 云端 v2 原始结果与速记（本地产物，不入库） |
