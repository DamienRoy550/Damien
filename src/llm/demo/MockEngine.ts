import type {
  LLMEngine,
  CompletionOptions,
  CompletionResult,
  LoadedModelInfo,
  ModelDefinition,
} from '../types';
import { EngineCancelledError } from '../types';
import type { Brain } from './brain';

export type MockBrainSpec = Brain | string[];

/**
 * Deterministic, dependency-free engine.
 *
 *  - Pass an array of strings to SCRIPT exact replies (tests).
 *  - Pass a brain function for dynamic behaviour (web demo).
 *
 * Streams word-by-word with small delays so the UI's streaming path is
 * exercised for real.
 */
export class MockEngine implements LLMEngine {
  private script: string[] | null;
  private brain: Brain | null;
  private _info: LoadedModelInfo | null = null;
  private _busy = false;
  private cancelled = false;

  constructor(spec: MockBrainSpec = []) {
    if (Array.isArray(spec)) {
      this.script = spec.slice();
      this.brain = null;
    } else {
      this.script = null;
      this.brain = spec;
    }
  }

  get info(): LoadedModelInfo | null {
    return this._info;
  }

  get busy(): boolean {
    return this._busy;
  }

  async load(
    model: ModelDefinition & { localPath?: string },
    onProgress?: (fraction: number) => void,
  ): Promise<void> {
    onProgress?.(0.25);
    await delay(10);
    onProgress?.(0.6);
    await delay(10);
    onProgress?.(1);
    this._info = {
      id: model.id,
      name: model.name,
      contextSize: model.contextSize,
      gpu: false,
    };
  }

  async complete(options: CompletionOptions): Promise<CompletionResult> {
    if (this._busy) throw new Error('MockEngine is busy');
    this._busy = true;
    this.cancelled = false;

    const onAbort = () => {
      this.cancelled = true;
    };
    options.signal?.addEventListener('abort', onAbort);

    try {
      const text = this.script
        ? this.takeScripted()
        : await Promise.resolve(
            (this.brain as Brain)({ messages: options.messages }),
          );

      // Stream the reply in word chunks.
      const chunks = text.match(/\S+\s*/g) ?? [text];
      let full = '';
      for (const chunk of chunks) {
        if (this.cancelled || options.signal?.aborted) {
          return { text: full, stopReason: 'canceled' };
        }
        full += chunk;
        options.onToken?.(chunk, full);
        await delay(8);
      }
      if (this.cancelled || options.signal?.aborted) {
        return { text: full, stopReason: 'canceled' };
      }
      return { text: full, stopReason: 'eos' };
    } finally {
      options.signal?.removeEventListener('abort', onAbort);
      this._busy = false;
    }
  }

  private takeScripted(): string {
    const next = this.script?.shift();
    if (next === undefined) {
      throw new Error('MockEngine script exhausted — add more scripted replies');
    }
    return next;
  }

  cancel(): void {
    this.cancelled = true;
  }

  async release(): Promise<void> {
    this._info = null;
  }
}

export async function assertScriptNotCancelled(signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw new EngineCancelledError();
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
