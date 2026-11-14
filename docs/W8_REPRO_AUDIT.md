# 全链路回归与 W8 复盘（W8 D6 · 2026-11-14）

> 任务卡 11/29 原句：**做**（2h）全链路回归一遍，确认没有一步是「靠记忆才能跑」；**记**（1h）W8 复盘 + 作品集 checklist 逐项打勾；**产出**作品集可投递；**验收**把它交给一个陌生工程师，他独立能跑起来。
> 本页是正本；库内过程档见 `E:\Projects\Obsidian\YYDS\01LLMOps-Engineer\LLM基础学习\W8 D6 动手实践 · 全链路回归与复盘.md`。

## 1 · 「没有一步靠记忆」怎么变成可检查的东西

「我演示给你看」和「你自己能跑起来」的差别，不在脚本齐不齐，而在**命令链上有没有隐含前提**：某个环境变量要手动设、某个 compose 命令要额外带参数、某个依赖只有跑评估时才知道要装。这些东西不写进 README，陌生人一定会卡住，而且卡住时报错指向别处。

所以本轮把回归做成一条可复跑的命令 `python scripts/w8d6_repro_check.py`：它按七个阶段核对 README 里每一条会被照抄的路径与命令、七类前置条件是否写在明处、相对链接是否失效、默认关闭的开关是否真的默认关闭，并真跑非 LLM 门禁的三个方向。纯标准库、零网络、零凭据、不需要 Docker，在干净克隆里也能跑；输出全 ASCII，GBK 控制台不乱码。

## 2 · 七阶段回归表

顺序与阅读清单 11/29 一致：环境 → 容器构建 → 灌库/检索 → 评估门禁 → 护栏与成本开关 → trace，最后加一层文档与脱敏核查。

| 阶段 | 陌生人最容易卡在哪 | 文档落点 | 本轮核法 | 结果 |
| --- | --- | --- | --- | --- |
| 1 环境 | 代理端口是死的；`localhost` 走 IPv6；`.env` 没填 `POSTGRES_PASSWORD`；评估依赖没装 | `README.md` 第 5 段前置表、`docs/env-setup.md` | 7 类前置关键词、`.env` 未被 git 跟踪、`.gitignore` 两条、CI Python 版本 | 补写后全过 |
| 2 容器构建 | `docker compose -f infra/compose.yml` 把项目目录指到 `infra/`，读不到仓库根 `.env`；没等 `healthy` 就跑验收 | `README.md` 第 5 段、`scripts/verify_d2.sh`、`infra/DOCKER-SETUP-Win11.md` | Dockerfile / compose / healthcheck / CI build 步骤 / 脚本内 `--env-file` | 全过 |
| 3 灌库与检索 | golden set 与冻结语料在哪、`ivfflat.probes` 要不要改、5 个数字去哪核 | `evals/RAGAS_BASELINE.md`、`docs/ARCHITECTURE.md`、README 第 4 段 | 题库 / 语料 / 灌库与检索脚本 / 五份数字出处文件 | 全过 |
| 4 评估门禁 | CI 里的门禁和本地跑的是不是同一条、改坏 prompt 会不会真被拦 | `evals/CI_EVAL_GATE.md`、`scripts/w6d6_eval_gate.py` | 真跑正常 / `--sabotage` / `--self-test`，核退出码 0 / 1 / 0 | 全过 |
| 5 护栏与成本开关 | 三个开关默认关着，照 README 跑会以为功能没实现 | `app/main.py`、`evals/OUTPUT_GUARD_BENCH.md`、`evals/COST_BENCH.md` | 代码里默认值 `"0"` 逐个核、报告与验收文件存在 | 全过 |
| 6 trace | 以为 Langfuse 上报是必需的、本机没起服务就判失败 | `app/observability.py`、`evals/AGENT_GRAPH.md` | Agent / 观测 / 两个 trace 校验脚本与报告存在 | 全过 |
| 7 文档、链接与脱敏 | README 里的相对链接失效、复现命令引用了不存在的脚本、凭据进了版本库 | `README.md`、`docs/REPO_AUDIT.md` | 五段结构、导航表、相对链接、README 命令链 16 个路径、CI 引用脚本、脱敏脚本与报告 | 补写后全过 |

## 3 · 首跑真的查出了缺口（不是零发现）

`python scripts/w8d6_repro_check.py` 首跑 **75 / 80 PASS，退出码 1**，5 项失败：

