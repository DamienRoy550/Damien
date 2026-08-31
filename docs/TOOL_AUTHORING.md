# Tool Authoring Guide

Tools are how Damien learns new skills. A tool is a ~30-line object, and good tools are the
highest-leverage contribution you can make.

## Anatomy of a tool

```ts
// src/tools/builtin/coin_flip.ts
import type { Tool } from '../types';
import { ok, err } from '../types';

export const coinFlip: Tool = {
  name: 'coin_flip',
  description: 'Flip a fair coin. Returns "heads" or "tails".',
  parameters: [
    { name: 'flips', type: 'number', description: 'How many times (default 1)' },
  ],
  runsOffline: true,

  async execute(args, ctx) {
    const flips = Math.max(1, Math.min(100, Number(args.flips ?? 1) || 1));
    const results = Array.from({ length: flips }, () =>
      Math.random() < 0.5 ? 'heads' : 'tails',
    );
    return ok(results.join(', '));
  },
};
```

That's it. Register it in `src/tools/index.ts`:

```ts
import { coinFlip } from './builtin/coinFlip';
registry.register(coinFlip);
```

## Rules that make tools work with small models

The model reads only your `description` and `parameters` — and small models are literal-
minded. These rules matter:

1. **The description is a prompt.** Say what the tool does, when to use it, and when NOT to
   use it. Compare:
   - ❌ `"Performs stochastic binary entropy generation."`
   - ✅ `"Flip a fair coin. Use when the user asks for a random heads/tails decision."`
2. **One tool, one job.** If you need an `operation` enum, consider whether two tools are
   clearer. (The datetime tool keeps an enum only because its three operations share six
   parameters.)
3. **Validate everything.** `coerceArguments` handles types, but clamp ranges inside your
   execute (`Math.min/max`) and return `err('...')` for nonsense. The error string is read
   by the model — tell it how to fix the problem:
   - ✅ `err('Unsupported unit "league". Supported: length, mass, volume, speed, data, time.')`
4. **Return model-shaped strings.** The `output` goes back into the prompt as
   `[OBSERVATION]`. Lead with the answer, keep it under ~2 sentences when possible, and
   include the value in a scannable form (`= 42`, `HTTP 200 JSON from host: {...}`).
5. **Never throw.** Throwing is reserved for the engine's timeout/cancellation machinery.
   Expected failures are `err(...)` returns; unexpected ones get caught by the engine and
   reported as observations anyway — but you lose the helpful message.
6. **Offline flag is honest.** `runsOffline` powers UI badges. Set it `false` only if the
   tool genuinely needs the network.

## ToolContext: what you get

```ts
interface ToolContext {
  now(): Date;                 // injectable clock (tests!)
  timeZone?: string;
  storage: KeyValueStore;      // get/set/delete/keysWithPrefix — async, persistent
  fetchFn: typeof fetch;       // use this, never global fetch (testability, timeouts)
  scheduler?: ReminderScheduler; // absent when notifications unavailable
  device?: DeviceActions;        // clipboard + openUrl; absent on unsupported hosts
  isDemo?: boolean;
}
```

If your tool needs a new capability (contacts, calendar, location...), add it to
`ToolContext`, provide a web-safe default, and gate registration in `createDefaultRegistry`
on its presence — see how `clipboardTool` is only registered when `ctx.device` exists.

## Persistence convention

`storage` is a flat KV store. Prefix your keys (`note:`, `todo:`, `cache:username:`) and use
`keysWithPrefix(prefix)` to enumerate. Values are strings — JSON-serialize structured data.

## Testing your tool

```ts
// __tests__/tools.test.ts (add a describe block)
import { coinFlip } from '../src/tools/builtin/coinFlip';

const ctx = () => ({
  now: () => new Date('2026-03-01T12:00:00Z'),
  storage: new FakeStore(),
  fetchFn: ...,
});

it('returns heads or tails', async () => {
  const res = await coinFlip.execute({}, ctx());
  expect(res.ok).toBe(true);
  expect(['heads', 'tails']).toContain(res.output);
});
```

If your tool calls another prompt-based LLM step, don't — keep tools deterministic;
the agent loop is the place for reasoning.

## Checklist before opening a PR

- [ ] Tool follows naming conventions (snake_case verb_noun)
- [ ] Description written *for the model*, with one usage hint
- [ ] Arguments clamped and validated; errors explain fixes
- [ ] `runsOffline` accurate
- [ ] Registered in `createDefaultRegistry` (gated if capability-dependent)
- [ ] Tests added and passing (`npm test`), typecheck clean
- [ ] Added to the tools table in `README.md`
