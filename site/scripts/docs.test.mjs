import assert from 'node:assert/strict';
import { test } from 'node:test';
import { localizeMedia, mapProse, pictureToDuo, rewriteLinks, stripH1, withFrontmatter } from './lib/docs.mjs';

const ctx = {
  sourcePath: 'docs/a.md',
  published: new Map([
    ['docs/b.md', 'b-page'],
    ['docs/index.md', ''],
  ]),
  kindOf: (p) => (['README.md', 'docs/c.md', 'bench/x'].includes(p) ? (p.endsWith('x') ? 'dir' : 'file') : null),
  repoUrl: 'https://github.com/o/r',
  branch: 'main',
};

test('links to published docs become site-relative page links', () => {
  assert.equal(rewriteLinks('[B](b.md)', ctx), '[B](../b-page/)');
  assert.equal(rewriteLinks('[B](b.md#sec)', ctx), '[B](../b-page/#sec)');
  assert.equal(rewriteLinks('[Home](index.md)', ctx), '[Home](../)');
});

test('the home page links one level down', () => {
  assert.equal(rewriteLinks('[B](b.md#sec)', { ...ctx, toRoot: './' }), '[B](./b-page/#sec)');
});

test('links to other repo files become blob or tree URLs', () => {
  assert.equal(rewriteLinks('[R](../README.md)', ctx), '[R](https://github.com/o/r/blob/main/README.md)');
  assert.equal(rewriteLinks('[C](c.md#x)', ctx), '[C](https://github.com/o/r/blob/main/docs/c.md#x)');
  assert.equal(rewriteLinks('[S](../bench/x)', ctx), '[S](https://github.com/o/r/tree/main/bench/x)');
});

test('a link to a missing path fails', () => {
  assert.throws(() => rewriteLinks('[X](gone.md)', ctx), /does not exist/);
});

test('external, anchor, and absolute links are untouched', () => {
  const md = '[a](https://x.dev) [b](#top) [c](mailto:a@b.co) [d](//cdn.x/y) [e](/abs/path)';
  assert.equal(rewriteLinks(md, ctx), md);
});

test('links inside code are untouched', () => {
  const md = 'use `[x](b.md)` here\n\n```md\n[x](b.md)\n```\n';
  assert.equal(rewriteLinks(md, ctx), md);
  assert.equal(mapProse('`a` b', (s) => s.toUpperCase()), '`a` B');
});

test('relative media URLs in HTML attributes get the base path', () => {
  const md = '<video src="media/v.mp4" poster="media/p.png"><img src="./media/g.gif" /></video><source srcset="media/d.gif" /><img src="https://x/media/y.png" />';
  assert.equal(
    localizeMedia(md, '/sc'),
    '<video src="/sc/media/v.mp4" poster="/sc/media/p.png"><img src="/sc/media/g.gif" /></video><source srcset="/sc/media/d.gif" /><img src="https://x/media/y.png" />',
  );
});

test('pictureToDuo: centered pictures become captioned figures with a theme duo', () => {
  const md = [
    '<p align="center">',
    '<picture>',
    '<source media="(prefers-color-scheme: dark)" srcset="/sc/media/contextbench-cost-dark.png" />',
    '<img src="/sc/media/contextbench-cost.png" alt="Cost" width="720" />',
    '</picture>',
    '<br />',
    '<sub>Cost per trial.</sub>',
    '</p>',
  ].join('\n');
  const out = pictureToDuo(md);
  assert.match(out, /<figure class="sc-figure sc-figure--chart">/);
  assert.match(out, /<img class="sc-diagram-light" src="\/sc\/media\/contextbench-cost\.png" alt="Cost" \/>/);
  assert.match(out, /<img class="sc-diagram-dark" src="\/sc\/media\/contextbench-cost-dark\.png" alt="Cost" \/>/);
  assert.match(out, /<figcaption>Cost per trial\.<\/figcaption>/);
  assert.doesNotMatch(out, /<picture>/);
});

test('pictureToDuo: a centered video block is left as written', () => {
  const md = '<p align="center">\n<video src="/sc/media/v.mp4">\n<img src="/sc/media/v.gif" alt="Demo" />\n</video>\n</p>';
  assert.equal(pictureToDuo(md), md);
});

test('stripH1 and withFrontmatter', () => {
  assert.equal(stripH1('# Title\n\nBody'), 'Body');
  assert.equal(withFrontmatter({ title: 'T "q"', description: 'D' }, '\nBody\n'), '---\ntitle: "T \\"q\\""\ndescription: "D"\n---\n\nBody\n');
});
