import type { ChatMessage } from '../../llm/types';

export interface BrainRequest {
  messages: ChatMessage[];
}

export type Brain = (req: BrainRequest) => string | Promise<string>;

export type SyncBrain = (req: BrainRequest) => string;

/**
 * The demo brain: a tiny rule-based stand-in for the on-device LLM.
 *
 * It speaks the EXACT same wire protocol as the real model
 * ({"thought","tool","arguments"} / {"thought","answer"}), so the web demo
 * exercises the full agent pipeline: parsing, tool execution, observations,
 * multi-step loops, cancellation.
 */

const MATH_RE = /(-?\d+(?:\.\d+)?(?:\s*(?:[+\-*/^%×x÷]|\*\*)\s*-?\d+(?:\.\d+)?)+)/;
const CONVERT_RE = /(-?\d+(?:\.\d+)?)\s*(?:°\s*)?([a-zA-Z°/]+)\s+(?:to|in|into|as)\s+(?:°\s*)?([a-zA-Z°/]+)/i;
const REMIND_RE = /remind me(?:\s+to)?\s+(.+?)\s+(in\s+\d+\s*(?:min(?:ute)?s?|hours?|hrs?|h|days?|d)|at\s+\d{1,2}(?::\d{2})?)/i;
const URL_RE = /(https?:\/\/[^\s"']+)/;
const NOTE_RE = /\b(?:take a note|make a note|note down|write down|remember that|remember this|save (?:a )?note)\b[:\s]*(.*)/i;

function lastMessage(messages: ChatMessage[]): ChatMessage | undefined {
  return messages[messages.length - 1];
}

function taskOf(messages: ChatMessage[]): string {
  const firstUser = messages.find((m) => m.role === 'user' && !m.content.startsWith('[OBSERVATION'));
  return firstUser?.content ?? '';
}

const CAPABILITIES = `I'm Damien, running in demo mode right now (simulated on-device brain). In the real app, a small language model runs 100% offline on your phone and drives the same tool loop you see here. Try asking me to:
• "What is 234 * 17 + sqrt(961)?" — math via the calculator tool
• "Convert 42 km to miles" — unit conversion
• "Take a note: buy oat milk tomorrow" — persistent memory
• "Remind me to stretch in 45 minutes" — notifications (device only)
• "Fetch https://example.com" — reading a web page`;

export const demoBrain: SyncBrain = ({ messages }) => {
  const last = lastMessage(messages);
  const task = taskOf(messages);

  // ---- Phase 2: an observation just came back → produce the final answer ----
  if (last && last.content.startsWith('[OBSERVATION')) {
    const observation = last.content.replace(/^\[OBSERVATION(?: - ERROR)?\]\s*/, '');
    if (last.content.startsWith('[OBSERVATION - ERROR]')) {
      return JSON.stringify({
        thought: 'The tool failed; I should tell the user honestly.',
        answer: `I hit a problem while working on that: ${observation.slice(0, 300)}`,
      });
    }
    const calcMatch = /=\s*(-?[\d,.]+)\s*$/.exec(observation);
    if (calcMatch) {
      return JSON.stringify({
        thought: 'I have the computed result.',
        answer: `${calcMatch[1]} — that's the exact result, computed on-device with the calculator tool.`,
      });
    }
    const trimmed = observation.length > 400 ? `${observation.slice(0, 400)}…` : observation;
    return JSON.stringify({
      thought: 'I have what I need.',
      answer: trimmed,
    });
  }

  // ---- Phase 1: decide the first step from the task text ----
  const math = MATH_RE.exec(task);
  if (math) {
    const expression = math[1]!.replace(/\s+/g, '').replace(/[×x]/g, '*').replace(/÷/g, '/');
    return JSON.stringify({
      thought: 'This needs exact math, so I will use the calculator.',
      tool: 'calculator',
      arguments: { expression },
    });
  }

  const conv = CONVERT_RE.exec(task);
  if (conv) {
    return JSON.stringify({
      thought: 'Unit conversion — using the converter tool.',
      tool: 'unit_convert',
      arguments: {
        value: Number(conv[1]),
        from_unit: conv[2]!.replace('°', '').replace('°', ''),
        to_unit: conv[3]!.replace('°', '').replace('°', ''),
      },
    });
  }

  const remind = REMIND_RE.exec(task);
  if (remind) {
    return JSON.stringify({
      thought: 'Scheduling a reminder on the device.',
      tool: 'schedule_reminder',
      arguments: {
        message: remind[1]!.trim(),
        when: remind[2]!.replace(/^at\s+/i, '+').replace(
          /^(\d{1,2}(?::\d{2})?)$/,
          '',
        ) || remind[2]!,
        title: 'Damien reminder',
      },
    });
  }

  const note = NOTE_RE.exec(task);
  if (note) {
    const body = (note[1] ?? '').trim() || task;
    const title = body.split(/[\n.,;]/)[0]!.slice(0, 40) || 'Note';
    return JSON.stringify({
      thought: 'The user wants this stored persistently.',
      tool: 'save_note',
      arguments: { title: `Note: ${title}`, body },
    });
  }

  const url = URL_RE.exec(task);
  if (url) {
    return JSON.stringify({
      thought: 'Fetching that page to read its contents.',
      tool: 'web_fetch',
      arguments: { url: url[1] },
    });
  }

  if (/^(hi|hello|hey|yo)\b/i.test(task.trim())) {
    return JSON.stringify({
      thought: 'Simple greeting — no tools needed.',
      answer: 'Hey! I can calculate, convert units, take notes, set reminders and fetch pages. What should I do for you?',
    });
  }

  return JSON.stringify({
    thought: 'No tool clearly applies; answering directly.',
    answer: CAPABILITIES,
  });
};
