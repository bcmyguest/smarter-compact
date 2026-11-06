import type { APIRoute } from 'astro';
import { orderedDocs } from '../lib/docs';
import { site } from '../site.config';

export const GET: APIRoute = async () => {
  const docs = await orderedDocs();
  const text = [`# ${site.name}\n\n${site.description}\n`, ...docs.map((d) => `\n---\n\n# ${d.data.title}\n\n${(d.body ?? '').trim()}\n`)].join('');
  return new Response(text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
