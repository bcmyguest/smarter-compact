// Usage: node scripts/sync.mjs
// Reads ../docs/*.md and ../docs/media, writes the generated inputs under
// src/generated/, src/content/docs/, and public/.
import { fileURLToPath } from 'node:url';
import { runSync } from './lib/sync.mjs';
import { site } from '../src/site.config.mjs';

const siteRoot = fileURLToPath(new URL('..', import.meta.url));
const repoRoot = fileURLToPath(new URL('../..', import.meta.url));
const basePath = process.env.BASE_PATH || site.basePath;

try {
  runSync({ repoRoot, siteRoot, repoUrl: site.repoUrl, branch: site.branch, basePath, log: (s) => console.log(`sync: ${s}`) });
} catch (e) {
  console.error(`sync: ${/** @type {Error} */ (e).message}`);
  process.exit(1);
}
