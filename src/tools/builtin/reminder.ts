import type { Tool } from '../types';
import { ok, err } from '../types';

/**
 * Schedule a local notification reminder. The platform scheduler is
 * injected via ctx.scheduler (expo-notifications on device) so this tool
 * stays pure and testable.
 */
export function createReminderTool(): Tool {
  return {
    name: 'schedule_reminder',
    description:
      'Schedule a phone notification (reminder/alarm). Only for future times — not for taking notes.',
    parameters: [
      { name: 'message', type: 'string', description: 'What the notification should say', required: true },
      {
        name: 'when',
        type: 'string',
        description:
          'When to fire: ISO datetime ("2026-03-01T09:00:00") or relative like "+30m", "+2h", "+1d", "+90s"',
        required: true,
      },
      { name: 'title', type: 'string', description: 'Notification title (default "Damien reminder")' },
    ],
    runsOffline: true,
    async execute(args, ctx) {
      const scheduler = ctx.scheduler;
      if (!scheduler) return err('Reminders are not available on this device.');

      const message = String(args.message ?? '').trim();
      if (!message) return err('message is required');
      const whenRaw = String(args.when ?? '').trim();
      if (!whenRaw) return err('when is required');

      const now = ctx.now();
      let fireAt: Date;
      const rel = /^\+(\d+)\s*(s|m|h|d|w)$/i.exec(whenRaw);
      if (rel) {
        const amount = Number(rel[1]);
        const unit = (rel[2] as string).toLowerCase();
        const multipliers: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
        fireAt = new Date(now.getTime() + amount * (multipliers[unit] as number));
      } else {
        fireAt = new Date(whenRaw);
        if (Number.isNaN(fireAt.getTime())) {
          return err(`Could not understand time "${whenRaw}". Use ISO datetime or "+30m" style.`);
        }
      }

      if (fireAt.getTime() <= now.getTime()) {
        return err('That time is in the past. Give a future time.');
      }

      const granted = await scheduler.requestPermissions();
      if (!granted) return err('Notification permission was denied by the user.');

      const title = String(args.title ?? 'Damien reminder');
      const id = `rem${Date.now().toString(36)}`;
      await scheduler.schedule({ id, title, body: message, fireAt });

      const diffMin = Math.round((fireAt.getTime() - now.getTime()) / 60000);
      return ok(
        `Reminder scheduled (id ${id}) to fire in ~${diffMin} minute(s) at ${fireAt.toISOString()}: "${message}". Confirm details to the user.`,
      );
    },
  };
}
