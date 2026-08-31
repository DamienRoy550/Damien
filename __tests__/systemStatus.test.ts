import { systemStatus } from '../src/tools/builtin/systemStatus';
import type { ToolContext, SystemSnapshot } from '../src/tools/types';

function ctxWith(snapshot: Partial<SystemSnapshot>, fail = false): ToolContext {
  return {
    now: () => new Date('2026-03-01T12:00:00Z'),
    storage: {
      async get() { return null; },
      async set() {},
      async delete() {},
      async keysWithPrefix() { return []; },
    },
    fetchFn: (async () => { throw new Error('offline'); }) as unknown as typeof fetch,
    systemInfo: fail
      ? async () => { throw new Error('sensor offline'); }
      : async () => ({
          platform: 'web (demo)',
          engine: 'demo brain (simulated)',
          engineLoaded: true,
          toolCount: 15,
          noteCount: 3,
          ...snapshot,
        }),
  };
}

describe('system_status tool', () => {
  it('produces a full diagnostic report', async () => {
    const res = await systemStatus.execute({}, ctxWith({ model: 'Qwen 2.5 Instruct 1.5B' }));
    expect(res.ok).toBe(true);
    expect(res.output).toContain('DAMIEN OS v0.1.0');
    expect(res.output).toContain('all systems operational');
    expect(res.output).toContain('Qwen 2.5 Instruct 1.5B');
    expect(res.output).toContain('Tools armed: 15');
    expect(res.output).toContain('Memories on file: 3');
    expect(res.output).toMatch(/Uptime this session: \d+/);
  });

  it('reports standby when the engine is not loaded', async () => {
    const res = await systemStatus.execute({}, ctxWith({ engineLoaded: false }));
    expect(res.output).toContain('standing by');
  });

  it('fails gracefully when diagnostics are unavailable', async () => {
    const res = await systemStatus.execute({}, ctxWith({}, true));
    expect(res.ok).toBe(false);
    expect(res.output).toContain('sensor offline');
  });

  it('errs honestly when no provider exists', async () => {
    const bare = { ...ctxWith({}) } as Partial<ToolContext>;
    delete bare.systemInfo;
    const res = await systemStatus.execute({}, bare as ToolContext);
    expect(res.ok).toBe(false);
    expect(res.output).toContain('not available');
  });
});
