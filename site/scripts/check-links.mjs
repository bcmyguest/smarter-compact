// Usage: node scripts/check-links.mjs [dir=dist]
// Reads BASE_PATH / SITE_URL like the build does.
import { existsSync } from 'node:fs';
import { checkLinks } from './lib/links.mjs';
import { site } from '../src/site.config.mjs';

const dir = process.argv[2] ?? 'dist';
if (!existsSync(dir)) {
  console.error(`check-links: ${dir} does not exist; build first.`);
  process.exit(2);
}
const base = process.env.BASE_PATH || site.basePath;
const origin = (process.env.SITE_URL || site.siteUrl).replace(/\/$/, '');
const { pages, links, errors } = checkLinks(dir, { base, origin });
if (errors.length) {
  console.error(`check-links: ${errors.length} problem(s) across ${pages} pages:`);
  for (const e of errors.slice(0, 80)) console.error(`  ${e}`);
  process.exit(1);
}
console.log(`check-links: OK (${pages} pages, ${links} internal links resolve under ${base})`);
