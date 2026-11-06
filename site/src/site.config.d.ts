/** Runtime values live in {@link ./site.config.mjs}. */
declare const site: {
  name: string;
  tagline: string;
  description: string;
  repoUrl: string;
  branch: string;
  siteUrl: string;
  basePath: string;
};

export { site };
