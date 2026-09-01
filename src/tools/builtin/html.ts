/**
 * Minimal HTML → text conversion. Handles script/style removal, common block
 * structure, entities, and collapses whitespace. No external dependencies.
 */

export function stripHtml(html: string): string {
  let s = html;

  // Drop non-content blocks entirely
  s = s.replace(/<(script|style|noscript|svg|iframe|head|nav|footer)[\s\S]*?<\/\1>/gi, ' ');
  // HTML comments
  s = s.replace(/<!--[\s\S]*?-->/g, ' ');
  // Block-level tags become newlines
  s = s.replace(/<\/(p|div|section|article|h[1-6]|li|tr|blockquote|pre|header|main|table)>/gi, '\n');
  s = s.replace(/<(br|hr)\s*\/?>/gi, '\n');
  // Strip all remaining tags
  s = s.replace(/<[^>]+>/g, ' ');
  // Entities
  s = decodeEntities(s);
  // Collapse whitespace but keep line structure
  s = s
    .split('\n')
    .map((line) => line.replace(/[ \t\u00A0]+/g, ' ').trim())
    .filter((line) => line.length > 0)
    .join('\n');

  return s.trim();
}

export function truncateText(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  // try to cut at a line boundary
  const lastNl = cut.lastIndexOf('\n');
  return (lastNl > limit * 0.6 ? cut.slice(0, lastNl) : cut) + '\n… [truncated]';
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  mdash: '—', ndash: '–', hellip: '…', rsquo: '\u2019', lsquo: '\u2018',
  ldquo: '\u201C', rdquo: '\u201D', copy: '©', reg: '®', trade: '™',
  deg: '°', plusmn: '±', times: '×', divide: '÷', euro: '€', pound: '£', yen: '¥', cent: '¢',
};

export function decodeEntities(s: string): string {
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (match, body: string) => {
    if (body[0] === '#') {
      const isHex = body[1] === 'x' || body[1] === 'X';
      const code = parseInt(body.slice(isHex ? 2 : 1), isHex ? 16 : 10);
      if (!Number.isFinite(code)) return match;
      try {
        return String.fromCodePoint(code);
      } catch {
        return match;
      }
    }
    return NAMED_ENTITIES[body.toLowerCase()] ?? match;
  });
}
