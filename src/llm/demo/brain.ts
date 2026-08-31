import type { ChatMessage } from '../../llm/types';
import { looksLikeWebsite } from '../../tools/builtin/apps';

export interface BrainRequest {
  messages: ChatMessage[];
}

export type Brain = (req: BrainRequest) => string | Promise<string>;

export type SyncBrain = (req: BrainRequest) => string;

/**
 * The demo brain: a small rule-based conversationalist that stands in for
 * the on-device LLM on the web.
 *
 * It speaks the EXACT same wire protocol as the real model
 * ({"thought","tool","arguments"} / {"thought","answer"}), so the web demo
 * exercises the full agent pipeline: parsing, tool execution, observations,
 * multi-step loops, conversation memory.
 */

const MATH_RE = /(-?\d+(?:\.\d+)?(?:\s*(?:[+\-*/^%×x÷]|\*\*)\s*-?\d+(?:\.\d+)?)+)/;
const PERCENT_RE = /([\d.]+)\s*(?:%|percent)\s*(?:of)\s*([\d.]+)/i;
const CONVERT_RE = /(-?\d+(?:\.\d+)?)\s*(?:°\s*)?([a-zA-Z°/]+)\s+(?:to|in|into|as)\s+(?:°\s*)?([a-zA-Z°/]+)/i;
const REMIND_RE = /remind me(?:\s+to)?\s+(.+?)\s+(in\s+\d+\s*(?:min(?:ute)?s?|hours?|hrs?|h|days?|d)|at\s+\d{1,2}(?::\d{2})?)/i;
const URL_RE = /(https?:\/\/[^\s"']+)/;
const NOTE_RE = /\b(?:take a note|make a note|note down|write down|remember that|remember this|save (?:a )?note)\b[:\s]*(.*)/i;
const NOTES_READ_RE = /\b(?:my notes|list notes|show notes|read (?:my )?notes|what did i (?:note|save|write)|what have i saved)\b/i;
const TIME_RE = /\b(?:what(?:'s| is)?(?: the)? time|what(?:'s| is)?(?: the)? date|what day|current time|current date|today'?s date|what time is it)\b/i;
const OPEN_RE = /^(?:please\s+)?(?:open|launch|start|go\s+to|visit|browse(?:\s+to)?|take\s+me\s+to)\s+(?:the\s+)?(.+?)[\s.!]*$/i;

function lastMessage(messages: ChatMessage[]): ChatMessage | undefined {
  return messages[messages.length - 1];
}

/** The current task = newest user message that is not a tool observation. */
function taskOf(messages: ChatMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'user' && !m.content.startsWith('[OBSERVATION')) {
      return m.content;
    }
  }
  return '';
}

/** The previous user message, for follow-ups like "yes, do that". */
function previousUserMessage(messages: ChatMessage[]): string {
  let seenCurrent = false;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'user' || m.content.startsWith('[OBSERVATION')) continue;
    if (!seenCurrent) {
      seenCurrent = true;
      continue;
    }
    return m.content;
  }
  return '';
}

/** Word math → symbols, so "12 times 8" becomes computable. */
function normalizeWordMath(text: string): string {
  return text
    .replace(/\bplus\b/gi, '+')
    .replace(/\bminus\b/gi, '-')
    .replace(/\b(?:times|multiplied by)\b/gi, '*')
    .replace(/\b(?:divided by|over)\b/gi, '/')
    .replace(/\bto the power of\b/gi, '^');
}

const CAPABILITIES = `Nice to meet you! I'm Damien — an AI agent that runs entirely on your phone. In this browser demo my brain is simulated, but every tool and result is real. Things you can ask me:
• Math — "What is 234 * 17?" or "15 percent of 80"
• Conversions — "Convert 42 km to miles", "38 c to f"
• Memory — "Take a note: buy oat milk", then later "Show my notes"
• Time — "What time is it?", "What's 3 weeks after March 1?"
• Reminders — "Remind me to stretch in 45 minutes"
• Web — "Fetch https://example.com" or "open wikipedia.org"
• Launch — "open youtube", "open com.whatsapp", "open spotify" (on a phone this launches the real app; in this browser preview a site opens in a new tab)
On a real phone, a small language model runs 100% offline and drives this same loop — no cloud, no account.`;

function answer(text: string, thought: string): string {
  return JSON.stringify({ thought, answer: text });
}

function toolCall(
  thought: string,
  name: string,
  args: Record<string, unknown>,
): string {
  return JSON.stringify({ thought, tool: name, arguments: args });
}

