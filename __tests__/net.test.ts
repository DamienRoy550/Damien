import { createNetFetch, type ProxyDef } from '../src/services/net';

const fakeProxy: ProxyDef = {
  name: 'test-relay',
  build: (u) => `https://relay.test/?url=${encodeURIComponent(u)}`,
};

describe('net layer (internet access)', () => {
  it('uses the direct connection when it works', async () => {
    const impl = (async (input: RequestInfo | URL) =>
      new Response(`direct:${input}`)) as unknown as typeof fetch;
    const netFetch = createNetFetch(impl, [fakeProxy]);
    const res = await netFetch('https://cors-friendly.example');
    expect(await res.text()).toBe('direct:https://cors-friendly.example');
  });

  it('falls back to a relay when direct is blocked (CORS)', async () => {
    const impl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith('https://relay.test/')) {
        return new Response('proxied body');
      }
      throw new TypeError('Failed to fetch (CORS)');
    }) as unknown as typeof fetch;
    const netFetch = createNetFetch(impl, [fakeProxy]);
    const res = await netFetch('https://blocked.example/page');
    expect(res.ok).toBe(true);
    expect(await res.text()).toBe('proxied body');
    expect(res.headers.get('x-damien-via')).toBe('test-relay');
  });

  it('tries relays in order and skips failing ones', async () => {
    const impl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('flaky-relay')) throw new TypeError('down');
      if (url.includes('solid-relay')) return new Response('solid!');
      throw new TypeError('CORS');
    }) as unknown as typeof fetch;
    const netFetch = createNetFetch(impl, [
      { name: 'flaky-relay', build: (u) => `https://flaky-relay.test/?u=${u}` },
      { name: 'solid-relay', build: (u) => `https://solid-relay.test/?u=${u}` },
    ]);
    const res = await netFetch('https://blocked.example');
    expect(await res.text()).toBe('solid!');
    expect(res.headers.get('x-damien-via')).toBe('solid-relay');
  });

  it('errors clearly when everything fails', async () => {
    const impl = (async () => {
      throw new TypeError('offline');
    }) as unknown as typeof fetch;
    const netFetch = createNetFetch(impl, [fakeProxy]);
    await expect(netFetch('https://unreachable.example')).rejects.toThrow(
      /Unable to reach the internet/,
    );
  });
});
