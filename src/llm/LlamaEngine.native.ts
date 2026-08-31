import { initLlama, type LlamaContext, type ContextParams } from 'llama.rn';
import type {
  LLMEngine,
  CompletionOptions,
  CompletionResult,
  LoadedModelInfo,
  ModelDefinition,
} from './types';
import { EngineCancelledError } from './types';

/**
 * On-device engine backed by llama.cpp through llama.rn.
 *
 * Notes:
 *  - Uses the model's own chat template (jinja) so every catalog model
 *    is prompted the way its authors intended.
 *  - `forceJson` maps to llama.cpp's response_format json_object, which is
 *    grammar-constrained — hugely improves small-model tool calling.
 *  - All calls are defensive: if jinja path fails we fall back to a
 *    manually formatted prompt via getFormattedChat().
 */
export class LlamaEngine implements LLMEngine {
  private ctx: LlamaContext | null = null;
  private _info: LoadedModelInfo | null = null;
  private _busy = false;
  private stopping = false;

  get info(): LoadedModelInfo | null {
    return this._info;
  }

  get busy(): boolean {
    return this._busy;
  }

  async load(
    model: ModelDefinition & { localPath: string },
    onProgress?: (fraction: number) => void,
  ): Promise<void> {
    if (this.ctx) {
      await this.release();
    }
    const params: ContextParams = {
      model: model.localPath,
      n_ctx: model.contextSize,
      n_batch: 512,
      n_threads: Math.max(2, Math.min(6, Math.ceil((navigator_hardware_concurrency() || 4) / 2))),
      // Let llama.cpp offload everything it can to GPU (Metal / OpenCL / Vulkan)
      n_gpu_layers: 99,
      use_progress_callback: true,
      flash_attn_type: 'auto',
    };

    const ctx = await initLlama(params, (progress: number) => {
      onProgress?.(Math.min(1, progress / 100));
    });

    this.ctx = ctx;
    this._info = {
      id: model.id,
      name: model.name,
      contextSize: model.contextSize,
      gpu: Boolean(ctx.gpu),
    };
  }

  async complete(options: CompletionOptions): Promise<CompletionResult> {
    const ctx = this.ctx;
    if (!ctx) throw new Error('Engine not loaded — call load() first');
    if (this._busy) throw new Error('Engine is busy with another completion');
    this._busy = true;
    this.stopping = false;

    const onAbort = () => this.cancel();
    options.signal?.addEventListener('abort', onAbort);

    let accumulated = '';
    let stopReason: CompletionResult['stopReason'] = 'eos';

    try {
      const messages = options.messages.map((m) => ({ role: m.role, content: m.content }));
      const base = {
        n_predict: options.maxTokens,
        temperature: options.temperature,
        stop: options.stopSequences ?? ['</s>'],
        onToken: () => {
          // streaming handled via callback param below
        },
      };

      const callback = (data: { token?: string }) => {
        const piece = data.token ?? '';
        if (!piece) return;
        accumulated += piece;
        options.onToken?.(piece, accumulated);
      };

      let result: { text?: string };
      try {
        // Preferred path: OAI-style messages + the GGUF's built-in jinja template.
        result = await ctx.completion(
          {
            ...base,
            messages,
            jinja: true,
            ...(options.forceJson ? { response_format: { type: 'json_object' } } : {}),
          },
          callback,
        );
      } catch {
        // Fallback: format the prompt ourselves without jinja tooling.
        const formatted = await ctx.getFormattedChat(messages);
        const promptText =
          typeof formatted === 'string' ? formatted : (formatted as { prompt?: string }).prompt ?? '';
        result = await ctx.completion({ ...base, prompt: promptText }, callback);
      }

      accumulated = accumulated || (result.text ?? '');
      stopReason = this.stopping ? 'canceled' : 'eos';
      return { text: accumulated, stopReason };
    } catch (e) {
      if (e instanceof EngineCancelledError || this.stopping) {
        return { text: accumulated, stopReason: 'canceled' };
      }
      throw e;
    } finally {
      options.signal?.removeEventListener('abort', onAbort);
      this._busy = false;
    }
  }

  cancel(): void {
    this.stopping = true;
    this.ctx?.stopCompletion().catch(() => undefined);
  }

  async release(): Promise<void> {
    this.cancel();
    const ctx = this.ctx;
    this.ctx = null;
    this._info = null;
    if (ctx) {
      await ctx.release().catch(() => undefined);
    }
  }
}

/** RN has no `navigator`; hardware threads come from the platform. */
function navigator_hardware_concurrency(): number | undefined {
  try {
    // react-native-worklets / Hermes expose this in newer builds; be safe.
    const g = globalThis as { navigator?: { hardwareConcurrency?: number } };
    return g.navigator?.hardwareConcurrency;
  } catch {
    return undefined;
  }
}
