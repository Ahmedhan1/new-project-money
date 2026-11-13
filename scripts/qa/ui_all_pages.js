// Independent QA: open every page of the main UI (/) in real Chromium (Node Playwright) and record JS errors, failed
// requests, 5xx responses, visible error notes and load time. Usage: node ui_all_pages.js <base> <token> <outDir>
const { chromium } = require('playwright');
const fs = require('fs');
const [base, token, outDir] = process.argv.slice(2);
(async () => {
  const rec = { base: 'http://127.0.0.1:<port>', pages: {}, page_errors: [], console_errors: [], failed_requests: [], http_5xx: [], screenshots: [] };
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  let current = 'login';
  page.on('pageerror', e => rec.page_errors.push(current + ': ' + String(e)));
  page.on('console', m => { if (m.type() === 'error') rec.console_errors.push(current + ': ' + m.text()); });
  page.on('requestfailed', r => rec.failed_requests.push(current + ': ' + r.url().replace(base, '') + ' ' + (r.failure() || {}).errorText));
  page.on('response', r => { if (r.status() >= 500) rec.http_5xx.push(current + ': ' + r.status() + ' ' + r.url().replace(base, '')); });
  try {
    await page.goto(base + '/studio');                          // the documented login screen sets the session cookie
    await page.waitForSelector('#tok');
    await page.fill('#tok', token);
    await page.click('#loginb');
    await page.waitForSelector('h2.page');
    await page.goto(base + '/');
    await page.waitForFunction(() => typeof PAGES !== 'undefined');
    const ids = await page.evaluate(() => Object.keys(PAGES));
    rec.page_ids = ids;
    for (const id of ids) {
      current = id;
      const t0 = Date.now();
      await page.evaluate(i => go(i), id);
      await page.waitForTimeout(parseInt(process.env.WAIT_MS || "1200"));
      const info = await page.evaluate(() => {
        const m = document.querySelector('main') || document.body;
        const txt = m.innerText || '';
        return { h: (m.querySelector('h1,h2') || {}).textContent || '', chars: txt.length,
                 error_text: (txt.match(/(Traceback|Internal Server Error|undefined is not|TypeError|\bNaN\b|\[object Object\])/) || [null])[0] };
      });
      rec.pages[id] = { ...info, ms: Date.now() - t0 };
      if (['home', 'review', 'creator', 'editor', 'storage', 'publish', 'research'].some(k => id.includes(k))) {
        const f = outDir + '/ui_' + id.replace(/[^a-z0-9_-]/gi, '_') + '.png';
        await page.screenshot({ path: f });
        rec.screenshots.push(f.split('/').pop());
      }
    }
  } catch (e) {
    rec.fatal = String(e);
  }
  await browser.close();
  rec.summary = { pages: Object.keys(rec.pages).length, page_errors: rec.page_errors.length, console_errors: rec.console_errors.length,
                  http_5xx: rec.http_5xx.length, pages_with_error_text: Object.entries(rec.pages).filter(([, v]) => v.error_text).map(([k, v]) => k + ':' + v.error_text),
                  empty_pages: Object.entries(rec.pages).filter(([, v]) => v.chars < 40).map(([k]) => k) };
  fs.writeFileSync(outDir + '/ui_all_pages.json', JSON.stringify(rec, null, 1));
  console.log(JSON.stringify(rec.summary));
})();
