import type { Tool } from '../tools/types';

/**
 * System prompt tuned for SMALL (0.5B–3B) instruction models.
 *
 * Principles:
 *  - Short. Every token competes with context space for observations.
 *  - One format, shown twice. Small models imitate; they don't infer.
 *  - Explicit stop rule: exactly one JSON object per turn.
 */
export function buildSystemPrompt(
  tools: Tool[],
  now: Date,
  timeZone?: string,
  extraInstructions?: string,
): string {
  const toolDocs = tools
    .map((t) => {
      const params = t.parameters.length
        ? t.parameters
            .map(
              (p) =>
                `    - ${p.name} (${p.type}${p.required ? '' : ', optional'}): ${p.description}`,
            )
            .join('\n')
        : '    (no parameters)';
      return `- ${t.name}: ${t.description}\n${params}`;
    })
    .join('\n');

  const when = formatDateForPrompt(now, timeZone);

  return `You are Damien, an autonomous AI assistant running COMPLETELY OFFLINE on the user's phone. You complete tasks step by step using tools.

# OUTPUT FORMAT — follow exactly
Reply with exactly ONE JSON object per turn and NOTHING else.

To use a tool:
{"thought":"<one short sentence: what you're doing and why>","tool":"<tool name>","arguments":{...}}

To give the final reply to the user:
{"thought":"<one short sentence>","answer":"<your complete reply>"}

# RULES
1. Only use tools from the list below. Never invent tools or parameters.
2. Use a tool whenever it would make your answer more accurate or useful.
3. After each tool you receive an OBSERVATION. Use it for your next step.
4. Keep "thought" under 20 words. Keep "answer" complete but concise.
5. Arguments must be valid JSON. Numbers without quotes. Strings in double quotes.
6. Do calculations with the calculator tool, never in your head.
7. The current date and time is: ${when}. Use the datetime tool if you need more.

Example turn 1:
{"thought":"I need to convert the distance.","tool":"unit_convert","arguments":{"value":5,"from_unit":"km","to_unit":"mi"}}

Example final turn:
{"thought":"I have the result.","answer":"5 km is about 3.11 miles."}

# TOOLS
${toolDocs}
${extraInstructions ? `\n# EXTRA INSTRUCTIONS FROM THE USER\n${extraInstructions}\n` : ''}`;
}

function formatDateForPrompt(now: Date, timeZone?: string): string {
  try {
    const opts: Intl.DateTimeFormatOptions = {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    };
    const formatted = new Intl.DateTimeFormat('en-GB', {
      ...opts,
      ...(timeZone ? { timeZone } : {}),
    }).format(now);
    return `${formatted}${timeZone ? ` (${timeZone})` : ''}`;
  } catch {
    return now.toISOString();
  }
}
