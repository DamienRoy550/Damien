import type { Tool, SystemSnapshot } from '../types';
import { ok, err } from '../types';

const BOOT_TIME = Date.now();

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${seconds % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * JARVIS-style self-diagnostic: reports engine, tool complement, memory
 * usage and uptime. The snapshot provider is injected via ctx.systemInfo
 * (see runtime.ts) so this tool stays platform-agnostic.
 */
export const systemStatus: Tool = {
  name: 'system_status',
  description:
    'Run a self-diagnostic and report Damien\'s current status: engine, model, tools, memories, uptime. Use when the user asks for a status, diagnostics, or what is available.',
  parameters: [],
  runsOffline: true,
  async execute(_args, ctx) {
    if (!ctx.systemInfo) return err('Diagnostics are not available in this environment.');
    try {
      const s: SystemSnapshot = await ctx.systemInfo();
      const lines = [
        `DAMIEN OS v0.1.0 — all systems ${s.engineLoaded ? 'operational' : 'standing by'}`,
        `Engine: ${s.engine}${s.model ? ` (${s.model})` : ''}`,
        `Platform: ${s.platform}`,
        `Tools armed: ${s.toolCount}`,
        `Memories on file: ${s.noteCount} note(s)`,
        `Reminders scheduled: yes` /* placeholder replaced below */,
        `Uptime this session: ${formatUptime(Math.floor((Date.now() - BOOT_TIME) / 1000))}`,
      ];
      if (typeof s.reminderCount === 'number') {
        lines[5] = `Reminders scheduled: ${s.reminderCount}`;
      }
      return ok(lines.join('\n'));
    } catch (e) {
      return err(`Diagnostic failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  },
};
