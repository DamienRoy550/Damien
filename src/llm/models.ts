import type { ModelDefinition } from './types';

export type { ModelDefinition };

/**
 * Curated catalog of small, phone-ready GGUF models.
 *
 * Selection criteria:
 *  - Permissive licenses first (Apache-2.0, MIT) for an OSS project.
 *  - Single-file GGUF downloads (no shard juggling on device).
 *  - Proven instruction following at Q4_K_M on mobile-class hardware.
 *
 * Downloaded from Hugging Face at runtime by the app (not bundled).
 */
export const MODEL_CATALOG: ModelDefinition[] = [
  {
    id: 'qwen2.5-0.5b',
    name: 'Qwen 2.5 Instruct 0.5B',
    description:
      'Featherweight. Fast even on old phones, ~400 MB. Best first model for testing Damien end to end.',
    url: 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf',
    sizeBytes: 400_000_000,
    fileName: 'qwen2.5-0.5b-instruct-q4_k_m.gguf',
    parameterCount: '0.5B',
    quantization: 'Q4_K_M',
    license: 'Apache-2.0',
    contextSize: 2048,
    minDevice: 'any',
  },
  {
    id: 'qwen2.5-1.5b',
    name: 'Qwen 2.5 Instruct 1.5B',
    description:
      'The sweet spot. Reliable tool calling and multi-step reasoning at ~1 GB. Recommended default.',
    url: 'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf',
    sizeBytes: 1_000_000_000,
    fileName: 'qwen2.5-1.5b-instruct-q4_k_m.gguf',
    parameterCount: '1.5B',
    quantization: 'Q4_K_M',
    license: 'Apache-2.0',
    contextSize: 3072,
    recommended: true,
    minDevice: '4GB+ RAM',
  },
  {
    id: 'smollm2-1.7b',
    name: 'SmolLM2 Instruct 1.7B',
    description:
      'Hugging Face\'s fully-open model (data + training code). Strong chat quality, ~1.1 GB.',
    url: 'https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF/resolve/main/smollm2-1.7b-instruct-q4_k_m.gguf',
    sizeBytes: 1_100_000_000,
    fileName: 'smollm2-1.7b-instruct-q4_k_m.gguf',
    parameterCount: '1.7B',
    quantization: 'Q4_K_M',
    license: 'Apache-2.0',
    contextSize: 3072,
    minDevice: '4GB+ RAM',
  },
  {
    id: 'llama32-1b',
    name: 'Llama 3.2 Instruct 1B',
    description: 'Meta\'s compact model. Good general chat, ~800 MB.',
    url: 'https://huggingface.co/lmstudio-community/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf',
    sizeBytes: 810_000_000,
    fileName: 'Llama-3.2-1B-Instruct-Q4_K_M.gguf',
    parameterCount: '1B',
    quantization: 'Q4_K_M',
    license: 'Llama 3.2 Community',
    contextSize: 3072,
    minDevice: '4GB+ RAM',
  },
  {
    id: 'qwen2.5-3b',
    name: 'Qwen 2.5 Instruct 3B',
    description:
      'Smartest option, ~2 GB and slower. For flagship phones — noticeably better at complex multi-step tasks.',
    url: 'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf',
    sizeBytes: 2_000_000_000,
    fileName: 'qwen2.5-3b-instruct-q4_k_m.gguf',
    parameterCount: '3B',
    quantization: 'Q4_K_M',
    license: 'Apache-2.0',
    contextSize: 4096,
    minDevice: '6GB+ RAM',
  },
];

export function findModel(id: string | undefined | null): ModelDefinition | undefined {
  if (!id) return undefined;
  return MODEL_CATALOG.find((m) => m.id === id);
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  if (bytes >= 1e3) return `${Math.round(bytes / 1e3)} KB`;
  return `${Math.round(bytes)} B`;
}
