import {
  webSearch,
  parseDuckDuckGoHtml,
  parseDuckDuckGoLite,
} from '../src/tools/builtin/webSearch';
import type { ToolContext } from '../src/tools/types';

const DDG_HTML = `
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FCat&amp;rut=abc">Cat - Wikipedia</a>
  <a class="result__snippet" href="x">The <b>cat</b> is a domestic species of small carnivorous mammal.</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://example.com/cats">All About Cats</a>
  <a class="result__snippet" href="y">Cats are great pets &amp; companions.</a>
</div>
`;

const DDG_LITE = `
<tr><td><a rel="nofollow" href="https://example.org/one">First Result</a></td></tr>
<tr><td><a rel="nofollow" href="https://duckduckgo.com/l/?uddg=x">Internal</a></td></tr>
<tr><td><a rel="nofollow" href="https://example.org/two">Second Result</a></td></tr>
`;

function ctxWith(html: string | Error): ToolContext {
  return {
    now: () => new Date('2026-03-01T12:00:00Z'),
    storage: {
      async get() { return null; },
      async set() {},
      async delete() {},
      async keysWithPrefix() { return []; },
    },
    fetchFn: (async () => {
      if (html instanceof Error) throw html;
      return new Response(html, { status: 200, headers: { 'Content-Type': 'text/html' } });
    }) as unknown as typeof fetch,
  };
}

describe('duckduckgo parsers', () => {
  it('extracts titles, unwrapped URLs and snippets from the HTML endpoint', () => {
    const results = parseDuckDuckGoHtml(DDG_HTML, 5);
    expect(results).toHaveLength(2);
    expect(results[0]).toEqual({
      title: 'Cat - Wikipedia',
      url: 'https://en.wikipedia.org/wiki/Cat',
      snippet: 'The cat is a domestic species of small carnivorous mammal.',
    });
    expect(results[1]!.url).toBe('https://example.com/cats');
    expect(results[1]!.snippet).toContain('&');
  });

  it('parses the lite endpoint and skips internal links', () => {
    const results = parseDuckDuckGoLite(DDG_LITE, 5);
    expect(results.map((r) => r.title)).toEqual(['First Result', 'Second Result']);
    expect(results.every((r) => !r.url.includes('duckduckgo.com'))).toBe(true);
  });
});

describe('web_search tool', () => {
  it('returns formatted results for the model', async () => {
    const res = await webSearch.execute({ query: 'cat facts' }, ctxWith(DDG_HTML));
    expect(res.ok).toBe(true);
    expect(res.output).toContain('Top web results for "cat facts"');
    expect(res.output).toContain('https://en.wikipedia.org/wiki/Cat');
    expect(res.output).toContain('open_website');
  });

  it('falls back to the lite endpoint when the html one yields nothing', async () => {
    let call = 0;
    const ctx = ctxWith(DDG_LITE);
    ctx.fetchFn = (async () => {
      call++;
      if (call === 1) return new Response('<html>no results</html>', { status: 200 });
      return new Response(DDG_LITE, { status: 200, headers: { 'Content-Type': 'text/html' } });
    }) as unknown as typeof fetch;
    const res = await webSearch.execute({ query: 'obscure' }, ctx);
    expect(res.ok).toBe(true);
    expect(res.output).toContain('First Result');
  });

  it('surfaces network failures as readable errors', async () => {
    const res = await webSearch.execute(
      { query: 'anything' },
      ctxWith(new Error('relay chain down')),
    );
    expect(res.ok).toBe(false);
    expect(res.output).toContain('relay chain down');
  });

  it('reports when the engine returns nothing at all', async () => {
    const res = await webSearch.execute({ query: 'invisible' }, ctxWith(''));
    expect(res.ok).toBe(false);
    expect(res.output).toContain('No results');
  });

  it('requires a query', async () => {
    const res = await webSearch.execute({}, ctxWith(DDG_HTML));
    expect(res.ok).toBe(false);
  });
});
