import type { APIRoute } from 'astro';
import { absolute } from '../lib/url';
import { orderedDocs } from '../lib/docs';
import { site as brand } from '../site.config';

export const GET: APIRoute = async ({ site }) => {
  const docs = await orderedDocs();
  const links = docs.map((d) => `- [${d.data.title}](${absolute(`${d.id}.md`, site)}): ${d.data.description ?? ''}`).join('\n');
  const text = `# ${brand.name}

> ${brand.description}

Source: ${brand.repoUrl}

## Docs

${links}

## Optional

- [Full docs in one file](${absolute('llms-full.txt', site)})
`;
  return new Response(text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
