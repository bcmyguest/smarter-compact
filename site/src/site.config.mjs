// Single source of truth for branding and URLs. Plain .mjs so build scripts,
// the Astro config, and pages all read the same values.
//
// SITE_URL and BASE_PATH override siteUrl and basePath at build time. Use
// BASE_PATH=/ for a custom domain at the root.

export const site = {
  name: 'Smarter Compact',
  tagline: 'Context management (smart compaction) for coding agents.',
  description:
    'Context management (smart compaction) for coding agents — long sessions with small, cached prompts; every turn kept in a local SQLite store the agent can search.',
  repoUrl: 'https://github.com/bcmyguest/smarter-compact',
  /** Branch that source links in the docs point at. */
  branch: 'main',
  siteUrl: 'https://bcmyguest.github.io',
  basePath: '/smarter-compact/',
};
