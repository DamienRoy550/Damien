import { buildSystemPrompt } from '../src/agent/prompts';
import type { Tool } from '../src/tools/types';
import { MODEL_CATALOG } from '../src/llm/models';
import { findModel, formatBytes } from '../src/llm/models';

const dummyTool: Tool = {
  name: 'calculator',
  description: 'Evaluate math.',
  parameters: [
    { name: 'expression', type: 'string', description: 'The expression', required: true },
  ],
  runsOffline: true,
  execute: async () => ({ ok: true, output: '= 4' }),
};

describe('persona in the system prompt', () => {
  it('is neutral by default', () => {
    const p = buildSystemPrompt([dummyTool], new Date('2026-03-01T12:00:00Z'));
    expect(p).not.toContain('JARVIS');
    expect(p).toContain('You are Damien');
  });

  it('adds the JARVIS protocol with honorific', () => {
    const p = buildSystemPrompt([dummyTool], new Date(), undefined, undefined, {
      style: 'jarvis',
      honorific: 'Boss',
    });
    expect(p).toContain('JARVIS PROTOCOL');
    expect(p).toContain('"Boss"');
    expect(p).toContain('OUTPUT FORMAT'); // core protocol intact
    expect(p).toContain('calculator'); // tool docs intact
  });

  it('defaults the honorific to Sir', () => {
    const p = buildSystemPrompt([dummyTool], new Date(), undefined, undefined, {
      style: 'jarvis',
    });
    expect(p).toContain('"Sir"');
  });
});

describe('model catalog', () => {
  it('has a recommended model', () => {
    expect(MODEL_CATALOG.some((m) => m.recommended)).toBe(true);
  });

  it('uses single-file GGUF URLs on https', () => {
    for (const m of MODEL_CATALOG) {
      expect(m.url).toMatch(/^https:\/\/huggingface\.co\/.+\.(gguf)$/);
      expect(m.contextSize).toBeGreaterThanOrEqual(2048);
      expect(m.fileName.endsWith('.gguf')).toBe(true);
    }
  });

  it('findModel resolves and rejects correctly', () => {
    expect(findModel('qwen2.5-1.5b')?.name).toContain('Qwen');
    expect(findModel('nope')).toBeUndefined();
    expect(findModel(null)).toBeUndefined();
  });

  it('formats sizes', () => {
    expect(formatBytes(500)).toBe('500 B');
    expect(formatBytes(400_000_000)).toBe('400 MB');
    expect(formatBytes(2_000_000_000)).toBe('2.0 GB');
  });
});
