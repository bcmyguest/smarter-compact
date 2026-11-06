import type { APIRoute, GetStaticPaths } from 'astro';
import { orderedDocs } from '../lib/docs';
import { absolute } from '../lib/url';

// The earlier Jekyll site served /<slug>.html; each stub forwards to /<slug>/ and keeps the #fragment.
export const getStaticPaths: GetStaticPaths = async () =>
  (await orderedDocs()).filter((d) => d.id !== 'index').map((d) => ({ params: { slug: d.id } }));

export const GET: APIRoute = ({ params, site }) => {
  const to = absolute(`${params.slug}/`, site);
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Moved</title>
<meta name="robots" content="noindex">
<link rel="canonical" href="${to}">
<meta http-equiv="refresh" content="0; url=${to}">
<script>location.replace(${JSON.stringify(to)} + location.hash)</script>
</head><body><a href="${to}">${to}</a></body></html>
`;
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
};
