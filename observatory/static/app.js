/* =============================================================================
   K8s 观测台 · 前端
   -----------------------------------------------------------------------------
   职责只有三件：
     1. 每 1.5 秒拉一次 /api/state，把集群真实状态画成卡片
     2. 点按钮 → POST /api/action/<name> → 把结果写进操作时间线
     3. **把「变化」演出来** —— 新 Pod 闪一下、重启数跳一下、变红变绿有过渡

   第 3 点是最重要的。一个静态的状态表没有价值 ——
   用户需要「看见它从绿变红」，那是理解 K8s 的唯一途径。
   所以下面写了不少 diff 逻辑。
   ============================================================================= */

'use strict';

const POLL_MS = 1500;

// 上一次的快照，用来做 diff（判断谁是「新出现的 Pod」、谁的 RESTARTS 涨了）
let prevPods = new Map();
let prevRestarts = new Map();
let actionMeta = new Map();   // name -> {label, explain, danger}
let busy = false;
let failStreak = 0;

// 可切换的观测目标。boot 时从 /api/targets 拉一次，之后不再轮询 ——
// 目标清单（有几个 ns、几个 Deployment）在演示过程中基本不变，
// 每次轮询都去 kubectl get deploy -A 是纯浪费。
let targets = [];
let currentNs = '';
let currentDeploy = '';
let lastRelFetch = 0;   // 上一次拉 /api/releases 的时间戳（节流用）

// --- 小工具 ------------------------------------------------------------------

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtAge(sec) {
  if (!sec && sec !== 0) return '–';
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + 'm' + (sec % 60 ? (sec % 60) + 's' : '');
  const h = Math.floor(m / 60);
  return h + 'h' + (m % 60) + 'm';
}

// --- 渲染 --------------------------------------------------------------------

function renderSummary(state) {
  const d = state.deployment || {};
  const desired = d.replicas ?? '–';
  const ready = d.readyReplicas ?? 0;
  const eps = state.endpointReadyCount ?? 0;
  const totalRestarts = (state.pods || []).reduce((a, p) => a + (p.restarts || 0), 0);

  $('v-desired').textContent = desired;
  $('v-ready').textContent = ready;
  $('v-endpoints').textContent = eps;
  $('v-restarts').textContent = totalRestarts;

  // 汇总条也上色：用户扫一眼就知道整体健康度
  $('s-ready').className = 'stat ' + (ready === desired ? 'good' : 'bad');
  $('s-endpoints').className = 'stat ' + (eps > 0 ? 'good' : 'bad');
  $('s-restarts').className = 'stat ' + (totalRestarts === 0 ? 'good' : 'bad');
  $('s-desired').className = 'stat';
}

function podClass(p) {
  const phase = (p.phase || '').toLowerCase();
  if (phase === 'succeeded' || phase === 'failed') return 'terminating';
  if (phase === 'pending') return 'pending';
  if (!p.readyBool) return 'notready';
  return 'ready';
}

