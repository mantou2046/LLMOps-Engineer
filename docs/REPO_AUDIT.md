# 公开仓库脱敏终检与主页整理（W8 D5 · 2026-11-14）

| 项 | 结果 |
| --- | --- |
| 仓库 | `mantou2046/LLMOps-Engineer`（公开，main） |
| 提交历史 | 166 个提交，2026-10-07 ~ 2026-11-14 |
| 工作区 | 259 个跟踪文件，其中 252 个可扫文本 |
| 历史内容扫描 | 43,989 条新增行；含本地未推送分支时 44,130 条 |
| 内置规则命中 | 0（工作区 / 已公开历史 / 全部分支，三个范围都是 0） |
| 补充盲扫 | 真实内网域名 0、真实单位名 0、非示例邮箱 0 |
| 是否重写历史 | 否。理由见第 4 节 |
| 验收线 | 「从主页点进来 30 秒能看懂」需真人，AI 不代判 |

*执行口径：只查不打印明文；命中只报提交号 / 文件 / 类型。本文是 W8 D5「记（1h）公开仓库脱敏终检」的正本。*

---

## 1 · 为什么单独做一次历史终检

仓库原有两层门禁，各自看得见的范围不一样：

| 层 | 扫什么 | 看不见什么 |
| --- | --- | --- |
| `.githooks/pre-commit` → `check_sensitive.py --staged` | 本次提交暂存的文件 | 已经提交过的旧内容 |
| `.github/workflows/ci.yml` → `check_sensitive.py --all` | 当前分支全树 | 曾经提交过、后来删掉的内容 |
| 本次 `scripts/audit_public_repo.py` | 当前跟踪文件 + 全部提交的 `+` 新增行 + 历史文件名 | 模式识别不到的业务敏感词（见第 6 节） |

GitHub 官方指南里那条「删文件不够，历史里还在」指的就是第三行。凭据一旦进过某个提交，克隆仓库就能取回，所以公开仓库要定期回看历史，而不是只在提交时查一次。

本脚本不替代提交门禁，两者规则同源：`audit_public_repo.py` 直接 import `check_sensitive.py` 的 `PATTERNS`、`CONN_RX`、`KV`、`is_placeholder()` 与两组键名豁免，规则改一处两边一起生效。

## 2 · 本次结果

```bash
python scripts/audit_public_repo.py
python scripts/audit_public_repo.py --history --all-refs
```

| 范围 | 规模 | 结果 |
| --- | --- | --- |
| 工作区跟踪文件 | 259 个（实扫 252 个文本，7 个二进制/超大跳过） | 0 命中 |
| 已公开历史 `origin/main` | 166 个提交 / 43,989 条新增行 / 260 个历史路径 | 0 命中 |
| 全部分支（含本地 `demo-worksheet`） | 167 个提交 / 44,130 条新增行 | 0 命中 |

上表是 D5 改动入库前的规模。本轮新增 `docs/REPO_AUDIT.md` 与 `scripts/audit_public_repo.py` 两个文件后复跑，工作区变成 261 个跟踪文件 / 254 个可扫文本，仍是 0 命中；提交后历史新增行数会随这次提交增加，历史范围不变。

内置规则 8 类：私钥、各类 API Key / Token、JWT、内网 IP 段、带口令的连接串、疑似凭据键值，加上本地自定义词表。

历史文件名另行体检：全部历史里出现过的路径共 260 个，从未跟踪过真 `.env`、`terraform.tfstate`、`*.pem` / `*.key`、`*.kdbx`、`*.ipynb`。带敏感名字的只有两份 `.env.example`、`infra/langfuse/.env.example`，以及 Helm 的 `secret.yaml` / `values-secret.example.yaml` 这类模板和清单字段名，内容均为空占位或变量引用。`.env` 本身被 `.gitignore` 排除，从未入库。

### 补充盲扫

内置规则管凭据，管不到「单位名 / 内网域名 / 真实账号 / 表名字段名」这类业务词。另跑了一次通用模式盲扫，同样不回显命中原文：

| 模式 | 结果 | 判定 |
| --- | --- | --- |
| 内网域名（`*.internal` / `*.corp` / `*.lan` / `*.intra`） | 历史 14 行、3 个去重值 | 全部是 Docker 官方保留名 `host.docker.internal` 和 `sensitive_terms.example.txt` 里的示例域名，真实内网域名 0 |
| 中文单位名（公司 / 集团 / 银行 / 财政局等后缀） | 3 行、3 个去重值 | 全部落在 `evals/route_probe.py` 的自造问句里，是「集团」「银行」这类通用词被正则吞进前文，没有具体单位名 |
| 非示例邮箱 | 0 | 无 |

> 盲扫的教训：中文没有词边界，`[\u4e00-\u9fa5]{2,12}` 会把句子前半段一起吞进来。这类命中必须看语境再下结论，不能按条数报「发现敏感信息」。

## 3 · 提交历史体检（只体检，不改写）

