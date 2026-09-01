import type { Tool } from '../types';
import { ok, err } from '../types';

function fmt(d: Date, timeZone?: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      ...(timeZone ? { timeZone } : {}),
    }).format(d);
  } catch {
    return d.toISOString();
  }
}

/**
 * Date & time toolbox: current time, date arithmetic, and differences.
 * One tool with an operation parameter — keeps the tool list small for
 * tiny-context models.
 */
export const dateTime: Tool = {
  name: 'datetime',
  description:
    'Get the current date/time, add/subtract a duration from a date, or find the difference between two dates. Operations: "now", "add", "diff".',
  parameters: [
    {
      name: 'operation',
      type: 'string',
      description: '"now" | "add" | "diff"',
      required: true,
      enum: ['now', 'add', 'diff'],
    },
    { name: 'date', type: 'string', description: 'ISO date/datetime for add/diff, e.g. "2026-03-01" or "2026-03-01T09:30:00"' },
    { name: 'date2', type: 'string', description: 'Second ISO date for "diff"' },
    { name: 'days', type: 'number', description: 'Days to add (can be negative) for "add"' },
    { name: 'hours', type: 'number', description: 'Hours to add for "add"' },
    { name: 'minutes', type: 'number', description: 'Minutes to add for "add"' },
  ],
  runsOffline: true,
  async execute(args, ctx) {
    const op = String(args.operation ?? 'now');

    if (op === 'now') {
      const now = ctx.now();
      return ok(
        `Current date and time: ${fmt(now, ctx.timeZone)}. ISO: ${now.toISOString()}. Unix seconds: ${Math.floor(now.getTime() / 1000)}.`,
      );
    }

    if (op === 'add') {
      const base = args.date ? new Date(String(args.date)) : ctx.now();
      if (Number.isNaN(base.getTime())) return err(`Could not parse date "${String(args.date)}"`);
      const days = Number(args.days ?? 0);
      const hours = Number(args.hours ?? 0);
      const minutes = Number(args.minutes ?? 0);
      if (![days, hours, minutes].every(Number.isFinite)) {
        return err('days/hours/minutes must be numbers');
      }
      const out = new Date(base.getTime() + (days * 86400 + hours * 3600 + minutes * 60) * 1000);
      return ok(`${fmt(base, ctx.timeZone)} + ${days}d ${hours}h ${minutes}m = ${fmt(out, ctx.timeZone)} (ISO: ${out.toISOString()})`);
    }

    if (op === 'diff') {
      const a = new Date(String(args.date ?? ''));
      const b = new Date(String(args.date2 ?? ''));
      if (Number.isNaN(a.getTime())) return err(`Could not parse date "${String(args.date)}"`);
      if (Number.isNaN(b.getTime())) return err(`Could not parse date2 "${String(args.date2)}"`);
      let from = a.getTime();
      let to = b.getTime();
      const sign = to >= from ? 1 : -1;
      if (sign < 0) [from, to] = [to, from];
      const seconds = Math.round((to - from) / 1000);
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      return ok(
        `Difference: ${days} days, ${hours} hours, ${minutes} minutes (${seconds} seconds total)${sign < 0 ? ' (negative direction — date2 is earlier)' : ''}.`,
      );
    }

    return err(`Unknown operation "${op}". Use "now", "add", or "diff".`);
  },
};
