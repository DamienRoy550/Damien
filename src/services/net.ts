/**
 * Damien's internet access layer.
 *
 * Native (see net.native.ts): plain fetch — no CORS restrictions.
 *
 * Web: browsers block cross-origin reads of most sites (CORS), so a raw
 * `fetch('https://any-site.com')` fails. Strategy:
 *
 *   1. Try DIRECT first — CORS-friendly sites (Wikipedia, many APIs) work
 *      with zero intermediaries.
 *   2. On failure, relay through public read-only proxies, in order, and
 *      return the first successful response (normalized into a standard
 *      Response tagged with an `x-damien-via` header).
 *   3. Everything failed → one clear error the agent can relay.
 *
 * The relay list is plain data — contributors can extend/replace it.
 */

export interface ProxyDef {
  name: string;
  build: (url: string) => string;
}

export const WEB_PROXIES: ProxyDef[] = [
  { name: 'corsproxy.io', build: (u) => `https://corsproxy.io/?url=${encodeURIComponent(u)}` },
  { name: 'allorigins', build: (u) => `https://api.allorigins.win/raw?url=${encodeURIComponent(u)}` },
  { name: 'jina-reader', build: (u) => `https://r.jina.ai/${u}` },
];

export type FetchLike = typeof fetch;

const DEFAULT_TIMEOUT_MS = 15_000;

function mergedSignal(initSignal: AbortSignal | null | undefined, timeoutMs: number): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  if (initSignal && typeof AbortSignal.any === 'function') {
    return AbortSignal.any([initSignal, timeout]);
  }
  return timeout;
}

export interface NetFetchOptions extends RequestInit {
  timeoutMs?: number;
}

export function createNetFetch(impl: FetchLike, proxies: ProxyDef[] = WEB_PROXIES): FetchLike {
  return (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const { timeoutMs, ...rest } = (init ?? {}) as NetFetchOptions;
    const timeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;

    // 1. Direct — works for CORS-enabled origins, and any same-origin call.
    try {
      return await impl(url, {
        ...rest,
        signal: mergedSignal(rest.signal, timeout),
      });
    } catch {
      // CORS / network failure — fall through to relays.
    }

    // 2. Relays.
    for (const proxy of proxies) {
      try {
        const res = await impl(proxy.build(url), {
          ...rest,
          signal: mergedSignal(rest.signal, timeout),
        });
        if (res.ok) {
          const text = await res.text();
          return new Response(text, {
            status: 200,
            headers: {
              'Content-Type':
                res.headers.get('content-type') ?? 'text/plain; charset=utf-8',
              'x-damien-via': proxy.name,
            },
          });
        }
      } catch {
        continue; // next relay
      }
    }

    throw new Error(
      'Unable to reach the internet: the direct request was blocked and all relay fallbacks failed. The network may be down.',
    );
  }) as FetchLike;
}

/** Platform net access: direct-when-possible, relayed-when-necessary. */
export const netFetch: FetchLike = createNetFetch(fetch.bind(globalThis));
