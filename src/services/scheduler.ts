import type { ReminderScheduler } from '../tools/types';

/**
 * Base resolution (web bundle + tooling): no local notification scheduling
 * in the demo build. Native builds override via scheduler.native.ts.
 */
export async function getScheduler(): Promise<ReminderScheduler | undefined> {
  return undefined;
}
