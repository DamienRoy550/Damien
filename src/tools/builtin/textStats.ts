import type { Tool } from '../types';
import { ok, err } from '../types';

/** Text analysis for writing/reading tasks — counts, reading time, keyword density. */
export const textStats: Tool = {
  name: 'text_stats',
  description:
    'Analyze a piece of text: word count, character count, sentence count, estimated reading time, and top keywords. Useful before/after writing tasks.',
  parameters: [
    { name: 'text', type: 'string', description: 'The text to analyze', required: true },
  ],
  runsOffline: true,
  async execute(args) {
    const text = String(args.text ?? '');
    if (!text.trim()) return err('text is empty');

    const words = text.split(/\s+/).filter(Boolean);
    const sentences = text.split(/[.!?]+(?:\s|$)/).filter((s) => s.trim().length > 0);
    const chars = text.length;
    const readingMinutes = Math.max(1, Math.round(words.length / 200));

    const stop = new Set([
      'the', 'a', 'an', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'for', 'with', 'at', 'by',
      'is', 'are', 'was', 'were', 'be', 'been', 'it', 'its', 'this', 'that', 'these', 'those',
      'as', 'from', 'not', 'have', 'has', 'had', 'will', 'would', 'can', 'could', 'should',
      'i', 'you', 'he', 'she', 'we', 'they', 'my', 'your', 'our', 'their', 'do', 'does', 'did',
    ]);
    const freq = new Map<string, number>();
    for (const w of words) {
      const k = w.toLowerCase().replace(/[^a-z0-9'’-]/g, '');
      if (k.length < 3 || stop.has(k)) continue;
      freq.set(k, (freq.get(k) ?? 0) + 1);
    }
    const top = Array.from(freq.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([w, c]) => `${w}(${c})`)
      .join(', ');

    return ok(
      `Words: ${words.length} | Characters: ${chars} | Sentences: ${sentences.length} | Reading time: ~${readingMinutes} min | Top keywords: ${top || 'n/a'}`,
    );
  },
};
