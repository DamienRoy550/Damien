import { MODEL_CATALOG, findModel, formatBytes } from '../src/llm/models';

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