| # | 失败项 | 性质 |
| --- | --- | --- |
| 1 | 第 5 段命令链没写 `requirements-eval.txt` | 跑全量 RAGAS 评估才会发现少装依赖 |
| 2 | 第 5 段命令链没写 `--env-file .env` | 只有 W1 逐周明细里提过，照抄命令链会报 `required variable POSTGRES_PASSWORD is missing a value` |
| 3 | README 没提到 `requirements-eval.txt` | 同上 |
| 4 | README 没提到 `scripts/w8d6_repro_check.py` | 体检入口本身没进文档 |
| 5 | `docs/W8_REPRO_AUDIT.md` 不存在 | 本页当时尚未落盘 |

补写动作全部落在 README 第 5 段，没有改脚本去迁就文档：

- `cp .env.example .env` 注释从「填自己的值」改成「至少填 `POSTGRES_PASSWORD`」；
- 命令链加 `pip install -r requirements-eval.txt`（注明仅全量 RAGAS 评估需要）；
- 两条 compose 命令都改成 `docker compose --env-file .env -f infra/compose.yml ...`，并在下方写出为什么（`infra/` 作为项目目录读不到根 `.env`）；
- 命令链加 `python scripts/w8d6_repro_check.py`，「30 秒看懂」导航表的「我自己跑一遍」行挂上本报告链接（表仍保持七行，不改 D5 终验的计数口径）。

补写后复跑结果见 §4。**没有为了凑「全绿」删掉任何一条检查**；`--env-file .env` 这一项特意只认第 5 段命令链，W1 明细里那句提示不算数。

