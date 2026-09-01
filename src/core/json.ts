/**
 * Robust JSON extraction from LLM output.
 *
 * Small models frequently wrap JSON in code fences, prepend chatter, use
 * trailing commas, or emit smart quotes. These helpers extract and repair
 * the first JSON object found in a blob of text.
 */

/** Strip markdown code fences like ```json ... ``` */
export function stripCodeFences(text: string): string {
  const fenced = /```(?:json|JSON)?\s*([\s\S]*?)```/.exec(text);
  return fenced && fenced[1] ? fenced[1].trim() : text;
}

/**
 * Extract the first balanced `{ ... }` block, ignoring braces inside strings.
 * Returns null if no balanced object exists.
 */
export function extractJsonObject(text: string): string | null {
  const start = text.indexOf('{');
  if (start === -1) return null;

  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === '\\') {
      if (inString) escaped = true;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}

/** Lightweight repairs for near-JSON emitted by small models. */
export function repairJson(input: string): string {
  let s = input.trim();
  // Normalise smart quotes that models sometimes emit.
  s = s.replace(/[\u201C\u201D]/g, '"').replace(/[\u2018\u2019]/g, "'");
  // Trailing commas before } or ]
  s = s.replace(/,\s*([}\]])/g, '$1');
  // JS-style single-quoted keys/values -> double quotes (only outer quotes).
  // Applied conservatively: only if the string still fails normal JSON.parse
  // is this worth it, so the caller decides when to run repair.
  return s;
}

/**
 * Try to parse a JSON object out of free-form model text.
 * Returns the parsed object or null.
 */
export function parseJsonObjectLoose(text: string): Record<string, unknown> | null {
  const candidates: string[] = [];
  const stripped = stripCodeFences(text);
  candidates.push(stripped);
  const extracted = extractJsonObject(stripped);
  if (extracted) candidates.push(extracted);

  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // fall through to repair
    }
    try {
      const repaired = repairJson(candidate);
      const parsed = JSON.parse(repaired);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // continue
    }
  }
  return null;
}
