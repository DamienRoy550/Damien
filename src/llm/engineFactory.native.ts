import type { LLMEngine } from './types';
import { LlamaEngine } from './LlamaEngine.native';

let shared: LlamaEngine | null = null;

/** Native platforms: one process-wide llama.cpp context. */
export function getEngine(): LLMEngine {
  if (!shared) shared = new LlamaEngine();
  return shared;
}
