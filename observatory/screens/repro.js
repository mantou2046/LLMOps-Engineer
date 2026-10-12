// 最小复现：切 dev → 页面内 fetch delete-pod → 抓原始响应
const { chromium } = require('playwright');

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

  // 切 dev
  const tabs = await page.$$('#sw-tabs .sw-tab');
  for (const t of tabs) {
    const label = (await t.textContent()) || '';
    if (label.includes('dev')) { await t.click(); break; }
  }
  await page.waitForTimeout(2500);
  const sub = await page.textContent('#target');
  const btns = await page.$$eval('#actions button .lbl', els => els.map(e => e.textContent.trim()));
  console.log('切换后目标 =', sub.replace(/\s+/g, ' '));
  console.log('按钮 =', JSON.stringify(btns));

  // 页面内直接 fetch，绕开点击机制
  const r = await page.evaluate(async () => {
    const resp = await fetch('/api/action/delete-pod', { method: 'POST' });
    const text = await resp.text();
    let j = null;
    try { j = JSON.parse(text); } catch (e) {}
    return { status: resp.status, ok: resp.ok, body: j || text.slice(0, 300) };
  });
  console.log('delete-pod 原始响应:', JSON.stringify(r, null, 1));

  // 再核验服务端当前目标状态
  const st = await page.evaluate(async () => {
    const resp = await fetch('/api/state', { cache: 'no-store' });
    return await resp.json();
  });
  console.log('服务端 state.target =', JSON.stringify(st.target));
  console.log('state.pods =', (st.pods || []).map(p => p.name + '/' + (p.ready || '?')));
  await browser.close();
})().catch((e) => { console.error('FATAL', e && e.message ? e.message : e); process.exit(1); });
