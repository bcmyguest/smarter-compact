// Turns repository docs/*.md into Starlight pages: drops the H1 (Starlight
// renders the title), rewrites relative links, and adds frontmatter. Pure
// functions; the filesystem work lives in sync.mjs.
import { posix } from 'node:path';

/**
 * Every docs page, in sidebar order. `slug: ''` is the home page. `label` is
 * the sidebar text when it differs from `title`. Descriptions follow the
 * "Learn more" table in docs/index.md.
 */
export const PAGES = [
  {
    slug: '',
    source: 'docs/index.md',
    group: 'Start',
    title: 'Smarter Compact',
    label: 'Overview',
    description:
      'Context management (smart compaction) for coding agents — long sessions with small, cached prompts; every turn kept in a local SQLite store the agent can search.',
  },
  {
    slug: 'context-management',
    source: 'docs/context-management.md',
    group: 'Guides',
    title: 'Context management (smart compaction)',
    label: 'Smart compaction',
    description: 'How trimming works, ContextBench results, agents, MCP tools.',
  },
  {
    slug: 'context-proxy',
    source: 'docs/context-proxy.md',
    group: 'Guides',
    title: 'Context proxy',
    description: 'LiteLLM proxy, env vars, proxy port, token estimate.',
  },
  {
    slug: 'opencode',
    source: 'docs/opencode.md',
    group: 'Guides',
    title: 'OpenCode',
    description: 'Plugin install and options.',
  },
  {
    slug: 'context-store',
    source: 'docs/context-store.md',
    group: 'Reference',
    title: 'Context store contract',
    label: 'Context store',
    description: 'File layout, schema, trim rules, search and SQL.',
  },
  {
    slug: 'context-research',
    source: 'docs/context-research.md',
    group: 'Reference',
    title: 'Context research',
    description: 'Related work, design evidence, strict runs through Run E.',
  },
];

/**
 * Replace text segments outside fenced code and inline code with fn(segment).
 * @param {string} md
 * @param {(s: string) => string} fn
 */
