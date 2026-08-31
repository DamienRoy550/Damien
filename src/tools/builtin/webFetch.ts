import type { Tool } from '../types';
import { ok, err } from '../types';
import { stripHtml, truncateText } from './html';

const DEFAULT_LIMIT = 1500;

/**
 * Fetch a web page and hand the model readable text.
 * Network access happens through ctx.fetchFn so the tool is testable and
 * the app layer can add timeouts / proxies.
 */
export const webFetch: Tool = {
  name: 'web_fetch',
  description:
    'Download a web page and return its readable text (HTML removed). Use for looking up facts, reading articles, or checking a specific page. Requires internet.',
  parameters: [
    { name: 'url', type: 'string', description: 'Full URL starting with https://', required: true },
    { name: 'max_chars', type: 'number', description: 'Max characters of text to return (default 1500)' },
  ],
  runsOffline: false,
  async execute(args, ctx) {
    let url = String(args.url ?? '').trim();
    if (!url) return err('url is required');
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`;

    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return err(`Invalid URL "${url}"`);
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return err('Only http(s) URLs are supported');
    }

    const limit = Math.max(200, Math.min(4000, Number(args.max_chars ?? DEFAULT_LIMIT) || DEFAULT_LIMIT));

    try {
      const res = await ctx.fetchFn(parsed.toString(), {
        headers: {
          'User-Agent': 'DamienAgent/0.1 (open source; +https://github.com/DamienRoy550/Damien)',
          Accept: 'text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.8',
        },
        signal: AbortSignal.timeout(15000),
      });
      const status = res.status;
      if (!res.ok && status >= 400) {
        return err(`HTTP ${status} from ${parsed.host}. Try a different URL.`);
      }
      const contentType = res.headers.get('content-type') ?? '';
      const body = await res.text();
      if (contentType.includes('application/json')) {
        return ok(truncateText(`HTTP ${status} JSON from ${parsed.host}:\n${body}`, limit));
      }
      const text = stripHtml(body);
      if (!text) return err(`Page loaded (HTTP ${status}) but no readable text was found.`);
      return ok(truncateText(`Text from ${parsed.host} (HTTP ${status}):\n${text}`, limit));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return err(`Fetch failed: ${msg}. The device may be offline or the site may block bots.`);
    }
  },
};
