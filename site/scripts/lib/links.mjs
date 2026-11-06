// Link checker for a built site: every internal href/src must resolve to a file
// in dist/ under the configured base path, and every #fragment must exist.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, posix, relative } from 'node:path';

/** @param {string} dir */
function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else yield p;
  }
}

const SKIP_SCHEME = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;

/**
 * @param {string} dist built directory
 * @param {{ base: string, origin?: string }} opts base like "/smarter-compact/"; origin like "https://host"
 */
export function checkLinks(dist, { base, origin }) {
  const baseDir = base.endsWith('/') ? base : `${base}/`;
  const idCache = new Map();
  const errors = [];
  let pages = 0;
  let links = 0;

  const resolveFile = (urlPath) => {
    const rel = urlPath.slice(baseDir.length);
    const candidates = urlPath.endsWith('/') ? [join(dist, rel, 'index.html')] : [join(dist, rel), join(dist, rel, 'index.html'), join(dist, `${rel}.html`)];
    return candidates.find((c) => existsSync(c) && statSync(c).isFile()) ?? null;
  };
  const idsOf = (file) => {
    if (!idCache.has(file)) {
      const html = readFileSync(file, 'utf8');
      idCache.set(file, new Set([...html.matchAll(/\s(?:id|name)="([^"]+)"/g)].map((m) => m[1])));
    }
    return idCache.get(file);
  };

  for (const file of walk(dist)) {
    if (!file.endsWith('.html')) continue;
    pages++;
    const rel = relative(dist, file).split('\\').join('/');
    const pageUrl = rel === 'index.html' ? baseDir : rel.endsWith('/index.html') ? `${baseDir}${rel.slice(0, -'index.html'.length)}` : `${baseDir}${rel}`;
    const html = readFileSync(file, 'utf8');
    // Link targets inside code samples are not links.
    const markup = html.replace(/<pre\b[\s\S]*?<\/pre>/gi, '').replace(/<code\b[\s\S]*?<\/code>/gi, '').replace(/<script\b[\s\S]*?<\/script>/gi, '');
    const refs = [...markup.matchAll(/\s(?:href|src)="([^"]*)"/g)].map((m) => m[1]);
    for (const m of markup.matchAll(/<meta\b[^>]*\scontent="([^"]*)"/g)) {
      if (origin && m[1].startsWith(`${origin}${baseDir}`)) refs.push(m[1]);
    }
    for (const raw of refs) {
      let href = raw.replace(/&amp;/g, '&');
      if (!href) continue;
      if (origin && href.startsWith(origin)) href = href.slice(origin.length) || '/';
      if (SKIP_SCHEME.test(href)) continue;
      links++;
      const [beforeHash, hash] = href.split(/#(.*)/s);
      const noQuery = beforeHash.split('?')[0];
      let target;
      if (noQuery === '') target = pageUrl;
      else if (noQuery.startsWith('/')) {
        if (!noQuery.startsWith(baseDir) && noQuery !== baseDir.slice(0, -1)) {
          errors.push(`${rel}: root-absolute link outside base path: ${raw}`);
          continue;
        }
        target = noQuery === baseDir.slice(0, -1) ? baseDir : noQuery;
      } else target = posix.normalize(posix.join(posix.dirname(pageUrl.endsWith('/') ? `${pageUrl}x` : pageUrl), noQuery));
      if (!target.startsWith(baseDir)) {
        errors.push(`${rel}: link escapes base path: ${raw}`);
        continue;
      }
      const resolved = resolveFile(target);
      if (!resolved) {
        errors.push(`${rel}: broken link ${raw} -> ${target}`);
        continue;
      }
      if (hash && resolved.endsWith('.html') && !idsOf(resolved).has(decodeURIComponent(hash))) {
        errors.push(`${rel}: missing anchor #${hash} in ${relative(dist, resolved)} (from ${raw})`);
      }
    }
  }
  return { pages, links, errors };
}
