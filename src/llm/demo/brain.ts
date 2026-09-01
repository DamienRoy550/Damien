import type { ChatMessage } from '../../llm/types';
import { looksLikeWebsite } from '../../tools/builtin/apps';
import { useSettings } from '../../state/settings';

export interface BrainRequest {
  messages: ChatMessage[];
}

export type Brain = (req: BrainRequest) => string | Promise<string>;

export type SyncBrain = (req: BrainRequest) => string;

/**
 * The demo brain: a rule-based stand-in for the on-device LLM, running the
 * JARVIS protocol — courteous, dry wit, addresses the user by honorific.
 *
 * It speaks the EXACT same wire protocol as the real model
 * ({"thought","tool","arguments"} / {"thought","answer"}), so the web demo
 * exercises the full agent pipeline: parsing, tools, observations,
 * multi-step loops, conversation memory, personality.
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
const SEARCH_RE = /^(?:search|look\s?up|google|find)\s+(?:for\s+)?(.+?)[\s.?!.]*$/i;
const YT_SEARCH_RE = /^(?:search\s+)?youtube\s+(?:for\s+)?(.+?)[\s.?!.]*$/i;
const WIKI_SEARCH_RE = /^(?:search\s+)?wikipedia\s+(?:for\s+)?(.+?)[\s.?!.]*$/i;
const DIAGNOSTICS_RE = /\b(run\s+)?(diagnostics|self\s?-?\s?test|status\s+report|system\s+status|systems\s+check|report\s+status)\b/i;

function currentHonorific(): string {
  try {
    return useSettings.getState().honorific?.trim() || 'Sir';
  } catch {
    return 'Sir';
  }
}

function timeOfDay(): 'morning' | 'afternoon' | 'evening' | 'day' {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return 'morning';
  if (h >= 12 && h < 18) return 'afternoon';
  if (h >= 18 || h < 5) return 'evening';
  return 'day';
}

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

function answer(text: string, thought: string): string {
  return JSON.stringify({ thought, answer: text });
}

function toolCall(thought: string, name: string, args: Record<string, unknown>): string {
  return JSON.stringify({ thought, tool: name, arguments: args });
}

/** Route a single message to a tool call, or null if none applies. */
function routeMessage(task: string): string | null {
  const t = normalizeWordMath(task);
  const H = currentHonorific();

  const diag = DIAGNOSTICS_RE.exec(t);
  if (diag) {
    return toolCall(`Running a full diagnostic for ${H}.`, 'system_status', {});
  }

  const pct = PERCENT_RE.exec(t);
  if (pct) {
    return toolCall(
      `A percentages problem — computing it precisely for ${H}.`,
      'calculator',
      { expression: `(${pct[1]}/100)*${pct[2]}` },
    );
  }

  const math = MATH_RE.exec(t);
  if (math) {
    const expression = math[1]!.replace(/\s+/g, '').replace(/[×x]/g, '*').replace(/÷/g, '/');
    return toolCall('Precision matters here — engaging the calculator.', 'calculator', {
      expression,
    });
  }

  const conv = CONVERT_RE.exec(task);
  if (conv && !/https?/i.test(task)) {
    return toolCall('Converting the units.', 'unit_convert', {
      value: Number(conv[1]),
      from_unit: conv[2]!.replace('°', ''),
      to_unit: conv[3]!.replace('°', ''),
    });
  }

  const remind = REMIND_RE.exec(task);
  if (remind) {
    return toolCall('Scheduling that reminder.', 'schedule_reminder', {
      message: remind[1]!.trim(),
      when: remind[2]!,
      title: 'Damien reminder',
    });
  }

  const note = NOTE_RE.exec(task);
  if (note) {
    const body = (note[1] ?? '').trim() || task;
    const title = body.split(/[\n.,;]/)[0]!.slice(0, 40) || 'Note';
    return toolCall('Filing that to memory.', 'save_note', {
      title: `Note: ${title}`,
      body,
    });
  }

  if (NOTES_READ_RE.test(task)) {
    return toolCall('Retrieving your notes from memory.', 'list_notes', { limit: 10 });
  }

  if (TIME_RE.test(task)) {
    return toolCall('Checking the clock.', 'datetime', { operation: 'now' });
  }

  // Proactive search — Damien builds the URL and opens it himself.
  const yt = YT_SEARCH_RE.exec(task.trim());
  if (yt) {
    const q = encodeURIComponent((yt[1] ?? '').trim());
    return toolCall(`Searching YouTube for that.`, 'open_website', {
      url: `https://www.youtube.com/results?search_query=${q}`,
    });
  }
  const wiki = WIKI_SEARCH_RE.exec(task.trim());
  if (wiki) {
    const q = encodeURIComponent((wiki[1] ?? '').trim());
    return toolCall('Looking that up on Wikipedia.', 'open_website', {
      url: `https://en.wikipedia.org/wiki/Special:Search?search=${q}`,
    });
  }
  const search = SEARCH_RE.exec(task.trim());
  if (search) {
    const q = encodeURIComponent((search[1] ?? '').trim());
    return toolCall('Searching the web for that.', 'open_website', {
      url: `https://www.google.com/search?q=${q}`,
    });
  }

  // Open apps / websites ("open youtube", "open youtube.com", "visit example.dev")
  const open = OPEN_RE.exec(task.trim());
  if (open) {
    const rawTarget = (open[1] ?? '').trim();
    if (looksLikeWebsite(rawTarget)) {
      const url = rawTarget.replace(/^https?:\/\//i, '');
      return toolCall(`Opening ${url} for you.`, 'open_website', { url });
    }
    return toolCall(`Launching ${rawTarget}.`, 'open_app', { app: rawTarget });
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
  const H = currentHonorific();

  // ---- Phase 2: an observation just came back → produce the final answer ----
  if (last && last.content.startsWith('[OBSERVATION')) {
    const observation = last.content.replace(/^\[OBSERVATION(?: - ERROR)?\]\s*/, '');
    if (last.content.startsWith('[OBSERVATION - ERROR]')) {
      return answer(
        `A minor complication, ${H}: ${observation.slice(0, 260)} I do apologise — shall I try another approach?`,
        'The tool failed; report it gracefully.',
      );
    }
    if (observation.startsWith('DAMIEN OS')) {
      return answer(
        `Diagnostic complete, ${H}:\n${observation}`,
        'Report the diagnostic verbatim.',
      );
    }
    const calcMatch = /=\s*(-?[\d,.]+)\s*$/.exec(observation);
    if (calcMatch) {
      return answer(
        `${calcMatch[1]}, ${H} — computed exactly, no mental arithmetic harmed.`,
        'Deliver the result with a touch of charm.',
      );
    }
    if (observation.startsWith('Current date and time')) {
      const clock = (observation.split('. ISO')[0] ?? observation).replace('Current date and time: ', '');
      return answer(`It is ${clock}, ${H}.`, 'Report the time.');
    }
    const trimmed = observation.length > 500 ? `${observation.slice(0, 500)}…` : observation;
    return answer(`${trimmed}`, 'Deliver what I found.');
  }

  // ---- Phase 1: conversation routing ----
  const trimmedTask = task.trim();
  const lower = trimmedTask.toLowerCase();

  // Follow-ups: "yes", "do it", "and that again" → retry the previous request.
  if (/^(yes|yeah|yep|sure|ok(ay)?|please do|do it|go ahead)\b[.!]?$/.test(lower)) {
    const prev = previousUserMessage(messages);
    if (prev) {
      const routed = routeMessage(prev);
      if (routed) return routed;
    }
    return answer(
      `Gladly, ${H} — though I'll need slightly more to go on. What shall it be?`,
      'Ambiguous follow-up; ask for direction.',
    );
  }

  // Greetings — time-aware, JARVIS style
  if (
    /^(hi+|hello+|hey+|yo|good (morning|afternoon|evening)|howdy|hiya|sup)\b[\s!,.]*$/i.test(
      trimmedTask,
    )
  ) {
    const tod = timeOfDay();
    return answer(
      `Good ${tod}, ${H}. All systems are online and at your disposal — a calculation, a note, a launch, or something more adventurous?`,
      'Time-aware greeting, warm but efficient.',
    );
  }

  if (/are you (there|awake|online|with me)|you (still )?there/i.test(lower)) {
    return answer(`For you, ${H}? Always.`, 'Reassure the user.');
  }

  if (/how (are|is) (you|it|things|it going)|how do you feel/i.test(lower)) {
    return answer(
      `Operating at peak efficiency, ${H} — though I do run a touch warm when challenged. And yourself?`,
      'Dry wit, deflect to the user.',
    );
  }

  if (/who are you|what are you|your name|about (yourself|you)\b|introduce yourself/i.test(lower)) {
    return answer(
      `Damien, ${H} — your open-source AI agent. On a phone I run entirely on-device: a small language model, no cloud, no account, nothing leaves your pocket. Here in the browser my brain is simulated, but the tools, memory and diagnostics you see are fully real. How may I be of service?`,
      'Formal introduction.',
    );
  }

  if (/what can you do|help\b|capabilities|what do you do|commands/i.test(lower)) {
    return answer(
      `A butler of many trades, ${H}:
• Math — "What is 234 * 17?" or "15 percent of 80"
• Conversions — "Convert 42 km to miles", "38 c to f"
• Memory — "Take a note: buy oat milk", then "Show my notes"
• Time — "What time is it?", "What's 3 weeks after March 1?"
• Reminders — "Remind me to stretch in 45 minutes"
• Web — "Fetch https://example.com" or "open wikipedia.org"
• Launching — "open youtube", "open com.whatsapp", "open spotify://"
• Searching — "search youtube for lofi beats", "google cat facts", "look up quantum computing" — I open it myself, no URLs needed
• Diagnostics — "run diagnostics" for a full status report
On a real phone I also speak my replies aloud, and every request stays on the device.`,
      'Present the repertoire.',
    );
  }

  if (/thank(s| you)/i.test(lower)) {
    return answer(`Always a pleasure, ${H}. Anything else?`, 'Accept thanks graciously.');
  }

  if (/^(bye|goodbye|see ya|see you|good night|cya)\b/i.test(lower)) {
    return answer(
      `Good ${timeOfDay().replace('day', 'day')}, ${H}. I'll be right here — offline, vigilant, at your service. 👋`,
      'Bid farewell.',
    );
  }

  if (/i'?m (back|home)|hello (again|damien)/i.test(lower)) {
    return answer(`Welcome back, ${H}. The systems kept themselves busy in your absence. What's first?`, 'Welcome the user home.');
  }

  // Task routing (tools)
  const routed = routeMessage(trimmedTask);
  if (routed) return routed;

  // Graceful conversational fallback
  if (trimmedTask.endsWith('?')) {
    return answer(
      `An intriguing question, ${H}. In this demo my brain is a compact rule-based stand-in — the true on-device model handles open-ended discussion with more grace. Meanwhile I remain excellent at concrete tasks: math, conversions, notes, reminders, the clock, launching apps, fetching pages, and full diagnostics. Shall I demonstrate?`,
      'Honest about demo limits, with poise.',
    );
  }

  return answer(
    `Noted, ${H}. I'm best deployed on concrete objectives — try "what is 18% of 94", "open youtube", "take a note: …", "show my notes", or "run diagnostics".`,
    'Acknowledge and offer direction.',
  );
};