| 项 | 实测 |
| --- | --- |
| 提交消息前缀 | `feat` 63 / `docs` 59 / `fix` 22 / `pre` 10 / `chore` 6 / `ai` 2 / 其余 4 |
| WIP / fixup / 草稿类消息 | 0 |
| `pre:` / `ai:` 批量改动快照 | 12 个（知识库 git 纪律带进来的，对公开读者信息量低） |
| 历史最大 blob | `docs/demo/w8d3-demo-final.mp4` 10.4 MB，其余是架构图 PNG 与答题纸 HTML |
| 分支 | `main`（= `origin/main`）、`demo-worksheet`（`f48d5dc`，本地未推送，含未签字答题纸草稿） |

**没有重写历史。** 三条理由：

1. 重写会改掉 166 个提交的 hash，README、博客、任务卡里已引用的链接和提交号全部失效；
2. 唯一能「整理掉」的是 12 个 `pre:` / `ai:` 快照消息，它们不影响仓库可读，收益远小于代价；
3. 本库纪律规定批量改动留快照、历史不擅自改写，公开仓库更不该在无人确认时做 force push。

「整理」在这里落地成：体检 + 把结论写成可复核的文档 + 补上会反复出现的未跟踪文件（见第 5 节），而不是把历史抹平。

## 4 · 主页整理

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 徽章 | `README.md` 文首 | CI 总状态（原有）、CI `test` job、Python 版本。三个链接都实测返回 200，且都指向真实状态；没有接的东西不放 |
| 30 秒导航表 | `README.md` 文首 | 按「想看什么 → 去哪」列六行，对应任务卡验收线 |
| 复现命令跨平台 | `README.md` 第 5 节 | 数字复核命令里的 Windows 专属 `cd /e/Projects/...` 改成通用写法 |
| 终检入口 | `README.md` 脱敏段 | 指向本文件与 `scripts/audit_public_repo.py`，写明「提交前门禁 ≠ 历史终检」 |
| `.gitignore` | 根 | 补 `infra/terraform/tfplan`。原规则 `*.tfplan` 匹配不到这个无扩展名文件，它一直以未跟踪身份挂着 |
| topics | GitHub 仓库设置 | 补 8 个与内容相符的标签，方便从主页和搜索进来 |

徽章只用真实状态：CI 最近 5 次 run 全部 success，`test` job 是 CI 里真实存在的英文 job 名（其余两个含中文名的 job 没做徽章，避免 URL 编码出错）。Python 版本徽章指向 `docs/env-setup.md`，那里记着 3.13 / 3.14 本机实测与 CI 用 3.12。

## 5 · 待办（本人，AI 不代办）

| # | 事项 | 为什么 AI 没做 |
| --- | --- | --- |
| 1 | pin 项目到个人主页 | GitHub GraphQL 的 mutation 只有 `pinIssue` / `pinEnvironment` / `pinIssueComment`，**没有 pin 仓库的官方接口**；网页路径：个人主页 → Pinned → Customize your pins → 勾选本仓库 |
| 2 | 补齐本地自定义词表后重跑终检 | `scripts/sensitive_terms.txt` 本轮为空，四类真实词条（单位/客户名、内网域名与地址、真实账号、表名/字段名）由本人在本地填写。真实词条不能写进公开仓库，也不能写进聊天记录；模板在 `scripts/sensitive_terms.example.txt` |
| 3 | 真人验收「点进来 30 秒能看懂」 | 和 D2「陌生人照 README 能跑起来」、D3「视频能独立看懂」一样，留 W8 收尾由真人判断 |
| 4 | 决定是否加 LICENSE | 当前 `licenseInfo` 为空，没擅自替作者选许可证 |
| 5 | 个人资料 name / bio 空白 | 属个人主页信息，不代填 |
| 6 | `demo-worksheet` 本地分支 | 含未签字答题纸草稿，签字后再决定推送或删除，本轮未动 |

## 6 · 复现命令

```bash
cd LLMOps-Engineer

# 全量：工作区 + 已公开历史 + 历史文件名
python scripts/audit_public_repo.py

# 只查历史，连本地未推送分支一起
python scripts/audit_public_repo.py --history --all-refs

# 提交前门禁（仍在，别省略）
python scripts/check_sensitive.py --staged
```

退出码 0 表示干净，1 表示有命中（只列位置，需人工判定），2 表示 git 命令执行失败。命中若确为真凭据，按 GitHub 官方指南清理历史并轮换该凭据；误报改写法或补豁免，不要关门禁。

---

**交付状态（2026-11-14 收口）**：任务卡 11/28 的「做」四项里，提交历史体检、README 徽章、复现说明补强已落地；pin 项目受限于无官方接口，留本人网页操作。「记（1h）公开仓库脱敏终检」由本文与 `scripts/audit_public_repo.py` 满足，三个范围均 0 命中。验收线「从主页点进来 30 秒能看懂」需真人，按 D1 / D2 先例留 W8 收尾，**本轮记为提前开工、主体完成，不自己判完结**。