/** Route a single message to a tool call, or null if none applies. */
function routeMessage(task: string): string | null {
  const t = normalizeWordMath(task);

  const pct = PERCENT_RE.exec(t);
  if (pct) {
    return toolCall(
      'Percentage — I will compute it exactly with the calculator.',
      'calculator',
      { expression: `(${pct[1]}/100)*${pct[2]}` },
    );
  }

  const math = MATH_RE.exec(t);
  if (math) {
    const expression = math[1]!.replace(/\s+/g, '').replace(/[×x]/g, '*').replace(/÷/g, '/');
    return toolCall('This needs exact math, so I will use the calculator.', 'calculator', {
      expression,
    });
  }

  const conv = CONVERT_RE.exec(task);
  if (conv && !/https?/i.test(task)) {
    return toolCall('Unit conversion — using the converter tool.', 'unit_convert', {
      value: Number(conv[1]),
      from_unit: conv[2]!.replace('°', ''),
      to_unit: conv[3]!.replace('°', ''),
    });
  }

  const remind = REMIND_RE.exec(task);
  if (remind) {
    return toolCall('Scheduling a reminder on the device.', 'schedule_reminder', {
      message: remind[1]!.trim(),
      when: remind[2]!,
      title: 'Damien reminder',
    });
  }

  const note = NOTE_RE.exec(task);
  if (note) {
    const body = (note[1] ?? '').trim() || task;
    const title = body.split(/[\n.,;]/)[0]!.slice(0, 40) || 'Note';
    return toolCall('The user wants this stored persistently.', 'save_note', {
      title: `Note: ${title}`,
      body,
    });
  }

  if (NOTES_READ_RE.test(task)) {
    return toolCall('Reading the saved notes from memory.', 'list_notes', { limit: 10 });
  }

  if (TIME_RE.test(task)) {
    return toolCall('Checking the clock.', 'datetime', { operation: 'now' });
  }

  // Open apps / websites ("open youtube", "open youtube.com", "visit example.dev")
  const open = OPEN_RE.exec(task.trim());
  if (open) {
    const rawTarget = (open[1] ?? '').trim();
    if (looksLikeWebsite(rawTarget)) {
      const url = rawTarget.replace(/^https?:\/\//i, '');
      return toolCall(`Opening the website ${url}.`, 'open_website', { url });
    }
    return toolCall(`Launching the app ${rawTarget}.`, 'open_app', { app: rawTarget });
  }

  const url = URL_RE.exec(task);
  if (url) {
    return toolCall('Fetching that page to read its contents.', 'web_fetch', {
      url: url[1],
    });
  }

  return null;
}

export const demoBrain: SyncBrain = ({ messages }) => {
  const last = lastMessage(messages);
  const task = taskOf(messages);

  // ---- Phase 2: an observation just came back → produce the final answer ----
  if (last && last.content.startsWith('[OBSERVATION')) {
    const observation = last.content.replace(/^\[OBSERVATION(?: - ERROR)?\]\s*/, '');
    if (last.content.startsWith('[OBSERVATION - ERROR]')) {
      return answer(
        `I hit a problem while working on that: ${observation.slice(0, 300)}`,
        'The tool failed; I should tell the user honestly.',
      );
    }
    const calcMatch = /=\s*(-?[\d,.]+)\s*$/.exec(observation);
    if (calcMatch) {
      return answer(
        `${calcMatch[1]} — that's the exact result, computed with the calculator tool.`,
        'I have the computed result.',
      );
    }
    if (observation.startsWith('Current date and time')) {
      return answer(observation, 'The clock says:');
    }
    const trimmed = observation.length > 500 ? `${observation.slice(0, 500)}…` : observation;
    return answer(trimmed, 'I have what I need.');
  }

  // ---- Phase 1: conversation routing ----
  const trimmedTask = task.trim();
  const lower = trimmedTask.toLowerCase();

  // Follow-ups: "yes", "do it", "and that again" → retry the previous request.
  if (/^(yes|yeah|yep|sure|ok(ay)?|please do|do it|go ahead)\b[.!]?$/.test(lower)) {
    const prev = previousUserMessage(messages);
    if (prev) {
      const routed = routeMessage(prev);
      if (routed) return routed.replace(/"thought":"([^"]*)"/, `"thought":"Following up on your last request — $1"`);
    }
    return answer('Happy to — could you say a bit more about what you\'d like me to do?', 'Ambiguous follow-up.');
  }

  // Greetings (possibly "hello damien")
  if (/^(hi+|hello+|hey+|yo|good (morning|afternoon|evening)|howdy|hiya|sup)\b[\s!,.]*$/i.test(trimmedTask)) {
    return answer(
      'Hey there! Good to see you. What can I do for you — a calculation, a note, a reminder, a conversion?',
      'Friendly greeting, no tools needed.',
    );
  }

  if (/how (are|is) (you|it|things|it going)/i.test(lower)) {
    return answer(
      'Running warm but happy — inference is compute, after all. 😄 More importantly: how can I help you today?',
      'Casual small talk.',
    );
  }

  if (/who are you|what are you|your name|about (yourself|you)\b/i.test(lower)) {
    return answer(
      'I\'m Damien — an open-source AI agent that lives on your phone. On a real device, a small language model runs fully offline (no cloud, no account) and drives the tool loop you\'re seeing here. In this web demo my brain is simulated, but the tools, memory and steps are all real.',
      'Telling the user who I am.',
    );
  }

  if (/what can you do|help\b|capabilities|what do you do|commands/i.test(lower)) {
    return answer(CAPABILITIES, 'Listing what I can do.');
  }

  if (/thank(s| you)/i.test(lower)) {
    return answer('Anytime! That\'s what I\'m here for. Anything else?', 'You\'re welcome.');
  }

  if (/^(bye|goodbye|see ya|see you|good night|cya)\b/i.test(lower)) {
    return answer('See you soon — I\'ll be right here, offline as always. 👋', 'Saying goodbye.');
  }

  // Task routing (tools)
  const routed = routeMessage(trimmedTask);
  if (routed) return routed;

  // Graceful conversational fallback
  if (trimmedTask.endsWith('?')) {
    return answer(
      'Good question. In this demo my brain is a compact rule-based stand-in, so deep open-ended answers are where it hands over to the real on-device model. I can, however, do concrete things right now: math ("12 times 8"), conversions ("30 c to f"), notes ("take a note: …"), reminders ("remind me to call mum in 1h"), the time, and fetching a URL. Want to try one?',
      'Honest about demo limits, offering what I can do.',
    );
  }

  return answer(
    `Got it — "${trimmedTask.slice(0, 80)}${trimmedTask.length > 80 ? '…' : ''}". I'm a lightweight demo brain, so I'm best at concrete tasks: try "what is 18% of 94", "convert 5 km to mi", "take a note: …", "show my notes", or "what time is it".`,
    'Acknowledging and steering toward what I can do.',
  );
};
