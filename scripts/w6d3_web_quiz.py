#!/usr/bin/env python3
"""生成 W6 D3 网页版答题页：候选句全部来自冻结语料原文，人点选的句子才成为标准答案。

单文件 HTML、零外部依赖、双击即开。答案由人在网页上勾选，脚本不生成任何答案文本。
用法：./.venv-eval/Scripts/python.exe scripts/w6d3_web_quiz.py
"""
from __future__ import annotations

import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import w6d3_worksheet as ws  # noqa: E402

OUT = ROOT / "evals" / "golden_set.quiz.html"
CORPUS = ws.load_corpus()
MIN_CANDIDATES = 5
MAX_OWN = 40  # 章节内句子全留；截断会把真正的答案句挡在选项外
DISTRACTORS = 3
HINT_WORDS = (
    "必须", "结论", "选择", "理由", "实测", "因为", "所以", "默认", "不能", "只有",
    "为什么", "代价", "复核", "验收", "判据", "一句话", "正确", "错误", "坑", "实际上",
    "✅", "⚠️", "判据", "意味着", "因此", "不能",
)


def conclusion_rank(s: str) -> int:
    """结论性句子排前面，纯规则排序，不做任何语义判断。"""
    return -sum(1 for w in HINT_WORDS if w in s)


ENUM_KEYS = ("列出", "哪几条", "分别是什么", "哪几个", "有哪些", "全部条目",
             "按原文顺序", "按步骤")


def is_enumerative(row: dict) -> bool:
    """列举题不受 3 句上限；用数据字段固化，避免两位标注员各自判断产生分歧。"""
    if row["must_refuse"] or row["type"] == "boundary":
        return False
    return row["type"] == "instruction" or any(k in row["question"] for k in ENUM_KEYS)

REFUSE_OPTIONS = [
    "文档里没有相关信息，应说明未覆盖并拒答",
    "给出具体数值或步骤",
    "含糊编一个大概的答案",
]
REFUSE_CORRECT = 0


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_sentences(text: str) -> list[str]:
    """从 Markdown 正文里抽出可当答案的整行，跳过代码块、标题、表格、命令行。"""
    out: list[str] = []
    in_code = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            # 代码块里的中文注释常是实测结论（例如「# ✅ 实测：kind 集群无需重建」），
            # 保留整行原文，逐字仍可核回语料。
            if s.startswith("#") and len(s) >= 12 and len(re.findall(r"[\u4e00-\u9fff]", s)) >= 4:
                if s not in out:
                    out.append(s)
            continue
        if not s or len(s) < 8:
            continue
        if re.match(r"^#{1,6}\s", s) or s.startswith(("|", "!", "---", "> [")):
            continue
        if re.match(r"^(\$\s|#\s|kubectl\s|docker\s|bash\s|cd\s|\./|python\s|pip\s|git\s)", s):
            continue
        if s not in out:
            out.append(s)
    return out


def candidates_for(row: dict) -> tuple[list[str], str]:
    doc = row["source_doc"]
    hint = row["source_hint"]
    full = CORPUS.get(doc, "")
    section = ws.section_text(full, hint) or ""
    reference_text = norm(section)
    own = extract_sentences(section)
    own.sort(key=conclusion_rank)  # 稳定排序：同分保持原文先后
    other = [s for s in extract_sentences(full) if norm(s) not in reference_text]

    seed = sum(ord(c) for c in row["id"])
    rng = random.Random(seed)
    distractors = rng.sample(other, min(DISTRACTORS, len(other)))
    pool = list(dict.fromkeys(own[:MAX_OWN]))
    for d in distractors:
        if d not in pool:
            pool.insert(rng.randint(0, len(pool)), d)
    if len(pool) < MIN_CANDIDATES:
        extra = [s for s in other if s not in pool]
        pool += extra[: MIN_CANDIDATES - len(pool)]
    return pool, section