## 4 · 机器侧实跑证据

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python scripts/w8d6_repro_check.py`（首跑） | 75 / 80 PASS，5 项缺口见 §3 | 1 |
| `python scripts/w8d6_repro_check.py`（补写后复跑） | 80 / 80 PASS | 0 |
| `python -m pytest` | 179 passed, 2 warnings | 0 |
| `python scripts/w6d6_eval_gate.py` | 子集 20 题，`IDBasedContextRecall@5 = 0.75`，阈值 0.65，PASS | 0 |
| `python scripts/w6d6_eval_gate.py --sabotage` | recall 0.0，FAIL | 1 |
| `python scripts/w6d6_eval_gate.py --self-test` | 正常放行 + 改坏被拦，两者都验 | 0 |
| `ruff check scripts/w8d6_repro_check.py` | 通过 | 0 |
| `python scripts/check_sensitive.py --staged` | 0 命中 | 0 |
| `python scripts/audit_public_repo.py` | 工作区 / 已公开历史 / 历史文件名三范围 0 命中 | 0 |

`--staged` 只覆盖暂存区，CI 与 `audit_public_repo.py` 覆盖全树与历史；三条命令的分工见 `docs/REPO_AUDIT.md` §6。首跑 `--staged` 命中 2 处「疑似凭据键值」误报，都在新脚本的函数签名：`KV_NAME` 含 `token` 字样，把形参名当键、后面的类型注解当值；形参改成不含该字样的名字后复跑 0 命中，与 D4 那次把配置写成裸等号的误报同类，检查逻辑与事实都没改。本页描述这个坑时也刻意避开了「键名加冒号再接一长串」的写法，否则报告自己会被门禁拦下。本轮没有重跑需要真实上游、GPU 或 K8s 集群的链路，§7 如实列出。

## 5 · W8 复盘三问（按 11/14 提前执行时点作答）

**1）本周做出了什么能演示的东西？** 架构图 v4.1（C4 两层 + 54 条决策标注）、README 五段与 5 个带出处的数字、220 秒配音 + 中文字幕的 demo 视频、技术博客 1（证伪口径）、公开仓库主页（3 个真实状态徽章 + 七行导航表 + 历史脱敏终检），以及本轮新增的全链路回归报告与一条命令的可复现性体检。这些都在公开仓库里，不依赖本机内存。

**2）哪个概念只会用、说不出为什么？** 没有在本轮新增证伪，按 W6 / W7 周复盘的既有结论，面试前仍要补的是：`context precision` 为什么依赖 `reference`、为什么用不读 response 的 `context recall` 当噪声标尺、IVFFlat `probes` 与索引重建漂移的关系、P95 直方图桶宽插值为什么会放大读数、语义缓存本机缺的三个前置。W8 没有把它们写成「已补」。

**3）作品集这一周多出来的那一层，能不能 3 分钟讲清楚？** 能讲，骨架是「**可复现 + 可核对**」：每条结论指回一份报告，每个数字标出处，每条选型写明为什么不是另一个做法，做不到的（真微调业务效果、语义缓存、Langfuse 常驻、真人验收）如实列成欠账。W7 D7 的 3 分钟讲稿可直接复用；W8 新增的一句是「把『没有一步靠记忆』做成一条命令，首跑真的查出了 3 处文档缺口」。

## 6 · 八周总收口清单逐项核对

按任务卡「八周总收口清单」原顺序，**没做的没打勾**，口径与任务卡一致。

| # | 项 | 状态 | 依据 / 边界 |
| --- | --- | --- | --- |
| 1 | 作品集公开仓库（已脱敏） | ✅ | `docs/REPO_AUDIT.md` 记 D5 时点 166 提交 / 43,989 条历史新增行 / 259 跟踪文件；本轮复跑为 167 提交 / 44,362 行 / 262 路径，三范围仍 0 命中 |
| 2 | README 含 5 个数字 | ✅ | README 第 4 段，五个数字逐条挂报告路径与小节 |
| 3 | 架构图（带决策标注） | ✅ | `docs/ARCHITECTURE.md` + 54 条 `docs/DECISION_INDEX.md` |
| 4 | demo 视频 | ✅ | `docs/demo/w8d3-demo-final.mp4` 已入库推送；⚠️ 上传到 B 站等平台仍留本人 |
| 5 | 技术博客 × 2 | ❌ | 博客 1 已完结；博客 2 按 2026-10-14 调整降为可选、投递后补，整项不打勾 |
| 6 | 简历（部署策略型 FDE × 资金/财务域） | ❌ | 11/30 D7 未开始 |
| 7 | JD 对照表 + 短板清单 | ❌ | 11/30 D7 未开始 |
| 8 | 5 个面试故事 + 案例题应答框架 | ❌ | 11/27 案例面试练习段无完成回执，D7 未开始 |
| 9 | 资金域 AI 用例发现提纲 | ❌ / 待核实 | 任务卡 11/26 晚选修段无完成回执、库内与仓内均未找到提纲正本；阅读清单该行已勾 ✅，两处口径不一致，按不臆测原则标待核实，等本人确认 |
| 10 | 第一批投递已发出 | ❌ | 11/30 D7 未开始 |

第 9 项是本轮盘点发现的口径漂移，只报告、不擅自改勾选或改判定。

## 7 · 边界与留口

- **D6 验收线是「交给一个陌生工程师，他独立能跑起来」**，要真人，AI 不代判。本轮完成的是「做」（全链路回归）与「记」（复盘 + checklist 核对）的机器侧部分，**按 D1 / D2 / D3 先例记为提前开工、主体完成，不自己判完结**；「产出：作品集可投递」也据此标为待终验，不写成已投递。
- 本机没有重跑的链路：容器起库与 pgvector 写入、真实上游（非 mock）问答、K8s / Helm / Terraform 部署、常驻 Langfuse 服务、全量 RAGAS 与真微调。本轮核的是「命令与前置写全没有、引用的脚本与报告在不在、门禁真跑是否仍 0 / 1 / 0」，不冒充这些链路已实跑。
- 承接前几日留口：pin 项目（GitHub 无官方接口）、真人「30 秒看懂」与「视频能独立看懂」回述、`sensitive_terms.txt` 四类真实词补齐、LICENSE 与 profile、`demo-worksheet` 答题纸签字、D2「换人换目录从头跑」——统一留 W8 收尾，不叠到 D6 验收线上。
- 提交历史只体检不改写（D5 口径），166 个已被引用的提交号保持不动。

## 8 · 复跑

```bash
cd LLMOps-Engineer
python scripts/w8d6_repro_check.py                 # 七阶段全量，含门禁真跑
python scripts/w8d6_repro_check.py --skip-live     # 只跑静态检查
python scripts/w8d6_repro_check.py --json          # 机器可读摘要
python scripts/w6d6_eval_gate.py --self-test       # 门禁单独复核
python scripts/check_sensitive.py --staged         # 提交前必跑
```

---

**交付状态（2026-11-14）**：D6 提前开工，全链路回归、W8 复盘与收口清单核对已落地，机器侧 80 / 80 PASS；真人独立跑起来的验收与「作品集可投递」终验留本人 / 后续拍板。
