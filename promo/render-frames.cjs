const { chromium } = require('playwright');
(async () => {
  const [,, file, out, fps, query = ''] = process.argv;
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 675 }, deviceScaleFactor: 1.6 });
  await p.goto('file://' + file + (query ? '?' + query : ''));
  await p.evaluate(() => document.fonts.ready);
  const dur = await p.evaluate(() => window.DURATION);
  const n = Math.round(dur * fps);
  for (let i = 0; i < n; i++) {
    await p.evaluate((t) => window.render(t), i / fps);
    await p.screenshot({ path: `${out}/f${String(i).padStart(4, '0')}.png` });
  }
  await b.close();
  console.log(n, 'frames');
})();