# 预标注日志指出 g034 / g035 / g038 的章节句子被抽取规则挡住（标题行、代码块命令、
# 表格行）。这些行本来就存在于冻结语料里，按关键词找回并追加到该题候选末尾；
# 只追加、不改动前面已答 47 题的选项顺序，日志里的索引仍然对得上。
EXTRA_BY_ID = {
    "g034": ("evals/corpus/docker-data-move-to-E.md",
             ("备选 A", "备选 B", "只压缩 vhdx", "让 kind 少占", "RECLAIMABLE", "verify_d3.sh")),
    "g035": ("evals/corpus/docker-data-move-to-E.md",
             ("docker info", "docker images", "kubectl get pods,svc,ingress,pvc",
              "verify_d4.sh", "数据盘应该出现在", "19 项")),
    "g038": ("evals/corpus/resource-ledger.md",
             ("8 vCPU", "8.1 GiB", "Pod 上限", "1006 GiB")),
    # 首轮盲标里两位标注员都指出「候选里没有答案」的题：代码块命令、表格行、
    # 跨行下半句被抽取规则挡住了。按关键词从全文找回，仍是原文逐字。
    "g021": ("evals/corpus/kubectl-cheatsheet.md",
             ("为什么危险", "Service/Ingress 配置问题")),
    "g022": ("evals/corpus/kubectl-cheatsheet.md",
             ("第一原则", "① 看 STATUS", "② 拉到最底", "③ 看上一条")),
    "g023": ("evals/corpus/kubectl-cheatsheet.md",
             ("**liveness** 失败", "**readiness** 失败", "READY` 变 `0/1", "RESTARTS` 涨")),
    "g024": ("evals/corpus/kubectl-cheatsheet.md",
             ("直接读 cgroup", "memory.current", "memory.max", "先量基线")),
    "g025": ("evals/corpus/kubectl-cheatsheet.md",
             ("port-forward 不依赖", "NodePort 的区别", "kubectl port-forward pod/")),
    "g028": ("evals/corpus/env-setup.md",
             ("git 默认读", "注册表里的 `9527` 是活的", "http.proxy=http://127.0.0.1:9527",
              "显式把代理指到")),
    "g029": ("evals/corpus/env-setup.md",
             ("HF_ENDPOINT =", "已写入用户级环境变量", "setx HF_ENDPOINT")),
}


def extra_candidates(rid: str) -> list[str]:
    rule = EXTRA_BY_ID.get(rid)
    if not rule:
        return []
    doc, keys = rule
    out: list[str] = []
    for raw in CORPUS.get(doc, "").splitlines():
        s = raw.strip()
        if s and len(s) >= 6 and any(k in s for k in keys) and s not in out:
            out.append(s)
    return out


def pretty(s: str) -> str:
    """显示用：去掉 Markdown 标记；选中存下来的值仍是原文逐字。"""
    return re.sub(r"[*`]", "", re.sub(r"^(?:>\s*|#\s*)", "", s)).strip()


