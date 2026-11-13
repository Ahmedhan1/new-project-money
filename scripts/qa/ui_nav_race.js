const { chromium } = require('playwright');
const [base, token] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch(); const p = await (await b.newContext()).newPage();
  const errs = []; let tag = '';
  p.on('pageerror', e => errs.push(tag + ': ' + String(e).slice(0, 160)));
  await p.goto(base + '/studio'); await p.fill('#tok', token); await p.click('#loginb'); await p.waitForSelector('h2.page');
  await p.goto(base + '/'); await p.waitForFunction(() => typeof PAGES !== 'undefined');
  for (const [a, c] of [['tools', null], ['storage', null], ['storage', 'tools'], ['storage', 'home'], ['resources', 'tools']]) {
    tag = a + (c ? '->' + c : ''); await p.evaluate(([a, c]) => { go(a); if (c) go(c); }, [a, c]); await p.waitForTimeout(2500);
    const h = await p.evaluate(() => (document.querySelector('main h2') || {}).textContent);
    errs.push(tag + ' shows: ' + h);
  }
  console.log(JSON.stringify(errs, null, 1)); await b.close();
})();
