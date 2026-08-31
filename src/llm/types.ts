/**
 * Platform-agnostic LLM engine interface.
 *
 * Implementations:
 *  - LlamaEngine (.native.ts)  → llama.cpp on-device via llama.rn
 *  - MockEngine (demo/)        → scripted/simulated engine for tests + web demo
 */

export type ChatRole = 'system' | 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface ModelDefinition {
  id: string;
  name: string;
  /** Human description for the setup screen. */
  description: string;
  /** Download URL (GGUF, single file). */
  url: string;
  /** Approx download size in bytes. */
  sizeBytes: number;
  /** GGUF filename we store locally. */
  fileName: string;
  parameterCount: string;
  quantization: string;
  license: string;
  /** Suggested context window (small phones → small contexts). */
  contextSize: number;
  recommended?: boolean;
  /** Minimum device class hint, shown in UI. */
  minDevice: 'any' | '4GB+ RAM' | '6GB+ RAM';
}

export interface LoadedModelInfo {
  id: string;
  name: string;
  contextSize: number;
  gpu: boolean;
}

export interface CompletionOptions {
  messages: ChatMessage[];
  maxTokens: number;
  temperature: number;
  stopSequences?: string[];
  /** When true, engine should constrain output to a JSON object if it can. */
  forceJson?: boolean;
  onToken?: (delta: string, full: string) => void;
  signal?: AbortSignal;
}

export interface CompletionResult {
  text: string;
  stopReason: 'eos' | 'stop' | 'canceled' | 'length' | 'error';
}

export interface LLMEngine {
  readonly info: LoadedModelInfo | null;
  /** True while a completion is in flight. */
  readonly busy: boolean;
  load(
    model: ModelDefinition & { localPath: string },
    onProgress?: (fraction: number) => void,
  ): Promise<void>;
  complete(options: CompletionOptions): Promise<CompletionResult>;
  cancel(): void;
  release(): Promise<void>;
}

export class EngineCancelledError extends Error {
  constructor() {
    super('Generation cancelled');
    this.name = 'EngineCancelledError';
  }
}
