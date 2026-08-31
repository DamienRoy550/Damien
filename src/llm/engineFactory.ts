import type { LLMEngine } from './types';
import { MockEngine } from './demo/MockEngine';
import { demoBrain } from './demo/brain';

let shared: MockEngine | null = null;

/**
 * Base resolution (web bundle + tooling): no native llama.cpp, so run the
 * simulated brain. The entire agent pipeline (parser, tools, loop, UI) is
 * identical — only the token generator differs. Native builds override this
 * via engineFactory.native.ts.
 */
export function getEngine(): LLMEngine {
  if (!shared) shared = new MockEngine(demoBrain);
  return shared;
}
