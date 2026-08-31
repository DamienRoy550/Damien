# Damien Architecture

This document explains how Damien works inside, and why it is shaped the way it is.
Audience: contributors. Estimated read: 10 minutes.

## 1. The one big idea: a platform-agnostic core

Everything that *thinks* lives in `src/agent`, `src/tools`, `src/llm/types.ts` and is
**pure TypeScript with zero React Native imports**. The phone only contributes two things:

1. **Tokens** — from the on-device model (`LlamaEngine.native.ts`).
2. **Effects** — notifications, clipboard, files (`src/services/*`).

```
┌─────────────────────────────  phone / web / node  ─────────────────────────────┐
│                                                                                │
│   UI (expo-router, zustand)                                                    │
│     │  startTask("...")                                                        │
│     ▼                                                                          │
│   runtime.ts ──────────────► ToolContext { now, storage, fetchFn,              │
│     │                          scheduler, device }   ◄── src/services (native) │
│     │                                                                          │
│     ▼                                                                          │
│   AgentEngine ──── loop ────► ToolRegistry ──► Tool.execute(args, ctx)          │
│     │                            ▲                                             │
│     │ messages                   │ lookup                                      │
│     ▼                            │                                             │
│   LLMEngine.complete()  ◄── LlamaEngine (native)  /  MockEngine (web, tests)   │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

Consequences:

- The **entire agent is unit-tested in Node** (`__tests__/`, jest + ts-jest, no device).
- The **web demo is not a mockup** — it runs the real loop; only the token generator is
  simulated (`src/llm/demo/brain.ts`).
- Porting to another runtime (CLI, server, Wear OS) means implementing one interface:
  `LLMEngine`.

## 2. The agent loop

`src/agent/AgentEngine.ts`, ~250 lines, no dependencies:

```
run(task):
  systemPrompt = buildSystemPrompt(tools, now)          # prompts.ts
  for step in 1..maxSteps:                              # default 6
    messages = buildMessages(system, task, steps)       # context.ts, trims to budget
    reply = engine.complete(messages, forceJson)        # streamed, cancellable
    parsed = parseModelReply(reply)                     # parser.ts (tolerant JSON)
    if parsed.kind == answer: return ok(parsed.answer)
    tool = registry.get(parsed.tool)
    args = coerceArguments(tool, parsed.arguments)      # schema validation
    observation = await tool.execute(args, ctx)         # 25s timeout, try/catch
    steps.push(record(observation))                     # error strings, never throws
  return forcedFinalAnswer()                            # "budget spent, answer now"
```

Design decisions worth knowing:

- **Errors are observations, not exceptions.** A failing tool produces
  `ERROR: ...` text the model can read and react to (retry differently, or tell the user).
  This turns failures into agent behavior instead of crashes.
- **Cancellation is a first-class outcome.** The engine polls `signal.aborted` at every
  boundary and returns `status: 'cancelled'`; the UI stop button aborts both the fetch loop
  and the llama.cpp generation.
- **The loop always terminates.** Two hard stops: the step budget, and the forced final turn
  whose observation literally instructs the model to answer now. `summarizeSteps()` is the
  last-resort answer if even that fails.

## 3. The wire protocol

Small models imitate formats; they don't reliably follow prose. So Damien uses one
dead-simple protocol documented twice in the system prompt with two concrete examples:

```json
{"thought":"<20 words max>","tool":"<name>","arguments":{...}}
{"thought":"<20 words max>","answer":"<final reply>"}
```

`parseModelReply` (parser.ts) is intentionally paranoid:

1. Strip markdown fences.
2. Extract the first *balanced* `{...}` (brace-counting that respects strings).
3. `JSON.parse`; on failure repair (smart quotes → `"`, trailing commas) and retry.
4. Anything unparseable becomes `{kind: 'answer'}` — the loop never stalls on garbage.

With `strictJson` enabled (default), the engine additionally passes
`response_format: {type:'json_object'}` to llama.cpp, which is **grammar-constrained** —
the model physically cannot emit invalid JSON. A portable GBNF grammar ships in
`src/llm/grammar.ts` for engines without native JSON mode.

## 4. Context management for 2k–8k windows

`buildMessages` (context.ts) assembles:

```
[system]  system prompt (tool docs + rules + current time)   ← never trimmed
[user]    the task                                            ← never trimmed
[assistant/user] ... per step: {"thought","tool","arguments"} + [OBSERVATION] ...
```

Trimming, in order of preference:

1. Stub every observation except the newest to `[OBSERVATION] [omitted to save space]`.
2. Drop oldest step pairs (assistant + observation) entirely.
3. Last resort: head + newest pair only.

Budget = `contextSize × 0.6` (the rest is reserved for generation). Token counting uses a
calibrated heuristic (`chars / 3.6`) because the core must run without a tokenizer; the
error bar is acceptable for trimming decisions.

## 5. The tool layer

A tool is a plain object:

```ts
interface Tool {
  name: string;
  description: string;      // this text IS the model's documentation
  parameters: ToolParam[];  // name, type, description, required, enum
  runsOffline: boolean;
  execute(args, ctx): Promise<ToolResult>;
}
```

`ToolContext` injects time, storage, fetch, scheduler, device actions — so tools are pure
and testable. `coerceArguments` validates/coerces before execute (numbers from strings,
booleans from `"true"`, enum checks) so model sloppiness becomes a *typed error message the
model can fix* on the next turn.

Registry order matters: it's the order tools appear in the system prompt. Most-used first.

## 6. On-device inference

`LlamaEngine.native.ts` wraps llama.rn:

- `initLlama({model, n_ctx, n_gpu_layers: 99, ...}, onProgress)` — GPU layers are "all";
  llama.cpp falls back to CPU for what it can't place.
- Completion uses OAI-style `messages` + the GGUF's own **jinja chat template**, so every
  catalog model is prompted the way its authors intended. Fallback: `getFormattedChat()`.
- `forceJson` → `response_format json_object` (grammar-constrained).
- `cancel()` → `stopCompletion()`; engine is a process-wide singleton (llama.cpp contexts
  are heavy).

Model catalog (`src/llm/models.ts`) is deliberately small and opinionated: permissive
licenses first, single-file GGUFs, Q4_K_M as the mobile sweet spot.

## 7. State & persistence

Three zustand stores (`src/state/`): `settings` (generation params), `models` (downloads,
installed paths, selection), `runs` (task transcripts). Persistence is AsyncStorage JSON,
debounced during streaming. The UI renders from `runs` events — the agent never touches the
UI directly, it only emits `AgentEvent`s.

## 8. Testing strategy

- `__tests__/` covers parser, JSON repair, context trimming, every pure tool, the full
  loop (multi-step, unknown-tool recovery, arg validation, max-steps, cancellation,
  streaming), and the demo brain — 61 tests, all in Node, no mocks beyond the engine.
- `npm run typecheck` is strict (`strict`, `noUncheckedIndexedAccess`).
- CI runs both on every push/PR (`.github/workflows/ci.yml`).

Native code paths (llama.rn calls, expo-file-system downloads, notifications) are kept thin
and defensive; they're the only parts not covered by automated tests, and they're isolated
behind the interfaces described above.
