const { chromium } = require('playwright');
(async () => {
  const [,, file, out, ...specs] = process.argv;
  const b = await chromium.launch(); 
  for (const sp of specs) {
    const gh = sp === 'card=gh';
    const p = await b.newPage({ viewport: { width: gh ? 1280 : 1200, height: gh ? 640 : 675 }, deviceScaleFactor: gh ? 1 : 2 });
    await p.goto('file://' + file + '?' + sp);
    await p.evaluate(() => document.fonts.ready);
    await p.screenshot({ path: `${out}/${sp.replace(/[=.]/g,'_')}.png` });
    await p.close();
  }
  await b.close();
})();
