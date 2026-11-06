import type { APIRoute, GetStaticPaths } from 'astro';
import { orderedDocs } from '../lib/docs';

// Plain Markdown copy of each docs page for agents: /<slug>.md (the home page is /index.md).
export const getStaticPaths: GetStaticPaths = async () => (await orderedDocs()).map((d) => ({ params: { slug: d.id }, props: { d } }));

export const GET: APIRoute = ({ props }) => {
  const { d } = props as { d: Awaited<ReturnType<typeof orderedDocs>>[number] };
  const text = `# ${d.data.title}\n\n${d.data.description ?? ''}\n\n${(d.body ?? '').trim()}\n`;
  return new Response(text, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
};
