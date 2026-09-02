# Damien — Voice AI 🎙️

A voice AI that **hears you and replies out loud**, right in your browser.

Tap the microphone, talk naturally, and Damien listens (speech‑to‑text),
thinks up a reply, and speaks it back to you (text‑to‑speech).

## Features

- 🎤 **Hears you** — uses the browser's built‑in Speech Recognition
- 🔊 **Talks back** — replies out loud with Speech Synthesis (pick any voice)
- 🙌 **Hands‑free mode** — auto‑listens again after each reply for a real conversation
- 🧠 **Built‑in brain** — chit‑chat, the time & date, live math, jokes, and fun facts
- ⌨️ **Type fallback** — if your browser lacks voice input, type and Damien still replies aloud
- 🎨 Animated orb that pulses while listening and while speaking

## Run it

```bash
npm install
npm start
```

Then open **http://localhost:3000**.

> **Best experience:** Google Chrome or Microsoft Edge (desktop or Android),
> which support the Web Speech API for microphone input. You'll be asked to
> allow microphone access the first time.

## Try saying

- "Hey, how are you?"
- "What time is it?" / "What day is it?"
- "What's twelve times eight?" / "Square root of 81" / "20 percent of 50"
- "Tell me a joke" / "Give me a fun fact"
- "What can you do?"

## How it works

```
Your voice ──▶ SpeechRecognition (browser) ──▶ /api/reply (server brain)
                                                     │
Your speakers ◀── SpeechSynthesis (browser) ◀── reply text
```

- `public/` — the front‑end (mic capture, TTS, UI)
- `server.js` — Express server + `/api/reply` endpoint
- `brain.js` — the reply engine. **Swap this for a real LLM** by replacing
  `generateReply()` with an API call (e.g. OpenAI) — the rest stays the same.

## Upgrading to a real LLM

In `brain.js`, replace the body of `generateReply` with a call to your model
of choice and keep the same return type (a string). The client already sends
the recent conversation `history`, so you can pass it straight through as
chat messages.
