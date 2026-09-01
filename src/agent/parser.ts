import { parseJsonObjectLoose } from '../core/json';
import type { ToolName, JsonRecord } from '../tools/types';
import type { ModelReply } from './types';

/**
 * Parse one model turn into the Damien wire protocol.
 *
 * Protocol (see prompts.ts):
 *   {"thought": "...", "tool": "<name>", "arguments": {...}}
 *   {"thought": "...", "answer": "..."}
 *
 * Falls back gracefully: anything unparseable becomes a plain answer so the
 * loop never gets stuck on a malformed turn.
 */
export function parseModelReply(rawText: string): ModelReply {
  const raw = rawText.trim();
  const obj = parseJsonObjectLoose(raw);

  if (obj) {
    const thought = typeof obj.thought === 'string' ? obj.thought : undefined;

    const tool = obj.tool;
    if (typeof tool === 'string' && tool.length > 0) {
      const args = obj.arguments;
      const arguments_: JsonRecord =
        args && typeof args === 'object' && !Array.isArray(args)
          ? (args as JsonRecord)
          : {};
      return { kind: 'tool', thought, tool: tool as ToolName, arguments: arguments_, raw };
    }

    const answer = obj.answer;
    if (typeof answer === 'string' && answer.length > 0) {
      return { kind: 'answer', thought, answer, raw };
    }

    // Object with neither tool nor answer: treat a "thought"-only object as a
    // direct answer, otherwise fall through to raw-text-as-answer.
    if (thought) {
      return { kind: 'answer', thought, answer: thought, raw };
    }
  }

  // The model just talked. That's fine — a plain reply IS an answer.
  if (raw.length > 0) {
    return { kind: 'answer', answer: stripProtoReminders(raw), raw };
  }

  return { kind: 'answer', answer: '', raw };
}

/** Remove protocol artifacts small models sometimes echo back. */
function stripProtoReminders(text: string): string {
  return text
    .replace(/^```(?:json)?\s*/i, '')
    .replace(/```\s*$/i, '')
    .trim();
}
