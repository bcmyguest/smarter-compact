import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { checkLinks } from './lib/links.mjs';

const BASE = '/smarter-compact/';
const ORIGIN = 'https://bcmyguest.github.io';

function dist(files) {
  const dir = mkdtempSync(join(tmpdir(), 'sc-links-'));
  for (const [name, body] of Object.entries(files)) {
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), body);
  }
  return dir;
}

test('resolves base-prefixed, relative, and anchor links', () => {
  const d = dist({
    'index.html': `<a href="${BASE}docs/a/">a</a><a href="${BASE}favicon.svg">i</a><a href="#top" >t</a><h1 id="top">x</h1>`,
    'docs/a/index.html': '<a href="../b/#sec">b</a><a href="../../">home</a>',
    'docs/b/index.html': '<h2 id="sec">s</h2>',
    'favicon.svg': '<svg/>',
  });
  const r = checkLinks(d, { base: BASE, origin: ORIGIN });
  assert.deepEqual(r.errors, []);
  assert.equal(r.pages, 3);
});

test('reports a root-absolute link that ignores the base path', () => {
  const d = dist({ 'index.html': '<a href="/docs/a/">a</a>', 'docs/a/index.html': '' });
  assert.match(checkLinks(d, { base: BASE, origin: ORIGIN }).errors[0], /outside base path/);
});

test('reports broken pages and missing anchors', () => {
  const d = dist({
    'index.html': `<a href="${BASE}nope/">x</a><a href="${BASE}docs/a/#gone">y</a>`,
    'docs/a/index.html': '<h1 id="here">h</h1>',
  });
  const errors = checkLinks(d, { base: BASE, origin: ORIGIN }).errors;
  assert.equal(errors.length, 2);
  assert.match(errors[0], /broken link/);
  assert.match(errors[1], /missing anchor #gone/);
});

test('ignores external links and links shown inside code samples', () => {
  const d = dist({ 'index.html': '<a href="https://example.com/x">e</a><pre><code><a href="/nope/">n</a></code></pre>' });
  assert.deepEqual(checkLinks(d, { base: BASE, origin: ORIGIN }).errors, []);
});

test('absolute URLs under the site origin and base are checked', () => {
  const d = dist({ 'index.html': `<meta property="og:image" content="${ORIGIN}${BASE}og.png"><link rel="canonical" href="${ORIGIN}${BASE}">` });
  const errors = checkLinks(d, { base: BASE, origin: ORIGIN }).errors;
  assert.equal(errors.length, 1);
  assert.match(errors[0], /og\.png/);
});
