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
  if (box.dataset.ready === '1') return;   // 只渲染一次，避免每次轮询重建导致闪烁
  box.innerHTML = actions.map(a => `
    <button class="btn ${a.danger ? 'danger' : ''} ${a.name === 'restore' ? 'safe' : ''}"
            data-action="${esc(a.name)}">
      <span class="lbl">${esc(a.label)}</span>
      <span class="exp">${esc(a.explain)}</span>
    </button>`).join('');
  box.dataset.ready = '1';

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

function pushTimeline(entry) {
  const box = $('timeline');
  const empty = $('tl-empty');
  if (empty) empty.remove();

  const el = document.createElement('div');
  el.className = 'tl' + (entry.ok ? '' : ' err');

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
    (data.log || []).slice().reverse().forEach(pushTimeline);
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

  await loadActions();
  await tick();
  setInterval(tick, POLL_MS);
})();