function renderPods(state) {
  const pods = state.pods || [];
  const box = $('pods');
  $('pod-count').textContent = `(${pods.length})`;

  if (!pods.length) {
    box.innerHTML = '<div class="empty">当前没有 Pod —— 如果刚点过「缩到 0 副本」或「删 Pod」，稍等几秒。</div>';
    return;
  }

  // 按名字排序，让卡片位置稳定（否则每次刷新都跳来跳去没法看）
  const sorted = [...pods].sort((a, b) => a.name.localeCompare(b.name));

  box.innerHTML = sorted.map(p => {
    const cls = podClass(p);
    const isNew = !prevPods.has(p.name);
    const restartDelta = (p.restarts || 0) - (prevRestarts.get(p.name) ?? (p.restarts || 0));
    const restartFlash = restartDelta > 0 ? ' restart-flash' : '';

    // 顶部状态徽章：优先显示「坏事情」——坏的必须先被看见
    let badge, badgeCls = 'ok';
    if (p.imagePullError) {
      badge = esc(p.stateReason || 'ImagePullError'); badgeCls = 'bad';
    } else if (p.phase === 'Pending') {
      badge = 'Pending'; badgeCls = 'warn';
    } else if (!p.readyBool) {
      badge = '0/1 不接流量'; badgeCls = 'bad';
    } else {
      badge = '1/1 Ready'; badgeCls = 'ok';
    }

    // 报警块：只在有问题时出现，并给出「下一步该看什么」
    let alert = '';
    if (p.imagePullError) {
      alert = `<div class="pod-alert">拉不到镜像：${esc(p.stateReason)}
        <span class="hint">镜像 tag 是否存在？kind 集群要先 kind load 才能取到本地镜像。</span></div>`;
    } else if (!p.readyBool && p.phase === 'Running') {
      const rf = p.probeFailures || {};
      const which = rf.readiness > 0 ? 'readinessProbe'
                  : rf.startup > 0 ? 'startupProbe'
                  : 'readinessProbe（尚未产生事件，稍等几秒）';
      alert = `<div class="pod-alert">容器在跑，但 <b>${which} 失败</b> → 已被摘出 EndpointSlice，
        Service 不会把流量发给它。<b>注意 RESTARTS 没有增长 —— readiness 失败不重启容器。</b>
        <span class="hint">对比：liveness 失败才会让 RESTARTS 增长。</span></div>`;
    } else if (p.exitCode === 137 || p.stateReason === 'OOMKilled') {
      alert = `<div class="pod-alert">容器被 OOMKilled（退出码 137）
        <span class="hint">${esc(p.exitHint || '内存 limit 可能低于应用实际需求')}</span></div>`;
    } else if (p.exitCode === 143) {
      alert = `<div class="pod-alert warn">上次是被优雅终止（143）
        <span class="hint">通常是滚动更新或人工删除，不一定是故障。</span></div>`;
    }

    const pf = p.probeFailures || {};
    const probeCell = (n, label) =>
      `<div class="probe ${n > 0 ? 'hit' : ''}"><div class="n">${n}</div><div class="l">${label}</div></div>`;

    return `
      <div class="pod ${cls}${isNew ? ' is-new' : ''}">
        <div class="pod-head">
          <span class="pod-name" title="${esc(p.name)}">${esc(p.name)}</span>
          <span class="badge ${badgeCls}">${badge}</span>
        </div>
        <div class="pod-rows">
          <div class="row"><span class="k">READY</span>
            <span class="v ${p.readyBool ? 'ok' : 'bad'}">${esc(p.ready)}</span></div>
          <div class="row"><span class="k">STATUS</span>
            <span class="v ${p.phase === 'Running' ? '' : 'bad'}">${esc(p.phase)}${p.stateReason ? ' / ' + esc(p.stateReason) : ''}</span></div>
          <div class="row"><span class="k">RESTARTS</span>
            <span class="v ${p.restarts > 0 ? 'bad' : ''}${restartFlash}">${p.restarts}${restartDelta > 0 ? ' ↑+' + restartDelta : ''}</span></div>
          <div class="row"><span class="k">AGE</span><span class="v">${fmtAge(p.ageSeconds)}</span></div>
          <div class="row"><span class="k">IP</span><span class="v">${esc(p.ip || '–')}</span></div>
        </div>
        ${alert}
        <div class="probes">
          ${probeCell(pf.startup || 0, 'startup')}
          ${probeCell(pf.liveness || 0, 'liveness')}
          ${probeCell(pf.readiness || 0, 'readiness')}
        </div>
      </div>`;
  }).join('');
}

function renderEndpoints(state) {
  const eps = state.endpoints || [];
  const box = $('eps');

  if (!eps.length) {
    box.innerHTML = '<div class="ep-empty">EndpointSlice 为空 —— 没有任何 Pod 准备好接流量。'
      + 'Service 不会报错，它只是没有后端可用（请求会失败或被拒绝）。</div>';
    return;
  }

  box.innerHTML = eps.map(e => `
    <div class="ep ${e.ready ? 'on' : 'off'}">
      <span class="mark"></span>
      <span class="ip">${esc(e.ip)}</span>
      <span style="margin-left:auto;color:var(--text-3)">${esc(e.target)}</span>
      <span style="color:${e.ready ? 'var(--ok)' : 'var(--bad)'}">
        ${e.ready ? 'ready' : 'notReady'}</span>
    </div>`).join('');
}

function renderActions(actions) {
  const box = $('actions');
  // ⚠️ 曾经的 bug：这里写过「只渲染一次」的守卫（dataset.ready === '1'）。
  //    但按钮清单是**按目标来源过滤**的 —— 切到裸清单目标应该亮出
  //    「恢复默认清单」、收起两个 Helm 按钮，切到 Helm 目标反之。
  //    一次性守卫让切换目标后按钮永远停在第一套：在 default 目标上
  //    点 Helm 按钮只会拿到一条 helm 报错，而「恢复默认清单」反而不见了。
  //    改成「清单没变才跳过」。照样不会每轮重建（loadActions 本来就只在
  //    boot 和切换目标时调用），但切换目标后按钮列表跟着变。
  const sig = actions.map(a => a.name).join(',');
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = actions.map(a => `
    <button class="btn ${a.danger ? 'danger' : ''} ${a.name === 'restore' ? 'safe' : ''}"
            data-action="${esc(a.name)}">
      <span class="lbl">${esc(a.label)}</span>
      <span class="exp">${esc(a.explain)}</span>
    </button>`).join('');

  box.querySelectorAll('button[data-action]').forEach(btn => {
    btn.addEventListener('click', () => runAction(btn.dataset.action, btn));
  });
}

