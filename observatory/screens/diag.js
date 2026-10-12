// 诊断：右侧操作列在 dev 目标下为何空白
const { chromium } = require('playwright');
const fs = require('fs');

const OUT = 'E:/Projects/LLMOps-Engineer/observatory/screens';
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

  async function dump(tag) {
    const info = await page.evaluate(() => {
      const layout = document.querySelector('.layout');
      const right = layout && layout.children[1];
      const actions = document.getElementById('actions');
      const timeline = document.getElementById('timeline');
      const r = right ? right.getBoundingClientRect() : null;
      const cs = right ? getComputedStyle(right) : null;
      return {
        rightRect: r ? { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } : null,
        rightDisplay: cs ? cs.display + '/' + cs.visibility : null,
        rightHtmlLen: right ? right.innerHTML.length : -1,
        rightText: right ? right.textContent.replace(/\s+/g, ' ').trim().slice(0, 80) : null,
        actionBtns: actions ? actions.querySelectorAll('button').length : -1,
        actionLabels: actions ? Array.from(actions.querySelectorAll('button .lbl')).map(e => e.textContent.trim()) : [],
        timelineHtmlLen: timeline ? timeline.innerHTML.length : -1,
        pageScrollH: document.documentElement.scrollHeight,
        viewportH: window.innerHeight,
      };
    });
    console.log('[' + tag + ']', JSON.stringify(info, null, 1));
    return info;
  }

  await page.goto('http://127.0.0.1:8899', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#sw-tabs .sw-tab', { timeout: 15000 });
  await page.waitForTimeout(3500);
  const bootTarget = await page.textContent('#target');
  console.log('boot target =', bootTarget);
  await dump('boot');

  // 点 dev
  const tabs1 = await page.$$('#sw-tabs .sw-tab');
  for (const t of tabs1) {
    const label = (await t.textContent()) || '';
    if (label.includes('dev')) { await t.click(); break; }
  }
  await page.waitForTimeout(2500);
  await dump('after-click-dev');
  await page.screenshot({ path: OUT + '/diag-dev-viewport.png' });
  await page.screenshot({ path: OUT + '/diag-dev-full.png', fullPage: true });

  // 点 default（yaml 目标）—— 这里验证 renderActions 只渲染一次的守卫 bug
  const tabs2 = await page.$$('#sw-tabs .sw-tab');
  for (const t of tabs2) {
    const label = (await t.textContent()) || '';
    if (label.includes('default')) { await t.click(); break; }
  }
  await page.waitForTimeout(2500);
  await dump('after-click-default');
  await page.screenshot({ path: OUT + '/diag-default-viewport.png' });

  console.log('JS errors:', errors.length ? '\n' + errors.join('\n') : '(none)');
  await browser.close();
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
