import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

/**
 * Voice input (speech → text), PC-grade.
 *
 * Engine 1 — "browser": the W3C SpeechRecognition API. Instant, streams
 * interim results, works in Chrome/Edge (desktop + Android) and Safari 14.1+.
 *
 * Engine 2 — "whisper": a fully client-side fallback for every other
 * browser (Firefox, older Safari). Transformers.js + Whisper tiny.en run in
 * WASM on the user's machine; the ~30 MB model is fetched once from the
 * public CDN and cached by the browser. Mic audio never leaves the device
 * for processing — inference is local.
 *
 * If the page runs inside an iframe whose mic is blocked (common in
 * previews), the UI offers a one-tap "pop out" so voice always works on PC.
 */

interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult:
    | ((event: {
        results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
      }) => void)
    | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
}

type SRCtor = new () => RecognitionLike;
type WhisperPipeline = (
  audio: Float32Array,
  options?: Record<string, unknown>,
) => Promise<Array<{ text: string }> | { text: string }>;

const TRANSFORMERS_CDN =
  'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.5/dist/transformers.min.js';
const MAX_RECORD_SECONDS = 30;

function getRecognitionCtor(): SRCtor | null {
  if (Platform.OS !== 'web') return null;
  const w = globalThis as { SpeechRecognition?: SRCtor; webkitSpeechRecognition?: SRCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export type VoiceEngine = 'browser' | 'whisper' | 'none';

function detectEngine(): VoiceEngine {
  if (Platform.OS !== 'web') return 'none';
  if (getRecognitionCtor()) return 'browser';
  if (typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function') {
    return 'whisper';
  }
  return 'none';
}

/** Load the transformers.js bundle once (browser only, on demand). */
let transformersPromise: Promise<WhisperPipeline> | null = null;

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) {
      resolve();
      return;
    }
    const el = document.createElement('script');
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error('Could not download the voice engine (internet needed once)'));
    document.head.appendChild(el);
  });
}

function getWhisperPipeline(onStatus: (s: string) => void): Promise<WhisperPipeline> {
  if (!transformersPromise) {
    transformersPromise = (async () => {
      onStatus('loading voice engine…');
      await loadScript(TRANSFORMERS_CDN);
      const T = (globalThis as { transformers?: { pipeline?: unknown } }).transformers;
      if (!T || typeof T.pipeline !== 'function') {
        throw new Error('Voice engine unavailable — try Chrome/Edge for built-in speech');
      }
      const pipeline = T.pipeline as (
        task: string,
        model: string,
        opts?: Record<string, unknown>,
      ) => Promise<WhisperPipeline>;
      onStatus('downloading voice model (~30 MB, first time only)…');
      const asr = await pipeline('automatic-speech-recognition', 'Xenova/whisper-tiny.en', {
        dtype: 'q8',
        progress_callback: (p: { status?: string; progress?: number }) => {
          if (p?.status === 'progress' && typeof p.progress === 'number') {
            onStatus(`downloading voice model… ${Math.round(p.progress)}%`);
          }
        },
      });
      onStatus('');
      return asr;
    })().catch((e) => {
      transformersPromise = null;
      throw e;
    });
  }
  return transformersPromise;
}

/** Merge recorded chunks and resample to the 16 kHz mono Whisper needs. */
async function audioChunksTo16kMono(chunks: Float32Array[], srcRate: number): Promise<AudioBuffer> {
  const length = chunks.reduce((n, c) => n + c.length, 0);
  const OfflineCtx =
    (globalThis as { OfflineAudioContext?: typeof OfflineAudioContext }).OfflineAudioContext ??
    (globalThis as { webkitOfflineAudioContext?: typeof OfflineAudioContext })
      .webkitOfflineAudioContext;
  if (!OfflineCtx) throw new Error('Audio processing unsupported in this browser');
  const targetRate = 16000;
  const offline = new OfflineCtx(1, Math.max(1, Math.ceil((length * targetRate) / srcRate)), targetRate);
  const buffer = offline.createBuffer(1, length, srcRate);
  const data = buffer.getChannelData(0);
  let offset = 0;
  for (const c of chunks) {
    data.set(c, offset);
    offset += c.length;
  }
  const source = offline.createBufferSource();
  source.buffer = buffer;
  source.connect(offline.destination);
  source.start();
  return offline.startRendering();
}

export interface VoiceInput {
  engine: VoiceEngine;
  engineLabel: string;
  listening: boolean;
  transcript: string;
  status: string;
  error: string | null;
  start(): void;
  stop(): void;
}

