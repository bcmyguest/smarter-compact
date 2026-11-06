import { site } from '../site.config';

/** Prefix an internal path with the configured base path (GitHub Pages sub-paths). */
export function withBase(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  return `${base}/${path.replace(/^\//, '')}`;
}

/** Absolute URL for canonical links, OG tags, and generated text files. */
export function absolute(path: string, origin: string | URL = import.meta.env.SITE ?? site.siteUrl): string {
  return new URL(withBase(path), origin).href;
}