// --- 操作执行 ----------------------------------------------------------------

async function runAction(name, btn) {
  if (busy) return;
  busy = true;

  const allButtons = document.querySelectorAll('#actions button');
  allButtons.forEach(b => b.disabled = true);

  const originalHTML = btn.innerHTML;
  btn.innerHTML = '<span class="lbl"><span class="spin"></span> 执行中…</span>';

  try {
    const resp = await fetch('/api/action/' + encodeURIComponent(name), { method: 'POST' });
    const data = await resp.json();
    pushTimeline(data);
    // 操作后立刻刷一次状态，不用等下一个轮询周期 —— 让反馈更快
    await tick();
  } catch (err) {
    pushTimeline({
      label: name, at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      ok: false, summary: '请求失败：' + err.message, commands: [],
    });
  } finally {
    btn.innerHTML = originalHTML;
    allButtons.forEach(b => b.disabled = false);
    busy = false;
  }
}

// 操作日志条目的稳定 key —— 用于时间线去重。同一动作在同一秒内执行两次
// 才会碰撞，而这些动作都远慢于 1 秒，可以接受。
function tlKey(e) {
  return (e.at || '') + '|' + (e.action || '') + '|' + (e.summary || '');
}

function pushTimeline(entry) {
  const box = $('timeline');
  const empty = $('tl-empty');
  if (empty) empty.remove();

  const el = document.createElement('div');
  el.className = 'tl' + (entry.ok ? '' : ' err');
  el.dataset.key = tlKey(entry);

  const cmds = (entry.commands || [])
    .map(c => `<code class="cmd">$ ${esc(c)}</code>`).join('');

  el.innerHTML = `
    <div class="head">
      <span class="time">${esc(entry.at || '')}</span>
      <span class="title">${entry.ok ? '' : '✕ '}${esc(entry.label || entry.action || '')}</span>
    </div>
    <div class="summary">${esc(entry.summary || '')}</div>
    ${cmds}
    ${entry.watchFor ? `<div class="watch">接下来看什么：${esc(entry.watchFor)}</div>` : ''}`;

  box.prepend(el);
  // 最多留 12 条，防止页面越滚越长
  while (box.children.length > 12) box.removeChild(box.lastChild);
}

// --- 目标切换 ----------------------------------------------------------------

function targetLabel(t) {
  // tab 上显示「ns / deployment」。Helm 托管的目标再加一个角标，
  // 让用户一眼看出哪个是 Chart 化之后的环境（W2 D6 的成果）。
  const tag = t.source === 'chart'
    ? `<span class="tag">Helm${t.release ? ' · ' + esc(t.release) : ''}</span>`
    : `<span class="tag">裸清单</span>`;
  return `<span class="ns">${esc(t.ns)}</span>/${esc(t.deployment)}${tag}`;
}

function renderTargets(state) {
  const ns = state && state.target ? state.target.ns : '';
  const deploy = state && state.target ? state.target.deployment : '';

  const box = $('sw-tabs');
  if (!box) return;

  // ⚠️ 刻意做成「只在切换时重建 DOM」：这函数每 1.5 秒被调一次，
  //    如果每次都 innerHTML 重画，用户手指正按在某个 tab 上时它会凭空消失。
  //    所以只在「当前目标变了」或「清单第一次到」时才重画。
  const sig = ns + '|' + deploy + '|' + targets.length;
  const sub = state && state.target
    ? (state.target.source === 'chart'
        ? ` · Helm release ${state.release ? state.release.name : ''} rev ${state.release ? state.release.revision : ''}`
        : ' · 裸清单')
    : '';
  // 副标题每轮都更新（rev 号会变），但 tab 条不重建。
  $('target').textContent = ns && deploy ? `${ns}/${deploy}${sub}` : '';

  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  currentNs = ns;
  currentDeploy = deploy;

  box.innerHTML = targets.map(t => {
    const active = (t.ns === ns && t.deployment === deploy);
    return `<button class="sw-tab${active ? ' active' : ''}"
              data-ns="${esc(t.ns)}" data-deploy="${esc(t.deployment)}">${targetLabel(t)}</button>`;
  }).join('');

  box.querySelectorAll('.sw-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTarget(btn));
  });
}

