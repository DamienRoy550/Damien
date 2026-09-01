import type { Tool } from '../types';
import { ok, err } from '../types';
import { truncateText } from './html';

const DEFAULT_LIMIT = 1200;

/**
 * Raw HTTP client for JSON APIs — the "call any API" escape hatch.
 * Deliberately separate from web_fetch (which returns readable page text).
 */
export const httpRequest: Tool = {
  name: 'http_request',
  description:
    'Send an HTTP request to a JSON API and return the raw response. Use for REST APIs and webhooks when web_fetch is not appropriate. Requires internet.',
  parameters: [
    { name: 'url', type: 'string', description: 'Full URL, e.g. https://api.example.com/v1/things', required: true },
    { name: 'method', type: 'string', description: 'HTTP method: GET (default), POST, PUT, PATCH, DELETE', enum: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] },
    { name: 'headers', type: 'string', description: 'Optional JSON object of headers, e.g. {"Authorization":"Bearer x"}' },
    { name: 'body', type: 'string', description: 'Optional request body (JSON string for POST/PUT/PATCH)' },
    { name: 'max_chars', type: 'number', description: 'Max characters of response to return (default 1200)' },
  ],
  runsOffline: false,
  async execute(args, ctx) {
    const url = String(args.url ?? '').trim();
    if (!/^https?:\/\//i.test(url)) return err('url must start with http:// or https://');

    const method = String(args.method ?? 'GET').toUpperCase();
    let headers: Record<string, string> = {};
    const headersRaw = args.headers;
    if (headersRaw !== undefined && headersRaw !== null && String(headersRaw).trim()) {
      try {
        const parsed = typeof headersRaw === 'object' ? headersRaw : JSON.parse(String(headersRaw));
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          headers = Object.fromEntries(
            Object.entries(parsed).map(([k, v]) => [k, String(v)]),
          );
        } else {
          return err('headers must be a JSON object');
        }
      } catch {
        return err('headers must be a valid JSON object string');
      }
    }

    const bodyRaw = args.body;
    const hasBody = bodyRaw !== undefined && bodyRaw !== null && String(bodyRaw).trim() !== '';
    if (hasBody && !['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      return err(`Method ${method} does not support a body`);
    }
    if (hasBody && !headers['Content-Type'] && !headers['content-type']) {
      headers['Content-Type'] = 'application/json';
    }

    const limit = Math.max(200, Math.min(4000, Number(args.max_chars ?? DEFAULT_LIMIT) || DEFAULT_LIMIT));

    try {
      const res = await ctx.fetchFn(url, {
        method,
        headers,
        ...(hasBody ? { body: String(bodyRaw) } : {}),
        signal: AbortSignal.timeout(15000),
      });
      const text = await res.text();
      const headerLine = Object.entries(res.headers as unknown as Record<string, string>)
        .filter(([k]) => ['content-type', 'date', 'x-request-id'].includes(k.toLowerCase()))
        .map(([k, v]) => `${k}: ${v}`)
        .join('; ');
      return ok(
        truncateText(
          `HTTP ${res.status} ${res.statusText || ''}${headerLine ? ` | ${headerLine}` : ''}\n${text || '(empty body)'}`,
          limit,
        ),
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return err(`Request failed: ${msg}`);
    }
  },
};
