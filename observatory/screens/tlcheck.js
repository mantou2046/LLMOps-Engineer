// 时间线重复追加验证：数 .tl 个数 + 列出 (time, title)
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 } });

  async function tl(tag) {
    const info = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('#timeline .tl'));
      return {
        count: els.length,
        items: els.map(e => {
          const t = e.querySelector('.time');
          const ti = e.querySelector('.title');
          return (t ? t.textContent : '?') + ' ' + (ti ? ti.textContent : '?');
        }),
      };
    });
    console.log('[' + tag + '] .tl count =', info.count);
    info.items.forEach((s, i) => console.log('   ', i, s.slice(0, 70)));
  }

  await page.goto('http://127.0.0.1:8899', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#sw-tabs .sw-tab', { timeout: 15000 });
  await page.waitForTimeout(3000);
  await tl('boot');

  for (const want of ['dev', 'prod']) {
    const tabs = await page.$$('#sw-tabs .sw-tab');
    for (const t of tabs) {
      const label = (await t.textContent()) || '';
      if (label.includes(want)) { await t.click(); break; }
    }
    await page.waitForTimeout(2000);
    await tl('after-' + want);
  }
  await browser.close();
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