export function useVoiceInput(onFinal: (text: string) => void): VoiceInput {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [engine, setEngine] = useState<VoiceEngine>('none');
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const whisperRef = useRef<{
    stream: MediaStream;
    context: AudioContext;
    processor: ScriptProcessorNode;
    source: MediaStreamAudioSourceNode;
    chunks: Float32Array[];
    sampleRate: number;
    timer?: ReturnType<typeof setTimeout>;
  } | null>(null);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;
  const finalSentRef = useRef(false);

  useEffect(() => {
    setEngine(detectEngine());
  }, []);

  useEffect(() => {
    return () => {
      try {
        recognitionRef.current?.abort();
        const w = whisperRef.current;
        if (w) {
          clearTimeout(w.timer);
          w.processor.disconnect();
          w.source.disconnect();
          w.stream.getTracks().forEach((t) => t.stop());
          void w.context.close();
          whisperRef.current = null;
        }
      } catch {
        // noop
      }
    };
  }, []);

  const startBrowserEngine = useCallback(() => {
    const Ctor = getRecognitionCtor();
    if (!Ctor) return;
    finalSentRef.current = false;
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
      if (finalText.trim() && !finalSentRef.current) {
        finalSentRef.current = true;
        onFinalRef.current(finalText.trim());
      }
    };
    recognition.onerror = (event) => {
      const code = event.error;
      setError(
        code === 'not-allowed'
          ? 'Microphone blocked — use the ↗ pop-out button, or allow mic access'
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
      setError('Could not start the microphone');
    }
  }, []);

  const startWhisperEngine = useCallback(async () => {
    try {
      setError(null);
      setTranscript('');
      setStatus('requesting microphone…');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AudioCtor =
        (globalThis as { AudioContext?: typeof AudioContext }).AudioContext ??
        (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtor) throw new Error('Audio unsupported in this browser');
      const context = new AudioCtor();
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(4096, 1, 1);
      const chunks: Float32Array[] = [];
      processor.onaudioprocess = (e) => {
        chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      };
      source.connect(processor);
      processor.connect(context.destination);
      whisperRef.current = {
        stream,
        context,
        processor,
        source,
        chunks,
        sampleRate: context.sampleRate,
      };
      setListening(true);
      setStatus('listening… (processing runs on this device)');
      const timer = setTimeout(() => {
        void stopWhisperAndTranscribe();
      }, MAX_RECORD_SECONDS * 1000);
      whisperRef.current.timer = timer;
    } catch (e) {
      setListening(false);
      setStatus('');
      setError(
        e instanceof Error && e.name === 'NotAllowedError'
          ? 'Microphone blocked — use the ↗ pop-out button, or allow mic access'
          : e instanceof Error
            ? e.message
            : 'Could not access the microphone',
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopWhisperAndTranscribe = useCallback(async () => {
    const w = whisperRef.current;
    if (!w) return;
    whisperRef.current = null;
    clearTimeout(w.timer);
    setListening(false);
    setStatus('transcribing on this device…');
    try {
      w.processor.disconnect();
      w.source.disconnect();
      w.stream.getTracks().forEach((t) => t.stop());
      const recordedSeconds = w.chunks.reduce((n, c) => n + c.length, 0) / w.sampleRate;
      const buffer = await audioChunksTo16kMono(w.chunks, w.sampleRate);
      await w.context.close();
      const pcm = buffer.getChannelData(0);
      if (recordedSeconds < 0.4) {
        setStatus('');
        setError('No speech detected');
        return;
      }
      const asr = await getWhisperPipeline((s) => setStatus(s));
      const output = await asr(pcm);
      const text = (Array.isArray(output) ? output[0]?.text : output?.text) ?? '';
      const clean = String(text).trim();
      setStatus('');
      if (clean) {
        setTranscript(clean);
        onFinalRef.current(clean);
      } else {
        setError('No speech detected');
      }
    } catch (e) {
      setStatus('');
      setError(e instanceof Error ? e.message : 'Voice transcription failed');
    }
  }, []);

  const start = useCallback(() => {
    const engineNow = engine === 'none' ? detectEngine() : engine;
    setEngine(engineNow);
    if (engineNow === 'browser') {
      startBrowserEngine();
    } else if (engineNow === 'whisper') {
      void startWhisperEngine();
    } else {
      setError('Voice needs a PC browser (Chrome/Edge recommended) or mic permission');
    }
  }, [engine, startBrowserEngine, startWhisperEngine]);

  const stop = useCallback(() => {
    if (recognitionRef.current) {
      try {
        recognitionRef.current.stop();
      } catch {
        // noop
      }
    }
    if (whisperRef.current) {
      void stopWhisperAndTranscribe();
    }
    setListening(false);
  }, [stopWhisperAndTranscribe]);

  const engineLabel =
    engine === 'browser'
      ? 'browser speech'
      : engine === 'whisper'
        ? 'on-device whisper'
        : 'voice unavailable';

  return { engine, engineLabel, listening, transcript, status, error, start, stop };
}
