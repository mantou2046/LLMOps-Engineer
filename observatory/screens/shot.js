// 观测台视觉验证 v2：逐目标截图 + 诊断发布历史渲染
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

  await page.goto('http://127.0.0.1:8899', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#sw-tabs .sw-tab', { timeout: 15000 });
  await page.waitForTimeout(3500);
  await page.screenshot({ path: OUT + '/01-default.png', fullPage: true });
  console.log('OK shot 01-default.png');

  for (const [i, want] of [['02', 'dev'], ['03', 'prod']]) {
    // 每次重新查询 tab（目标切换会触发 runbind 重建 DOM）
    const tabs = await page.$$('#sw-tabs .sw-tab');
    let clicked = false;
    for (const t of tabs) {
      const label = (await t.textContent()) || '';
      if (label.includes(want)) { await t.click(); clicked = true; break; }
    }
    if (!clicked) { console.log('MISS tab', want); continue; }
    await page.waitForTimeout(2500);
    await page.screenshot({ path: OUT + '/' + i + '-' + want + '.png', fullPage: true });
    const relVisible = await page.isVisible('#rel-sec');
    const relText = (await page.textContent('#rel-sec').catch(() => '(none)')) || '';
    const podCount = (await page.textContent('#pod-count').catch(() => '?')) || '?';
    const activeLabel = (await page.textContent('#sw-tabs .sw-tab.active').catch(() => '?')) || '';
    console.log('OK shot ' + want + ' | active=' + activeLabel.replace(/\s+/g, ' ').trim().slice(0, 60)
      + ' | rel-sec=' + relVisible + ' | pod-count=' + podCount.trim());
    console.log('  rel-sec text: ' + relText.replace(/\s+/g, ' ').trim().slice(0, 400));
  }

  console.log('JS errors:', errors.length ? '\n' + errors.join('\n') : '(none)');
  await browser.close();
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