async function switchTarget(btn) {
  const t = targets.find(x => x.ns === btn.dataset.ns && x.deployment === btn.dataset.deploy);
  if (!t) return;
  try {
    const resp = await fetch('/api/target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(t),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || ('HTTP ' + resp.status));
    // 切换成功后立刻拉一次新目标的状态，并把操作按钮列表也刷一遍 ——
    // 不同作用域的按钮是不同的（Helm 目标不会看到「恢复默认清单」）。
    // ⚠️ 还要清零发布历史的节流计时（lastRelFetch）：否则 5 秒内
    //    renderReleases 会直接 return，面板挂着**上一个目标**的数据 ——
    //    切到 dev 却显示 prod 的 release 名，比不显示更误导人。
    lastRelFetch = 0;
    await Promise.all([loadActions(), tick()]);
  } catch (err) {
    const b = $('error');
    b.textContent = '切换观测目标失败：' + err.message;
    b.style.display = 'block';
  }
}

async function loadTargets() {
  try {
    const resp = await fetch('/api/targets', { cache: 'no-store' });
    const data = await resp.json();
    targets = data.targets || [];
    renderTargets(null);
  } catch (err) {
    // 目标列表拉不到不该让整个页面变砖 —— 顶部显示一条提示就行，
    // 观测本身（Pod 卡片等）继续照常工作。
    const box = $('sw-tabs');
    if (box) box.innerHTML = `<span class="rel-note">目标列表加载失败：${esc(err.message)}</span>`;
  }
}

// --- Helm 发布历史 ----------------------------------------------------------

function renderReleases(state) {
  // 只在当前目标是 Helm 托管时才显示这一节。
  // W1 的 default 目标没有 release，显示一个空表格反而让人以为出错了。
  const sec = $('rel-sec');
  if (!sec) return;
  if (!state.target || state.target.source !== 'chart') {
    sec.style.display = 'none';
    return;
  }
  sec.style.display = '';

  // ⚠️ 节流：helm list -A + 每个 release 各跑一次 helm history，
  //    3 个 release 就是 4 个子进程。每 1.5 秒跑一轮纯属浪费，
  //    而且轮询本身就忙不过来。发布历史没必要秒级刷新 —— 5 秒足够。
  const now = Date.now();
  if (now - lastRelFetch < 5000) return;
  lastRelFetch = now;

  fetch('/api/releases', { cache: 'no-store' })
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        $('releases').innerHTML = `<div class="rel-note">读取 Helm 历史失败：${esc(data.error)}</div>`;
        return;
      }
      const rels = (data.releases || []).filter(r => r.namespace === currentNs);
      if (!rels.length) {
        $('releases').innerHTML =
          `<div class="rel-note">namespace ${esc(currentNs)} 下没有 Helm release。</div>`;
        return;
      }
      $('releases').innerHTML = rels.map(rel => relHtml(rel, state)).join('');
    })
    .catch(err => {
      $('releases').innerHTML = `<div class="rel-note">读取 Helm 历史失败：${esc(err.message)}</div>`;
    });
}

function relHtml(rel, state) {
  const curRev = (state.release && state.release.revision) || rel.revision;
  const hist = (rel.history || []).slice().sort((a, b) => Number(b.revision) - Number(a.revision));
  const rows = hist.map(h => {
    const rev = Number(h.revision);
    const isCur = rev === Number(curRev);
    const st = String(h.status || '').toLowerCase();
    // description 里那一长串（尤其是 failed 的）截断到 96 字符，
    // 完整内容悬停就能看到 —— 表格里放几百字的报错会把版面撑爆。
    const desc = String(h.description || '');
    return `<tr class="${isCur ? 'cur' : ''}">
      <td class="rev">${rev}${isCur ? ' ←' : ''}</td>
      <td><span class="st ${esc(st)}">${esc(h.status || '')}</span></td>
      <td class="when">${esc(fmtHelmTime(h.updated))}</td>
      <td class="desc" title="${esc(desc)}">${esc(desc.length > 96 ? desc.slice(0, 96) + '…' : desc)}</td>
    </tr>`;
  }).join('');

  return `<div class="rel${state.release && rel.name === state.release.name ? ' is-current' : ''}">
    <div class="rel-head">
      <span class="rel-name">${esc(rel.name)}</span>
      <span class="rel-ns">${esc(rel.namespace)}</span>
      <span class="st ${esc(String(rel.status || '').toLowerCase())}">${esc(rel.status || '')}</span>
      <span class="rel-chart">${esc(rel.chart || '')} · app ${esc(rel.app_version || '')}</span>
    </div>
    <table class="revs">
      <thead><tr><th>Rev</th><th>状态</th><th>时间</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="rel-note">
      最新 revision 的 DESCRIPTION 会原样记录失败原因 —— 这正是
      「helm 怎么帮我记住每一次发布」的证据。点上面的「发一个坏版本」可以亲手造一条
      <b>failed</b> 记录，再用「回滚到上一个可用版本」修好它。
    </div>
  </div>`;
}

