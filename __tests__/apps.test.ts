import { openWebsite, openApp, resolveAppTarget, looksLikeWebsite } from '../src/tools/builtin/apps';
import type { DeviceActions, ToolContext, KeyValueStore } from '../src/tools/types';

class FakeStore implements KeyValueStore {
  private map = new Map<string, string>();
  async get(k: string) { return this.map.get(k) ?? null; }
  async set(k: string, v: string) { this.map.set(k, v); }
  async delete(k: string) { this.map.delete(k); }
  async keysWithPrefix(p: string) { return [...this.map.keys()].filter((k) => k.startsWith(p)); }
}

function makeCtx(device?: Partial<DeviceActions>): ToolContext & { opened: string[]; openedInApp: string[] } {
  const opened: string[] = [];
  const openedInApp: string[] = [];
  return {
    opened,
    openedInApp,
    now: () => new Date('2026-03-01T12:00:00Z'),
    storage: new FakeStore(),
    fetchFn: (async () => { throw new Error('offline'); }) as unknown as typeof fetch,
    device: {
      async copyToClipboard() {},
      async openUrl(url: string) {
        opened.push(url);
        if (device?.openUrl) await device.openUrl(url);
      },
      async openInAppBrowser(url: string) {
        openedInApp.push(url);
      },
    },
  };
}

describe('resolveAppTarget', () => {
  it('maps well-known app names to schemes', () => {
    expect(resolveAppTarget('youtube')!.url).toBe('youtube://');
    expect(resolveAppTarget('youtube')!.strategy).toBe('app shortcut "youtube"');
    expect(resolveAppTarget('WhatsApp')!.url).toBe('whatsapp://');
    expect(resolveAppTarget('google maps').url).toBe('comgooglemaps://');
  });

  it('passes explicit deep links through untouched', () => {
    expect(resolveAppTarget('whatsapp://send?text=hi')).toEqual({
      url: 'whatsapp://send?text=hi',
      strategy: 'deep link',
    });
    expect(resolveAppTarget('spotify://playlist/abc').url).toBe('spotify://playlist/abc');
  });

  it('builds Android intent URIs from package names', () => {
    const r = resolveAppTarget('com.spotify.music');
    expect(r.strategy).toBe('Android package');
    expect(r.url).toContain('package=com.spotify.music');
    expect(r.url).toContain('android.intent.category.LAUNCHER');
  });

  it('falls back to a guessed scheme', () => {
    const r = resolveAppTarget('BanalApp');
    expect(r.url).toBe('banalapp://');
  });

  it('distinguishes packages from domains', () => {
    expect(looksLikeWebsite('youtube.com')).toBe(true);
    expect(looksLikeWebsite('https://x.dev/a')).toBe(true);
    expect(looksLikeWebsite('www.bbc.co.uk')).toBe(true);
    expect(looksLikeWebsite('com.whatsapp')).toBe(false);
    expect(looksLikeWebsite('youtube')).toBe(false);
  });
});

describe('open_website tool', () => {
  it('normalizes bare domains and opens in the in-app browser', async () => {
    const ctx = makeCtx();
    const res = await openWebsite.execute({ url: 'en.wikipedia.org/wiki/Cat' }, ctx);
    expect(res.ok).toBe(true);
    expect(ctx.openedInApp).toEqual(['https://en.wikipedia.org/wiki/Cat']);
  });

  it('passes full URLs through', async () => {
    const ctx = makeCtx();
    await openWebsite.execute({ url: 'https://example.com/x?q=1' }, ctx);
    expect(ctx.openedInApp).toEqual(['https://example.com/x?q=1']);
  });

  it('rejects non-websites', async () => {
    const res = await openWebsite.execute({ url: 'not a website!!' }, makeCtx());
    expect(res.ok).toBe(false);
  });

  it('reports unavailability honestly', async () => {
    const ctx = makeCtx();
    ctx.device = undefined;
    const res = await openWebsite.execute({ url: 'example.com' }, ctx);
    expect(res.ok).toBe(false);
    expect(res.error).toContain('not available');
  });
});

describe('open_app tool', () => {
  it('launches by alias', async () => {
    const ctx = makeCtx();
    const res = await openApp.execute({ app: 'youtube' }, ctx);
    expect(res.ok).toBe(true);
    expect(ctx.opened).toEqual(['youtube://']);
  });

  it('launches by Android package via intent', async () => {
    const ctx = makeCtx();
    const res = await openApp.execute({ app: 'com.whatsapp' }, ctx);
    expect(res.ok).toBe(true);
    expect(ctx.opened[0]).toContain('package=com.whatsapp');
  });

  it('launches by explicit deep link', async () => {
    const ctx = makeCtx();
    await openApp.execute({ app: 'spotify://track/xyz' }, ctx);
    expect(ctx.opened).toEqual(['spotify://track/xyz']);
  });

  it('falls back to the web version of an app in the demo', async () => {
    const ctx = makeCtx({
      openUrl: async () => {
        throw new Error('no scheme handler on web');
      },
    });
    const res = await openApp.execute({ app: 'youtube' }, ctx);
    expect(res.ok).toBe(true);
    expect(ctx.openedInApp).toEqual(['https://www.youtube.com']);
  });

  it('suggests the store page when the package is not installed', async () => {
    const ctx = makeCtx({
      openUrl: async () => {
        throw new Error('ActivityNotFound');
      },
    });
    const res = await openApp.execute({ app: 'com.does.notexist' }, ctx);
    expect(res.ok).toBe(false);
    expect(res.output).toContain('play.google.com/store/apps/details?id=com.does.notexist');
  });

  it('explains iOS scheme limits on unknown apps', async () => {
    const ctx = makeCtx({
      openUrl: async () => {
        throw new Error('no handler');
      },
    });
    const res = await openApp.execute({ app: 'zombo' }, ctx);
    expect(res.ok).toBe(false);
    expect(res.output).toContain('URL scheme');
  });

  it('falls back to the web version of an app in the demo', async () => {
    const ctx = makeCtx({
      openUrl: async () => {
        throw new Error('no scheme handler on web');
      },
    });
    const res = await openApp.execute({ app: 'youtube' }, ctx);
    expect(res.ok).toBe(true);
    expect(ctx.openedInApp).toEqual(['https://www.youtube.com']);
  });

  it('exposes web fallbacks in resolveAppTarget', () => {
    expect(resolveAppTarget('spotify').webFallback).toBe('https://open.spotify.com');
    expect(resolveAppTarget('zombo').webFallback).toBeUndefined();
  });
});
