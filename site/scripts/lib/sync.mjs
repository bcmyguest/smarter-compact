// Build-time sync: repository docs -> generated Starlight pages and public assets.
// Fails the build on a missing doc source or a broken relative link.
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAGES, localizeMedia, pictureToDuo, rewriteLinks, stripH1, withFrontmatter } from './docs.mjs';

/** Social card served as og.png (1280x640). */
export const OG_SOURCE = 'promo/out/github-social-preview-1280x640.png';

/**
 * @param {{ repoRoot: string, siteRoot: string, repoUrl: string, branch: string, basePath: string, log?: (s: string) => void }} opts
 */
export function runSync({ repoRoot, siteRoot, repoUrl, branch, basePath, log = () => {} }) {
  const generatedDir = join(siteRoot, 'src', 'generated');
  const docsOut = join(siteRoot, 'src', 'content', 'docs');
  const publicDir = join(siteRoot, 'public');
  rmSync(docsOut, { recursive: true, force: true });
  mkdirSync(generatedDir, { recursive: true });
  mkdirSync(docsOut, { recursive: true });

  const published = new Map(PAGES.map((p) => [p.source, p.slug]));
  const kindOf = (repoPath) => {
    const abs = join(repoRoot, repoPath);
    if (!existsSync(abs)) return null;
    return statSync(abs).isDirectory() ? 'dir' : 'file';
  };

  for (const page of PAGES) {
    const src = join(repoRoot, page.source);
    if (!existsSync(src)) throw new Error(`required doc source is missing: ${page.source}`);
    const raw = pictureToDuo(localizeMedia(readFileSync(src, 'utf8'), basePath));
    const toRoot = page.slug ? '../' : './';
    const body = rewriteLinks(stripH1(raw), { sourcePath: page.source, published, kindOf, repoUrl, branch, toRoot });
    writeFileSync(join(docsOut, `${page.slug || 'index'}.md`), withFrontmatter(page, body));
    log(`page /${page.slug} <- ${page.source}`);
  }

  rmSync(join(publicDir, 'media'), { recursive: true, force: true });
  cpSync(join(repoRoot, 'docs', 'media'), join(publicDir, 'media'), { recursive: true });
  log('media: docs/media -> public/media');
  cpSync(join(repoRoot, OG_SOURCE), join(publicDir, 'og.png'));
  log(`og image: ${OG_SOURCE} -> public/og.png`);

  writeFileSync(
    join(generatedDir, 'docs.json'),
    `${JSON.stringify(PAGES.map(({ slug, group, title, label, description }) => ({ slug, group, title, label: label ?? title, description })), null, 2)}\n`,
  );
  return { pages: PAGES };
}
