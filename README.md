# ◆ Damien

> **An open-source AI agent that lives on your phone.** No cloud. No API keys. No telemetry.
> A small language model runs on-device, plans your task, and uses tools until the job is done.

[![License: MIT](https://img.shields.io/badge/License-MIT-6C8CFF.svg)](LICENSE)
[![CI ready](https://img.shields.io/badge/CI-workflow%20included-38E1C6.svg)](docs/ci-workflow.yml)
![Platform](https://img.shields.io/badge/platform-Android%20%7C%20iOS-38E1C6.svg)
![Offline](https://img.shields.io/badge/inference-100%25%20on--device-6C8CFF.svg)

---

## What is this?

Damien is a **task-completion agent**, not a chatbot. You give it a goal — *"calculate the tip
on 84.50 for 3 people and save it as a note"*, *"convert 42 km to miles"*, *"remind me to
stretch in 45 minutes"*, *"fetch this page and summarize it"* — and it runs an autonomous
**plan → act → observe** loop:

```
 ┌──────────────────────────── Damien Agent Loop ───────────────────────────┐
 │                                                                          │
 │  task ──▶ THINK ──▶ pick a tool ──▶ RUN IT ──▶ observe result ──┐        │
 │              ▲                                                  │        │
 │              └────────────── not done yet ◀─────────────────────┘        │
 │              │                                                           │
 │              └── done ──▶ final answer to the user                       │
 └──────────────────────────────────────────────────────────────────────────┘
```

The brain is a **quantized small language model (GGUF)** executed by **llama.cpp** directly on
your phone's CPU/GPU. The hands are **tools** — calculator, unit converter, persistent notes,
notification reminders, a web reader, a raw HTTP client, clipboard, deep links — each one a
~30-line TypeScript object any contributor can add.

## Features

- 🔒 **Fully offline** — model weights, agent loop, notes and history never leave the device.
- 🧠 **On-device inference** — llama.cpp via [llama.rn](https://github.com/mybigday/llama.rn),
  GPU-accelerated (Metal / OpenCL / Vulkan) where available.
- 🛠️ **Tool runtime** — 15 built-in tools with schema validation, timeouts and argument coercion.
- 📐 **Small-model engineering** — strict JSON wire protocol, grammar-constrained generation
  (GBNF / `response_format`), context trimming tuned for 2k–8k windows, step budgets with a
  forced-final-answer escape hatch.
- 🌍 **Real internet access** — `web_search` reads actual search results and answers from them; page/API fetching rides a resilient direct→relay chain (corsproxy → allorigins → Jina reader) so it works in the browser too, where CORS would normally block it.
- 🌐 **Opens things for real** — an embedded browser panel (native: system browser tab) means "open youtube.com", "search youtube for lofi", or "google cat facts" actually open, proactively, no URLs required.
- 🎩 **JARVIS protocol** — courteous butler-engineer persona with dry wit, a configurable honorific ("Sir" by default), time-aware greetings, spoken replies (British voice when available) and a full self-diagnostic: just ask "run diagnostics".
- 🎙️ **Voice I/O (web)** — tap the mic, speak your task; Damien talks back. (On-device voice comes with the native speech module on the roadmap.)
- 📱 **One codebase, both platforms** — Expo + React Native + TypeScript (87%+ of the code is
  pure TypeScript with zero RN imports, fully unit-tested in Node).
- 🕸️ **Web demo included** — the same app runs in the browser with a simulated brain, so you
  can explore the agent pipeline without building anything.

## Try it

**Web demo (no setup):** open the live preview, or run it yourself:

```bash
npm install
npm run export:web && npm run serve:web   # → http://localhost:4173
```

**On your phone (real on-device inference):**

```bash
npm install
npx expo run:android        # or: npx expo run:ios
```

> `llama.rn` contains native code, so you need a development build (`expo run:*` or EAS Build)
> rather than Expo Go. The first `npm install` downloads prebuilt llama.cpp binaries for both
> platforms automatically.

Then: open **Model Setup** → download a brain (0.4–2 GB, one time) → give Damien a task.

## The model shelf

Small, permissive, phone-tested. Damien downloads GGUF files straight from Hugging Face.

| Model | Params | Quant | Size | License | Notes |
|---|---|---|---|---|---|
| **Qwen 2.5 Instruct** ⭐ | 1.5B | Q4_K_M | ~1 GB | Apache-2.0 | Best tool-calling per MB. Default. |
| Qwen 2.5 Instruct | 0.5B | Q4_K_M | ~400 MB | Apache-2.0 | Runs on anything; great for testing. |
| SmolLM2 Instruct | 1.7B | Q4_K_M | ~1.1 GB | Apache-2.0 | Fully open (data + recipe). |
| Llama 3.2 Instruct | 1B | Q4_K_M | ~800 MB | Llama Community | Solid general chat. |
| Qwen 2.5 Instruct | 3B | Q4_K_M | ~2 GB | Apache-2.0 | Flagship phones; noticeably smarter. |

## Built-in tools

| Tool | What it does | Offline |
|---|---|---|
| `calculator` | Exact arithmetic via a hand-rolled shunting-yard parser (no `eval`) | ✅ |
| `unit_convert` | Length, mass, volume, speed, data, time, temperature | ✅ |
| `datetime` | Now, date arithmetic, date differences | ✅ |
| `save_note` / `list_notes` / `search_notes` / `delete_note` | Persistent memory across restarts | ✅ |
| `schedule_reminder` | Local notifications at absolute or relative times | ✅ || `web_search` | Search DuckDuckGo, read titles/snippets/links, answer from them | 🌐 |
| `web_fetch` | Download a page → readable text (direct, with relay fallbacks) | 🌐 |
| `http_request` | Call any JSON API (the "everything else" escape hatch) | 🌐 |
| `text_stats` | Word counts, reading time, keywords | ✅ |
| `open_website` | Open any site in the in-app browser panel (native: OS browser tab) | ✅ |
| `open_app` | Launch installed apps: by name (40+ app directory + web fallbacks), Android package (`intent:` URI), or any deep link | ✅ |
| `system_status` | JARVIS-style self-diagnostic (engine, tools, memories, uptime) | ✅ |
| `copy_to_clipboard` | Hand results back to the phone | ✅ |

Adding your own is the main way to contribute → **[Tool authoring guide](docs/TOOL_AUTHORING.md)**.

## How the agent works (the interesting bits)

Small models are not GPT-class, so Damien is engineered around their limits:

1. **One strict wire format.** Every model turn is a single JSON object —
   `{"thought", "tool", "arguments"}` or `{"thought", "answer"}`. A tolerant parser
   (brace-matching, code-fence stripping, trailing-comma repair) degrades gracefully to
   "model was just talking" instead of crashing.
2. **Grammar-constrained JSON.** In strict mode, llama.cpp constrains generation to valid
   JSON — malformed tool calls become structurally impossible on supported models.
3. **A real tokenizer-free context budget.** The prompt assembler keeps the system prompt and
   task sacred, stubs old observations to `[omitted]`, then drops oldest steps, and always
   preserves the newest observation.
4. **Bounded autonomy.** Steps are capped (default 6). When the budget runs out, a forced
   final turn makes the model answer with what it has. Tools have 25 s timeouts; arguments are
   coerced and validated against schemas before any tool runs.
5. **Everything is injected.** Tools receive `{now, storage, fetchFn, scheduler, device}` —
   which is why the whole agent core is tested in plain Node with zero mocking frameworks.

Deep dive: **[Architecture](docs/ARCHITECTURE.md)**.

## Project layout

```
Damien/
├── app/                    # expo-router screens (chat, setup, history, settings)
├── src/
│   ├── agent/              # ★ the loop: prompts, parser, context, AgentEngine
│   ├── tools/              # ★ registry + builtin tools (pure TS, Node-testable)
│   ├── llm/                # engine interface, model catalog, llama.rn engine, demo brain
│   ├── services/           # platform bridges (downloads, notifications, storage)
│   ├── state/              # zustand stores
│   └── components/         # UI
├── __tests__/              # 61 unit tests over the platform-agnostic core
└── docs/                   # architecture + tool authoring
```

## Development

```bash
npm install          # also fetches llama.cpp native artifacts
npm test             # jest — the entire agent core
npm run typecheck    # strict TypeScript
npm run export:web   # web demo bundle
```

| | |
|---|---|
| Runtime | Expo SDK 57 · React Native 0.86 · TypeScript 5.9 |
| Inference | llama.cpp (llama.rn 0.12) |
| State | zustand + AsyncStorage |
| Tests | jest + ts-jest (Node, no device needed) |

## Roadmap

- [ ] Streaming final answers into the chat bubble (tokens already stream internally)
- [ ] Parallel tool calls for models that support them
- [ ] Session threads + task scheduling ("every morning, fetch X and notify me")
- [ ] LLM-powered tool: run another prompt as a sub-task
- [ ] Android quick-tile / share-sheet entry points
- [ ] Model hot-swap without app restart

## Contributing

PRs welcome — tools are the highest-leverage contribution (see
[CONTRIBUTING.md](CONTRIBUTING.md) and the [tool guide](docs/TOOL_AUTHORING.md)). Every new
tool should ship with tests; the harness makes that painless.

## Privacy

Damien has no backend. The only network traffic is model downloads (from Hugging Face, one
time, user-initiated) and explicit `web_fetch`/`http_request` calls a task asks for. Notes,
history and settings live in on-device storage.

## License

MIT — see [LICENSE](LICENSE). Downloaded models keep their own licenses (Apache-2.0 for the
Qwen/SmolLM families; Meta's community license for Llama 3.2).
