import { Platform } from 'react-native';

/**
 * Voice output — Damien speaks its replies.
 *
 * Web: browser speechSynthesis (prefers a British male voice for that
 * JARVIS flavour). Native: expo-speech. Global mute is controlled from the
 * chat header (🔊/🔇) and persisted in settings.
 */

let enabled = true;
let cachedVoice: SpeechSynthesisVoice | null = null;

export function setSpeechEnabled(value: boolean): void {
  enabled = value;
  if (!value) stopSpeaking();
}

export function isSpeechEnabled(): boolean {
  return enabled;
}

export function stopSpeaking(): void {
  try {
    if (Platform.OS === 'web') {
      const synth = (globalThis as { speechSynthesis?: SpeechSynthesis }).speechSynthesis;
      synth?.cancel();
    } else {
      void import('expo-speech').then((Speech) => Speech.stop());
    }
  } catch {
    // no-op
  }
}

function pickBritishVoice(synth: SpeechSynthesis): SpeechSynthesisVoice | null {
  if (cachedVoice) return cachedVoice;
  const voices = synth.getVoices?.() ?? [];
  if (voices.length === 0) return null;
  const preferred =
    voices.find((v) => /en-GB/i.test(v.lang) && /daniel|arthur|oliver|male/i.test(v.name)) ??
    voices.find((v) => /en-GB/i.test(v.lang) && /google uk english male/i.test(v.name)) ??
    voices.find((v) => /en-GB/i.test(v.lang)) ??
    voices.find((v) => /^en/i.test(v.lang)) ??
    null;
  cachedVoice = preferred;
  return preferred;
}

/** Strip protocol noise so the spoken reply sounds natural. */
function speakableText(text: string): string {
  return text
    .replace(/https?:\/\/\S+/g, 'the link')
    .replace(/[*_#`>]/g, '')
    .slice(0, 420);
}

export async function speak(text: string): Promise<void> {
  if (!enabled) return;
  const clean = speakableText(text.trim());
  if (!clean) return;

  try {
    if (Platform.OS === 'web') {
      const synth = (globalThis as { speechSynthesis?: SpeechSynthesis }).speechSynthesis;
      if (!synth) return;
      synth.cancel();
      const utterance = new SpeechSynthesisUtterance(clean);
      const voice = pickBritishVoice(synth);
      if (voice) utterance.voice = voice;
      utterance.lang = voice?.lang ?? 'en-GB';
      utterance.rate = 1.02;
      utterance.pitch = 0.92;
      synth.speak(utterance);
    } else {
      const Speech = await import('expo-speech');
      Speech.stop();
      Speech.speak(clean, { language: 'en-GB', rate: 1.02, pitch: 0.92 });
    }
  } catch {
    // speech is best-effort garnish — never break the task flow
  }
}
