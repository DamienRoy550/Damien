import type { Tool } from '../types';
import { ok, err } from '../types';
import { decodeEntities } from './html';

/**
 * Web search — Damien queries DuckDuckGo's HTML endpoints and reads back
 * the top results (title, URL, snippet) as an observation, so he can reason
 * about them and answer. Works on native directly and on web through the
 * relay layer in src/services/net.
 */

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

function stripTags(html: string): string {
  return decodeEntities(html.replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim();
}

/** DDG wraps result links in a redirector — unwrap to the real URL. */
function unwrapHref(href: string): string {
  let h = href;
  if (h.startsWith('//')) h = `https:${h}`;
  if (h.includes('uddg=')) {
    try {
      const u = new URL(h);
      const real = u.searchParams.get('uddg');
      if (real) return real;
    } catch {
      // keep original
    }
  }
  return h;
}

export function parseDuckDuckGoHtml(html: string, limit: number): SearchResult[] {
  const results: SearchResult[] = [];

  const titleRe = /class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
  const snippetRe = /class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;

  const titles: Array<{ url: string; title: string }> = [];
  let m: RegExpExecArray | null;
  while ((m = titleRe.exec(html)) !== null) {
    titles.push({ url: unwrapHref(m[1] ?? ''), title: stripTags(m[2] ?? '') });
  }
  const snippets: string[] = [];
  while ((m = snippetRe.exec(html)) !== null) {
    snippets.push(stripTags(m[1] ?? ''));
  }

  for (let i = 0; i < titles.length && results.length < limit; i++) {
    const t = titles[i];
    if (!t || !t.title) continue;
    results.push({
      title: t.title,
      url: t.url,
      snippet: snippets[i] ?? '',
    });
  }
  return results;
}

export function parseDuckDuckGoLite(html: string, limit: number): SearchResult[] {
  const results: SearchResult[] = [];
  const rowRe = /<a[^>]+rel="nofollow"[^>]+href="(https?:\/\/[^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
  let m: RegExpExecArray | null;
  while ((m = rowRe.exec(html)) !== null && results.length < limit) {
    const url = m[1] ?? '';
    const title = stripTags(m[2] ?? '');
    if (!title || /duckduckgo\.com/i.test(url)) continue;
    results.push({ title, url, snippet: '' });
  }
  return results;
}

function formatResults(query: string, results: SearchResult[]): string {
  const lines = [`Top web results for "${query}":`];
  results.forEach((r, i) => {
    lines.push(`${i + 1}. ${r.title}`);
    if (r.snippet) lines.push(`   ${r.snippet}`);
    lines.push(`   ${r.url}`);
  });
  lines.push('Summarize or use these results to answer. Offer to open one with open_website.');
  return lines.join('\n');
}

export const webSearch: Tool = {
  name: 'web_search',
  description:
    'Search the web and read back the top results (titles, links, snippets) so you can answer questions about current events, facts, prices, news, or anything you are unsure about. Prefer this over guessing. Requires internet.',
  parameters: [
    { name: 'query', type: 'string', description: 'The search query, e.g. "bitcoin price today"', required: true },
    { name: 'max_results', type: 'number', description: 'How many results to read (default 5, max 8)' },
  ],
  runsOffline: false,
  async execute(args, ctx) {
    const query = String(args.query ?? '').trim();
    if (!query) return err('query is required');
    const limit = Math.max(1, Math.min(8, Number(args.max_results ?? 5) || 5));

    const headers = {
      'User-Agent': 'DamienAgent/0.1 (open source assistant)',
      'Accept-Language': 'en',
    };

    // Primary: the full HTML endpoint; backup: the lite endpoint.
    const endpoints = [
      `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`,
      `https://lite.duckduckgo.com/lite/?q=${encodeURIComponent(query)}`,
    ];

    for (const endpoint of endpoints) {
      try {
        const res = await ctx.fetchFn(endpoint, { headers });
        const body = await res.text();
        const results = endpoint.includes('/html/')
          ? parseDuckDuckGoHtml(body, limit)
          : parseDuckDuckGoLite(body, limit);
        if (results.length > 0) {
          return ok(formatResults(query, results));
        }
      } catch (e) {
        if (endpoints.indexOf(endpoint) === endpoints.length - 1) {
          const msg = e instanceof Error ? e.message : String(e);
          return err(`Web search failed: ${msg}`);
        }
      }
    }

    return err(
      'No results came back (the search engine may be rate-limiting or blocking automated queries). Try web_fetch on a known URL instead.',
    );
  },
};
