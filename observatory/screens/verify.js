// 最终验证：按钮按目标切换 / 发布面板即时 / 时间线不重复 / delete-pod 在 Helm 目标可用
const { chromium } = require('playwright');

const R = [];
function check(name, cond, extra) {
  R.push((cond ? 'PASS' : 'FAIL') + '  ' + name + (extra !== undefined ? '  → ' + extra : ''));
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));

  const btnLabels = () => page.$$eval('#actions button .lbl', els => els.map(e => e.textContent.trim()));
  const tlCount = () => page.$$eval('#timeline .tl', els => els.length);
  const podNames = () => page.$$eval('#pods .pod .pod-name', els => els.map(e => e.textContent.trim()));
  const subtitle = () => page.textContent('#target');
  const clickTab = async (want) => {
    const tabs = await page.$$('#sw-tabs .sw-tab');
    for (const t of tabs) {
      const label = (await t.textContent()) || '';
      if (label.includes(want)) { await t.click(); return true; }
    }
    return false;
  };
  const waitTarget = async (want) => {   // 等到顶栏副标题真的是这个目标（避开轮询 lag）
    for (let i = 0; i < 12; i++) {
      if (((await subtitle()) || '').startsWith(want + '/')) return true;
      await page.waitForTimeout(500);
    }
    return false;
  };

  await page.goto('http://127.0.0.1:8899', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#sw-tabs .sw-tab', { timeout: 15000 });
  await page.waitForTimeout(3000);

  // 1) 显式切到 default（服务端目标可能是上次留下的），验证裸清单按钮集
  check('点击 default tab', await clickTab('default'));
  check('default 目标已生效', await waitTarget('default'), ((await subtitle()) || '').replace(/\s+/g, ' ').slice(0, 50));
  const defBtns = await btnLabels();
  check('default 有「恢复默认清单」', defBtns.includes('恢复默认清单'), defBtns.length + ' 个按钮');
  check('default 没有 Helm 按钮', !defBtns.some(b => b.includes('坏版本') || b.includes('回滚')));

  // 2) 切 dev：Helm 按钮出现、restore 消失、发布面板即时显示 llmops-dev
  check('点击 dev tab', await clickTab('dev'));
  check('dev 目标已生效', await waitTarget('dev'));
  await page.waitForTimeout(1200);   // 低于旧 5s 节流窗口
  const devBtns = await btnLabels();
  check('dev 有「发一个坏版本」', devBtns.some(b => b.includes('坏版本')));
  check('dev 有「回滚到上一个可用版本」', devBtns.some(b => b.includes('回滚到上一个可用版本')));
  check('dev 没有「恢复默认清单」', !devBtns.includes('恢复默认清单'));
  const relText = (await page.textContent('#rel-sec')) || '';
  check('dev 面板显示 llmops-dev（不是 prod）', relText.includes('llmops-dev') && !relText.includes('llmops-prod'));
  const tl1 = await tlCount();

  // 3) 切 prod 再切回 dev：时间线不重复
  check('点击 prod tab', await clickTab('prod'));
  check('prod 目标已生效', await waitTarget('prod'));
  check('点击 dev tab（回切）', await clickTab('dev'));
  check('dev 目标再次生效', await waitTarget('dev'));
  const tl2 = await tlCount();
  check('时间线不因切换目标而重复', tl2 === tl1, tl1 + ' → ' + tl2 + ' 条');

  // 4) delete-pod 在 dev 上真能跑（选择器修复 + 自愈）
  const before = await podNames();
  await page.click('button[data-action="delete-pod"]');
  await page.waitForTimeout(7000);
  const after = await podNames();
  const tlTop = await page.$eval('#timeline .tl',
    e => (e.querySelector('.title').textContent.trim()) + ' :: ' +
         (e.querySelector('.summary').textContent.trim().slice(0, 80))).catch(() => '?');
  const tlOk = await page.$eval('#timeline .tl', e => !e.className.includes('err')).catch(() => false);
  check('delete-pod 在 dev 上执行成功', tlOk, tlTop);
  check('Pod 已换新（自愈）', JSON.stringify(before) !== JSON.stringify(after),
    before.join(',') + ' → ' + after.join(','));

  check('全程无 JS 报错', errors.length === 0, errors.join('; ') || '(none)');

  console.log(R.join('\n'));
  const fails = R.filter(r => r.startsWith('FAIL')).length;
  console.log('\n==== ' + (fails ? fails + ' 项 FAIL' : '全部通过') + '（共 ' + R.length + ' 项）====');
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
