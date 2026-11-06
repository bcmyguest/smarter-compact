import { getCollection, type CollectionEntry } from 'astro:content';
import pageList from '../generated/docs.json';

/** Docs pages in sidebar order (the order scripts/sync.mjs generated). */
export async function orderedDocs(): Promise<CollectionEntry<'docs'>[]> {
  const docs = (await getCollection('docs')).filter((d) => d.id !== '404');
  const rank = (d: CollectionEntry<'docs'>) => pageList.findIndex((p) => (p.slug || 'index') === d.id);
  return docs.sort((a, b) => rank(a) - rank(b));
}