export function mapProse(md, fn) {
  const lines = md.split('\n');
  const out = [];
  let fence = null;
  let buf = [];
  const flush = () => {
    if (!buf.length) return;
    // Mask inline code spans so links inside them stay literal, while a code
    // span inside link text still survives the rewrite.
    const spans = [];
    const masked = buf.join('\n').replace(/(`+)[^`\n]*?\1/g, (m) => `\u0000${spans.push(m) - 1}\u0000`);
    out.push(fn(masked).replace(/\u0000(\d+)\u0000/g, (_m, i) => spans[Number(i)]));
    buf = [];
  };
  for (const line of lines) {
    const f = /^\s*(`{3,}|~{3,})/.exec(line);
    if (f) {
      if (fence === null) {
        flush();
        fence = f[1][0];
        out.push(line);
      } else if (f[1][0] === fence) {
        out.push(line);
        fence = null;
      } else out.push(line);
    } else if (fence !== null) out.push(line);
    else buf.push(line);
  }
  flush();
  return out.join('\n');
}

/**
 * Rewrite relative Markdown links.
 *  - a link to another published doc becomes a site-relative page link
 *  - any other existing repository path becomes a GitHub blob/tree URL
 *  - a link to a missing path throws, so a broken link fails the build
 *
 * @param {string} md
 * @param {object} ctx
 * @param {string} ctx.sourcePath repo-relative path of the source file, e.g. docs/a.md
 * @param {Map<string, string>} ctx.published repo-relative source path -> slug ('' is the home page)
 * @param {(repoPath: string) => 'file' | 'dir' | null} ctx.kindOf
 * @param {string} ctx.repoUrl
 * @param {string} ctx.branch
 * @param {string} [ctx.toRoot] relative path from the page to the site root: '../' for /slug/, './' for the home page
 */
export function rewriteLinks(md, { sourcePath, published, kindOf, repoUrl, branch, toRoot = '../' }) {
  const dir = posix.dirname(sourcePath);
  return mapProse(md, (prose) =>
    prose.replace(/(!?)\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)/g, (whole, bang, text, href, title) => {
      if (/^(?:[a-z][a-z0-9+.-]*:|#|\/\/)/i.test(href)) return whole;
      if (href.startsWith('/')) return whole;
      const [pathPart, hash = ''] = href.split(/#(.*)/s);
      const decoded = decodeURI(pathPart);
      const target = posix.normalize(posix.join(dir, decoded));
      if (target.startsWith('..')) throw new Error(`${sourcePath}: link "${href}" leaves the repository`);
      const slug = published.get(target);
      if (slug !== undefined) return `${bang}[${text}](${toRoot}${slug ? `${slug}/` : ''}${hash ? `#${hash}` : ''}${title})`;
      const kind = kindOf(target);
      if (!kind) throw new Error(`${sourcePath}: link "${href}" points to ${target}, which does not exist`);
      const where = `${repoUrl}/${kind === 'dir' ? 'tree' : 'blob'}/${branch}/${target.replace(/\/$/, '')}`;
      return `${bang}[${text}](${where}${hash ? `#${hash}` : ''}${title})`;
    }),
  );
}

/**
 * Point relative `media/...` URLs in HTML attributes (src, srcset, poster) at
 * the copy the build publishes under `basePath`, so they resolve from any page depth.
 * @param {string} md
 * @param {string} basePath e.g. /smarter-compact/
 */
export function localizeMedia(md, basePath) {
  const base = basePath.replace(/\/?$/, '/');
  return md.replace(/\b(src|srcset|poster)="(?:\.\/)?media\//g, `$1="${base}media/`);
}

const PICTURE = /<picture>\s*<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)"\s*\/?>\s*<img src="([^"]+)" alt="([^"]*)"[^>]*?\/?>\s*<\/picture>/g;
const PLAIN_IMG = /<img src="([^"]+)" alt="([^"]*)"[^>]*?\/?>/g;
const CENTERED = /<p align="center">\n([\s\S]*?)\n<\/p>/g;
const CAPTION = /<sub>([\s\S]*?)<\/sub>/;

/** Figure class from what it shows: benchmark chart, wide overview, animation, side-by-side pair. */
function figureKind(src, count) {
  if (count > 1) return 'pair';
  if (/\/media\/(?:contextbench|termbench)-/.test(src)) return 'chart';
  if (/-wide\.png$/.test(src)) return 'wide';
  if (/\.gif$/.test(src)) return 'animation';
  return 'plain';
}

/**
 * Turn GitHub-style figures into the site's figures.
 *
 * - A light/dark `<picture>` becomes the theme-toggled duo (`sc-diagram-duo`); a plain `<img>` stays one image.
 * - `<p align="center">` (GitHub's centering) holding pictures or images, and an optional `<sub>` caption,
 *   becomes `<figure class="sc-figure sc-figure--<kind>">` with a `<figcaption>`. A block holding a
 *   `<video>` stays as written, so its `<img>` fallback does not replace the video.
 * - A bare `<picture>` outside such a paragraph becomes an uncaptioned figure. Other pictures are left alone.
 * @param {string} md
 */
export function pictureToDuo(md) {
  const duo = (dark, light, alt) =>
    `<div class="sc-diagram-duo">\n<img class="sc-diagram-light" src="${light}" alt="${alt}" />\n<img class="sc-diagram-dark" src="${dark}" alt="${alt}" />\n</div>`;
  const figure = (items, caption) =>
    [`<figure class="sc-figure sc-figure--${figureKind(items[0].src, items.length)}">`, ...items.map((i) => i.html), ...(caption ? [`<figcaption>${caption.trim()}</figcaption>`] : []), '</figure>'].join('\n');

  const centered = md.replace(CENTERED, (whole, inner) => {
    if (/<video\b/.test(inner)) return whole;
    const items = [];
    const rest = inner.replace(PICTURE, (_m, dark, light, alt) => {
      items.push({ src: light, html: duo(dark, light, alt) });
      return '';
    });
    for (const [, src, alt] of rest.matchAll(PLAIN_IMG)) items.push({ src, html: `<img class="sc-figure-img" src="${src}" alt="${alt}" />` });
    if (!items.length) return whole;
    return figure(items, CAPTION.exec(rest)?.[1]);
  });
  return centered.replace(PICTURE, (_m, dark, light, alt) => figure([{ src: light, html: duo(dark, light, alt) }]));
}

/** Drop the leading H1; Starlight renders the title from frontmatter. */
export function stripH1(md) {
  return md.replace(/^\s*#\s+.+\n+/, '');
}

/** Quote a scalar for YAML frontmatter. */
export function yamlString(s) {
  return JSON.stringify(s);
}

/**
 * @param {{ title: string, description: string }} meta
 * @param {string} body
 */
export function withFrontmatter({ title, description }, body) {
  return `---\ntitle: ${yamlString(title)}\ndescription: ${yamlString(description)}\n---\n\n${body.trim()}\n`;
}