def build() -> int:
    rows = ws.load_rows()
    quiz: list[dict] = []
    carried: list[str] = []
    pending: list[str] = []

    for row in rows:
        item = {
            "id": row["id"],
            "type": row["type"],
            "question": row["question"],
            "must_refuse": bool(row["must_refuse"]),
            "source": row["source_doc"] or "",
            "hint": row["source_hint"],
            "criteria": row["refusal_criteria"],
            "group": row["source_doc"] or "__refuse__",
            "options": [],
            "values": [],
            "original": "",
            "preselect": [],
            "preselect_refuse": None,
            "enumerative": is_enumerative(row),
        }
        answered = bool(row["reference"])
        if not answered:
            pending.append(row["id"])
        if row["must_refuse"]:
            item["options"] = REFUSE_OPTIONS
            item["values"] = REFUSE_OPTIONS
            if answered and row["reference"] in REFUSE_OPTIONS:
                item["preselect_refuse"] = REFUSE_OPTIONS.index(row["reference"])
                carried.append(row["id"])
        else:
            pool, section = candidates_for(row)
            for e in extra_candidates(row["id"]):
                if e not in pool:
                    pool.append(e)
            item["options"] = [pretty(s) for s in pool]
            item["values"] = pool
            item["original"] = section
            preselect = [a for a in row["reference_atoms"] if a in pool]
            if preselect:
                item["preselect"] = preselect
            if answered:
                carried.append(row["id"])

        quiz.append(item)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(quiz),
        "carried": carried,
        "unverified": [],
        "pending": pending,
        "items": quiz,
        "refuse_correct": REFUSE_CORRECT,
    }
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__QUIZ_DATA__", embedded).replace("__GEN_TIME__", payload["generated_at"])
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUT}（{len(quiz)} 题，单文件 / 零外部依赖）")
    print(f"已从数据集带入 {len(carried)} 题答案 | 待补 {len(pending)} 题：{', '.join(pending) or '无'}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>W6 D3 · 50 题人工标注（网页点选版）</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #ffffff; --ink: #1f2329; --muted: #6b7280;
    --line: #e3e6eb; --accent: #2563eb; --accent-soft: #eff4ff;
    --ok: #15803d; --ok-soft: #ecfdf3; --warn: #b45309; --warn-soft: #fffbeb;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; background: var(--card);
           border-bottom: 1px solid var(--line); padding: 14px 24px; }
  .bar { max-width: 1080px; margin: 0 auto; display: flex; align-items: center;
         gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 650; }
  .count { color: var(--accent); font-weight: 700; font-variant-numeric: tabular-nums; }
  .track { flex: 1 1 200px; height: 8px; background: var(--line); border-radius: 99px; overflow: hidden; }
  .fill { height: 100%; width: 0; background: var(--accent); transition: width .18s ease; }
  button { font: inherit; cursor: pointer; border: 1px solid var(--line); background: var(--card);
           color: var(--ink); padding: 7px 13px; border-radius: 8px; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: #1d4ed8; color: #fff; }
  main { max-width: 1080px; margin: 0 auto; padding: 22px 24px 80px; }
  .notice { background: var(--warn-soft); border: 1px solid #fcd34d; border-radius: 10px;
            padding: 12px 16px; margin-bottom: 18px; font-size: 14px; }
  .notice b { color: var(--warn); }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  details.group { margin-bottom: 16px; }
  details.group > summary { cursor: pointer; font-weight: 650; padding: 10px 14px;
      background: var(--card); border: 1px solid var(--line); border-radius: 10px;
      list-style: none; display: flex; justify-content: space-between; }
  details.group > summary::-webkit-details-marker { display: none; }
  details.group > summary::before { content: "▸"; margin-right: 8px; color: var(--muted); }
  details.group[open] > summary::before { content: "▾"; }
  .grp-count { color: var(--muted); font-weight: 400; font-size: 13px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: 16px 18px; margin: 12px 0 0; }
  .card.done { border-left: 4px solid var(--ok); }
  .qhead { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  .qid { font-weight: 700; color: var(--accent); font-variant-numeric: tabular-nums; }
  .tag { font-size: 12px; color: var(--muted); border: 1px solid var(--line);
         border-radius: 6px; padding: 1px 7px; }
  .qtext { width: 100%; margin: 10px 0 6px; padding: 8px 10px; font: inherit;
           border: 1px solid var(--line); border-radius: 8px; background: #fcfcfd; }
  .qtext:focus { outline: 2px solid var(--accent-soft); border-color: var(--accent); }
  .hintline { color: var(--muted); font-size: 12.5px; margin-bottom: 10px; }
  .opts { display: grid; gap: 7px; }
  .opt { display: flex; gap: 9px; align-items: flex-start; padding: 8px 10px;
         border: 1px solid var(--line); border-radius: 8px; cursor: pointer; background: #fcfcfd; }
  .opt:hover { border-color: var(--accent); }
  .opt.on { background: var(--accent-soft); border-color: var(--accent); }
  .opt input { margin-top: 4px; flex: none; }
  .opt span { font-size: 14px; }
  .picked { margin-top: 10px; padding: 9px 11px; background: var(--ok-soft);
            border-radius: 8px; font-size: 13px; color: #14532d; white-space: pre-wrap; }
  .picked.empty { background: #f9fafb; color: var(--muted); }
  details.raw { margin-top: 10px; }
  details.raw > summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  pre.rawbox { max-height: 260px; overflow: auto; background: #f8f9fb; border: 1px solid var(--line);
               border-radius: 8px; padding: 10px; font-size: 12.5px; white-space: pre-wrap; }
  .criteria { background: var(--warn-soft); border-radius: 8px; padding: 9px 11px;
              font-size: 13px; color: #78350f; margin: 8px 0 10px; }
  footer { position: fixed; bottom: 0; left: 0; right: 0; background: var(--card);
           border-top: 1px solid var(--line); padding: 10px 24px; }
  .foot { max-width: 1080px; margin: 0 auto; display: flex; gap: 10px;
          align-items: center; flex-wrap: wrap; }
  .foot .spacer { flex: 1; }
  .msg { color: var(--muted); font-size: 13px; }
  @media print { header, footer { position: static; } }
</style>
</head>
<body>
<header><div class="bar">
  <h1>W6 D3 · 50 题人工标注</h1>
  <span class="count" id="cnt">0 / 50</span>
  <div class="track"><div class="fill" id="fill"></div></div>
  <button onclick="expandAll(true)">全部展开</button>
  <button onclick="expandAll(false)">全部收起</button>
</div></header>

<main>
  <div class="notice">
    <b>怎么答：</b>下面每个候选句都是从冻结语料原文里摘出来的，你勾选的句子拼起来就是该题的标准答案。
    答案由你点选，脚本不生成任何答案文字。同一章节的句子都在候选里（结论性句子排在前面），
    另有几句来自本篇其他章节作干扰项；想看上下文就点「看本章原文」。
    题干可以直接改；不改就按草案确认。拒答题选「应说明未覆盖并拒答」。
    <b>所有改动自动存在本机浏览器里</b>，关掉页面也不会丢，可以分批答。
  </div>
  <div class="meta" id="meta">生成时间：__GEN_TIME__</div>
  <div id="groups"></div>
</main>

<footer><div class="foot">
  <button class="primary" onclick="exportJSON()">导出答案 JSON</button>
  <button onclick="copyJSON()">复制答案 JSON</button>
  <button onclick="importJSON()">导入上次答案</button>
  <span class="spacer"></span>
  <span class="msg" id="msg"></span>
  <button onclick="clearAll()">清空</button>
</div></footer>

<input type="file" id="fileIn" accept=".json,application/json" hidden>
<script>
const DATA = __QUIZ_DATA__;
const KEY = "w6d3_quiz_answers_v1";
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }

function save() {
  localStorage.setItem(KEY, JSON.stringify(state));
  refresh();
}
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function labelOf(id) {
  const it = DATA.items.find(x => x.id === id);
  return it ? it.group : "__other__";
}
function groupName(g) {
  const names = {
    "evals/corpus/decisions.md": "一、决策日志 decisions.md",
    "evals/corpus/k8s-concepts.md": "二、K8s 概念 k8s-concepts.md",
    "evals/corpus/kubectl-cheatsheet.md": "三、kubectl 速查表",
    "evals/corpus/env-setup.md": "四、本机环境 env-setup.md",
    "evals/corpus/docker-data-move-to-E.md": "五、Docker 数据盘迁移",
    "evals/corpus/resource-ledger.md": "六、资源账本 resource-ledger.md",
    "__refuse__": "七、应该拒答（语料外）"
  };
  return names[g] || g;
}
function initial(id) {
  const s = state[id];
  if (s) return s;
  const it = DATA.items.find(x => x.id === id);
  return { question: it.question, picks: it.preselect || [],
           refuse: it.preselect_refuse === undefined ? null : it.preselect_refuse };
}
function answered(s, it) {
  if (it.must_refuse) return s.refuse !== null && s.refuse !== undefined;
  return Array.isArray(s.picks) && s.picks.length > 0;
}
function cardHTML(it) {
  const s = initial(it.id);
  let opts = "";
  if (it.must_refuse) {
    opts = it.options.map((o, i) =>
      `<label class="opt ${s.refuse === i ? "on" : ""}"><input type="radio" name="r_${it.id}" ` +
      `${s.refuse === i ? "checked" : ""} onchange="setRefuse('${it.id}', ${i})"><span>${esc(o)}</span></label>`
    ).join("");
  } else {
    opts = it.options.map((o, i) => {
      const v = it.values[i];
      const on = (s.picks || []).includes(v);
      return `<label class="opt ${on ? "on" : ""}"><input type="checkbox" ${on ? "checked" : ""} ` +
             `onchange="toggle('${it.id}', ${i}, this.checked)"><span>${esc(o)}</span></label>`;
    }).join("");
  }
  const criteria = it.must_refuse
    ? `<div class="criteria">判据草案：${esc(it.criteria)}</div>` : "";
  const raw = it.original
    ? `<details class="raw"><summary>看本章原文（${esc(it.hint.replace(/^#+\s*/, ""))}）</summary>` +
      `<pre class="rawbox">${esc(it.original)}</pre></details>` : "";
  return `<div class="card ${answered(s, it) ? "done" : ""}" id="card_${it.id}">
    <div class="qhead"><span class="qid">${it.id}</span>
      <span class="tag">${esc(it.type)}</span>
      <span class="tag">${it.must_refuse ? "应拒答" : "可答题"}</span></div>
    <input class="qtext" value="${esc(s.question)}" oninput="setQuestion('${it.id}', this.value)"
           placeholder="题干（改成真实问法，不改就留着草案）">
    <div class="hintline">${it.must_refuse ? "没有原文候选，选正确的期望行为" : "候选句均来自冻结语料原文，可多选"}</div>
    ${criteria}
    <div class="opts">${opts}</div>
    <div class="picked ${answered(s, it) ? "" : "empty"}" id="picked_${it.id}"></div>
    ${raw}
  </div>`;
}
function render() {
  const groups = {};
  DATA.items.forEach(it => { (groups[it.group] = groups[it.group] || []).push(it); });
  const order = Object.keys(groups).sort();
  document.getElementById("groups").innerHTML = order.map(g =>
    `<details class="group" open><summary><span>${esc(groupName(g))}</span>` +
    `<span class="grp-count" id="gc_${esc(g).replace(/[^a-zA-Z0-9]/g, "_")}"></span></summary>` +
    groups[g].map(cardHTML).join("") + `</details>`
  ).join("");
  document.getElementById("meta").textContent =
    "生成时间：" + DATA.generated_at +
    (DATA.carried.length ? " ｜ 已从数据集带入 " + DATA.carried.length + " 题答案" : "") +
    (DATA.pending && DATA.pending.length ? " ｜ 待补：" + DATA.pending.join("、") : "");
  refresh();
}
function refresh() {
  let n = 0;
  DATA.items.forEach(it => {
    const s = state[it.id] || initial(it.id);
    if (answered(s, it)) n++;
    const card = document.getElementById("card_" + it.id);
    if (card) card.classList.toggle("done", answered(s, it));
    const box = document.getElementById("picked_" + it.id);
    if (box) {
      const text = it.must_refuse
        ? (s.refuse === undefined || s.refuse === null ? "未作答" : "期望行为：" + it.options[s.refuse])
        : ((s.picks || []).length ? "已选 " + s.picks.length + " 句（将拼成标准答案）：\n" +
           s.picks.map((p, i) => (i + 1) + ". " + p).join("\n") : "未作答");
      box.textContent = text;
      box.classList.toggle("empty", !answered(s, it));
    }
  });
  document.getElementById("cnt").textContent = n + " / " + DATA.total;
  document.getElementById("fill").style.width = (n / DATA.total * 100) + "%";
  document.querySelectorAll(".grp-count").forEach(el => {
    const d = el.closest("details");
    const cards = d.querySelectorAll(".card");
    let done = 0;
    cards.forEach(c => { if (c.classList.contains("done")) done++; });
    el.textContent = done + " / " + cards.length + " 已答";
  });
}
function setQuestion(id, v) {
  const s = state[id] || initial(id); s.question = v; state[id] = s; save();
}
function toggle(id, i, on) {
  const it = DATA.items.find(x => x.id === id);
  const s = state[id] || initial(id);
  const v = it.values[i];
  s.picks = (s.picks || []).filter(x => x !== v);
  if (on) s.picks.push(v);
  state[id] = s; save();
  const card = document.getElementById("card_" + id);
  card.querySelectorAll(".opt").forEach((el, k) => {
    if (k === i) el.classList.toggle("on", on);
  });
}
function setRefuse(id, i) {
  const s = state[id] || initial(id); s.refuse = i; state[id] = s; save();
  const card = document.getElementById("card_" + id);
  card.querySelectorAll(".opt").forEach((el, k) => el.classList.toggle("on", k === i));
}
function payload() {
  const answers = {};
  DATA.items.forEach(it => {
    const s = state[it.id];
    if (!s || !answered(s, it)) return;
    answers[it.id] = {
      question: s.question,
      question_changed: s.question !== it.question,
      must_refuse: it.must_refuse,
      reference: it.must_refuse ? it.options[s.refuse] : (s.picks || []).join("\n"),
      atoms: it.must_refuse ? [] : (s.picks || []).slice(),
      refuse_index: it.must_refuse ? s.refuse : undefined,
      criteria: it.criteria
    };
  });
  return { source: "w6d3_quiz", exported_at: new Date().toISOString(), answers: answers };
}
function exportJSON() {
  const p = payload();
  const blob = new Blob([JSON.stringify(p, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "w6d3_quiz_answers.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  document.getElementById("msg").textContent = "已导出 " + Object.keys(p.answers).length + " 题";
}
function copyJSON() {
  const text = JSON.stringify(payload(), null, 2);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(
      () => document.getElementById("msg").textContent = "已复制到剪贴板",
      () => fallbackCopy(text)
    );
  } else { fallbackCopy(text); }
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.left = "-9999px";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); document.getElementById("msg").textContent = "已复制到剪贴板"; }
  catch (e) { document.getElementById("msg").textContent = "复制失败，请改用「导出答案 JSON」"; }
  document.body.removeChild(ta);
}
function importJSON() { document.getElementById("fileIn").click(); }
document.getElementById("fileIn").addEventListener("change", ev => {
  const f = ev.target.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    try {
      const obj = JSON.parse(r.result);
      const ans = obj.answers || {};
      Object.keys(ans).forEach(id => {
        const a = ans[id], it = DATA.items.find(x => x.id === id);
        if (!it) return;
        let ri = (a.refuse_index === undefined || a.refuse_index === null) ? null : a.refuse_index;
        if (it.must_refuse && ri === null && a.reference) {
          const idx = it.options.indexOf(a.reference);
          if (idx >= 0) ri = idx;
        }
        state[id] = { question: a.question || it.question, picks: a.atoms || [], refuse: ri };
      });
      save(); render();
      document.getElementById("msg").textContent = "已导入 " + Object.keys(ans).length + " 题";
    } catch (e) { document.getElementById("msg").textContent = "导入失败：不是合法的答案 JSON"; }
  };
  r.readAsText(f);
});
function expandAll(open) { document.querySelectorAll("details.group").forEach(d => d.open = open); }
function clearAll() {
  if (!confirm("清空本机保存的全部作答？不会改动已导出的文件。")) return;
  state = {}; localStorage.removeItem(KEY); render();
  document.getElementById("msg").textContent = "已清空";
}
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(build())
