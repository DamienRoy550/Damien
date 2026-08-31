import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

/**
 * Voice input (speech → text).
 *
 * Web: the W3C SpeechRecognition API (Chrome/Edge/Safari 14.1+), which the
 * preview and any hosted build have. Native: reserved for the next release
 * (needs a native speech module — tracked in the roadmap).
 */

interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
}

type SRCtor = new () => RecognitionLike;

function getRecognitionCtor(): SRCtor | null {
  if (Platform.OS !== 'web') return null;
  const w = globalThis as {
    SpeechRecognition?: SRCtor;
    webkitSpeechRecognition?: SRCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface VoiceInput {
  supported: boolean;
  listening: boolean;
  transcript: string;
  error: string | null;
  start(): void;
  stop(): void;
}

export function useVoiceInput(onFinal: (text: string) => void): VoiceInput {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const supported = getRecognitionCtor() !== null;

  useEffect(() => {
    return () => {
      try {
        recognitionRef.current?.abort();
      } catch {
        // noop
      }
    };
  }, []);

  const start = useCallback(() => {
    const Ctor = getRecognitionCtor();
    if (!Ctor) return;
    try {
      recognitionRef.current?.abort();
    } catch {
      // noop
    }
    const recognition = new Ctor();
    recognitionRef.current = recognition;
    recognition.lang = 'en-GB';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    setTranscript('');
    setError(null);

    recognition.onresult = (event) => {
      let finalText = '';
      let interim = '';
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i];
        if (!result) continue;
        const alt = result[0];
        if (!alt) continue;
        if (result.isFinal) finalText += alt.transcript;
        else interim += alt.transcript;
      }
      setTranscript((finalText + interim).trim());
      if (finalText.trim()) {
        onFinalRef.current(finalText.trim());
      }
    };
    recognition.onerror = (event) => {
      const code = event.error;
      setError(
        code === 'not-allowed'
          ? 'Microphone permission denied'
          : code === 'no-speech'
            ? 'No speech detected'
            : `Voice error: ${code}`,
      );
      setListening(false);
    };
    recognition.onend = () => setListening(false);

    try {
      recognition.start();
      setListening(true);
    } catch {
      setListening(false);
    }
  }, []);

  const stop = useCallback(() => {
    try {
      recognitionRef.current?.stop();
    } catch {
      // noop
    }
    setListening(false);
  }, []);

  return { supported, listening, transcript, error, start, stop };
}
