// 交付截图：三个目标各一张（viewport 模式，等页面稳定再拍）
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

  await page.goto('http://127.0.0.1:8899', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#sw-tabs .sw-tab', { timeout: 15000 });
  await page.waitForTimeout(3000);

  for (const [file, want] of [['final-1-default', 'default'], ['final-2-dev', 'dev'], ['final-3-prod', 'prod']]) {
    const tabs = await page.$$('#sw-tabs .sw-tab');
    for (const t of tabs) {
      const label = (await t.textContent()) || '';
      if (label.includes(want)) { await t.click(); break; }
    }
    // 等顶栏副标题变成这个目标，再等 3 秒让所有面板渲染稳
    for (let i = 0; i < 12; i++) {
      if (((await page.textContent('#target')) || '').startsWith(want + '/')) break;
      await page.waitForTimeout(500);
    }
    await page.waitForTimeout(3000);
    await page.screenshot({ path: OUT + '/' + file + '.png' });
    const sub = ((await page.textContent('#target')) || '').replace(/\s+/g, ' ');
    console.log('OK', file, '→', sub.slice(0, 70));
  }
  await browser.close();
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