// helm history 的 updated 形如 "2026-10-12 14:46:04.6927562 +0800 CST"
// 页面只想要能一眼扫的时间 → 去掉秒以下的小数和时区尾巴。
function fmtHelmTime(s) {
  if (!s) return '';
  return String(s).split('.')[0].replace(/\s*\+\d{4}\s*\w+$/, '');
}

// --- 轮询 --------------------------------------------------------------------

async function tick() {
  try {
    const resp = await fetch('/api/state', { cache: 'no-store' });
    const state = await resp.json();

    if (!resp.ok || state.error) {
      throw new Error(state.error || ('HTTP ' + resp.status));
    }

    failStreak = 0;
    $('dot').className = 'dot live';
    $('conn-text').textContent = '已连接';
    $('error').style.display = 'none';

    renderSummary(state);
    renderPods(state);
    renderEndpoints(state);
    // 目标切换栏：高亮当前 ns，并把顶栏副标题改成「ns/deploy · release rev N」
    renderTargets(state);
    // Helm 发布历史：只在 Helm 目标下渲染
    renderReleases(state);
    // 显示后端给的本地时间。之前这里显示的是 UTC —— 比墙上时间慢 8 小时，
    // 而同一页的操作时间线用的是本地时间，导致同屏两个时钟不一致。
    // 后端已改为本地时间，前端顺带把「最后刷新」四个字标出来，避免被误读成别的。
    $('tick').textContent = '· 刷新于 ' + (state.timestamp || '');

    // 存快照供下轮 diff
    prevPods = new Set((state.pods || []).map(p => p.name));
    prevRestarts = new Map((state.pods || []).map(p => [p.name, p.restarts || 0]));

  } catch (err) {
    failStreak++;
    $('dot').className = 'dot dead';
    $('conn-text').textContent = '连接失败（' + failStreak + '）';
    // 连续失败 2 次以上才弹横幅，避免一次抖动就吓人
    if (failStreak >= 2) {
      const b = $('error');
      b.textContent = '读不到集群状态：' + err.message;
      b.style.display = 'block';
    }
  }
}

async function loadActions() {
  try {
    const resp = await fetch('/api/actions', { cache: 'no-store' });
    const data = await resp.json();
    renderActions(data.actions || []);
    // ⚠️ 去重后再推：服务端 log 是全量的，而 loadActions 每逢切换目标就会
    //    调一次。原来直接 forEach(pushTimeline)，每切一次目标就把整段历史
    //    重复一遍（实测 4 条 → 8 条 → 12 条，直到撞上 12 条上限），
    //    同一条操作在页面上出现三四次。用 key 跳过已展示过的条目 ——
    //    本地刚执行的动作服务端也记了，不会漏。
    const seen = new Set(
      Array.from(document.querySelectorAll('#timeline .tl'))
        .map(e => e.dataset.key)
        .filter(Boolean)
    );
    (data.log || []).slice().reverse().forEach(e => {
      const key = tlKey(e);
      if (seen.has(key)) return;
      seen.add(key);
      pushTimeline(e);
    });
  } catch (err) {
    $('actions').innerHTML =
      `<div class="empty">无法加载操作列表：${esc(err.message)}</div>`;
  }
}

// --- 启动 --------------------------------------------------------------------

// 先记录一次初始状态，这样第一帧不会把所有 Pod 都当成「新出现」而全闪一遍
(async function boot() {
  try {
    const resp = await fetch('/api/state', { cache: 'no-store' });
    const state = await resp.json();
    prevPods = new Set((state.pods || []).map(p => p.name));
    prevRestarts = new Map((state.pods || []).map(p => [p.name, p.restarts || 0]));
  } catch (e) { /* 首次失败无所谓，tick 会报 */ }

  await loadTargets();   // 目标清单只拉一次，见 targets 变量处的注释
  await loadActions();
  await tick();
  setInterval(tick, POLL_MS);
})();
