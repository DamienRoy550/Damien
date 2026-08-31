# Contributing to Damien

Thanks for helping build an AI assistant that respects its user! Damien is MIT-licensed and
anyone can contribute — code, tools, models, docs, bug reports.

## The fastest ways to help (highest leverage first)

1. **Write a tool** — each one is a new skill. See [docs/TOOL_AUTHORING.md](docs/TOOL_AUTHORING.md).
2. **Improve small-model reliability** — prompt engineering, parser hardening, context
   trimming. Real device logs of model outputs are gold.
3. **Test on real hardware** — report which phone, which model, tokens/sec, and what broke.
4. **Docs & docs translations.**

## Development setup

```bash
git clone https://github.com/DamienRoy550/Damien.git
cd Damien
npm install          # downloads llama.cpp artifacts (~200 MB) — needs normal network
npm test             # 61 tests, pure Node, no phone needed
npm run typecheck
```

Running on a device:

```bash
npx expo run:android   # builds a dev client (first build is slow)
npx expo run:ios       # macOS + Xcode required
```

## Ground rules

- **TypeScript strict mode is non-negotiable** (`npm run typecheck` must pass).
- **New logic needs tests.** The agent core is pure TS precisely so testing is cheap.
- **Keep the core RN-free.** Files under `src/agent`, `src/tools`, `src/core` must import
  zero React Native — that's what keeps them testable in Node. Platform stuff goes in
  `src/services` or platform-split files (`*.native.ts` / base `.ts`).
- **Privacy is a feature.** No telemetry, no analytics, no "phone home" dependencies. If a
  feature must use the network, it does so only when the user's task asks for it.
- **Model licenses matter.** The catalog only accepts permissively licensed weights unless
  there's a strong reason (and an explicit note).

## Commit / PR style

- Small PRs beat big ones. One tool per PR is perfect.
- Conventional-ish commit messages: `feat(tools): add coin_flip`, `fix(agent): ...`,
  `docs: ...`.
- CI (typecheck + tests) must be green.

## Reporting bugs

Open an issue with: device/OS, selected model, the task you gave, the visible steps
(History screen shows them), and what you expected. Screenshots of the step cards are
extremely helpful.

## CI

A ready-made GitHub Actions workflow (typecheck + unit tests + web bundle) is checked in at
[`docs/ci-workflow.yml`](docs/ci-workflow.yml). Copy it to `.github/workflows/ci.yml` to
activate it (the repo's current bot connection lacks the `workflows` permission to push it
there directly).

## Roadmap

See the README roadmap. If you want to tackle one of those, comment on the issue first so
work isn't duplicated.
