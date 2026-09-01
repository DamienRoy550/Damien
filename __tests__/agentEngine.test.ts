import { AgentEngine } from '../src/agent/AgentEngine';
import { ToolRegistry } from '../src/tools/registry';
import { MockEngine } from '../src/llm/demo/MockEngine';
import type { Brain } from '../src/llm/demo/brain';
import type { KeyValueStore, ToolContext } from '../src/tools/types';
import type { AgentEvent, RunResult } from '../src/agent/types';

class FakeStore implements KeyValueStore {
  private map = new Map<string, string>();
  async get(key: string) { return this.map.get(key) ?? null; }
  async set(key: string, value: string) { this.map.set(key, value); }
  async delete(key: string) { this.map.delete(key); }
  async keysWithPrefix(prefix: string) {
    return Array.from(this.map.keys()).filter((k) => k.startsWith(prefix));
  }
  has(key: string) { return this.map.has(key); }
}

function makeCtx(): ToolContext & { store: FakeStore } {
  const store = new FakeStore();
  return {
    store,
    now: () => new Date('2026-03-01T12:00:00Z'),
    storage: store,
    fetchFn: (async () => { throw new Error('offline'); }) as unknown as typeof fetch,
  };
}

function collect(events: AgentEvent[]) {
  return (e: AgentEvent) => events.push(e);
}

describe('AgentEngine full loop', () => {
  it('runs a multi-step task: calculate → save note → answer', async () => {
    const engine = new MockEngine([
      '{"thought":"Need exact math.","tool":"calculator","arguments":{"expression":"6*7"}}',
      '{"thought":"Storing the result.","tool":"save_note","arguments":{"title":"Times table","body":"6 times 7 is 42"}}',
      '{"thought":"Both done.","answer":"6 × 7 = 42, and I saved it as a note titled \\"Times table\\"."}',
    ]);
    const ctx = makeCtx();
    const registry = new ToolRegistry();
    const { calculator } = await import('../src/tools/builtin/calculator');
    const notes = await import('../src/tools/builtin/notes');
    for (const t of notes.createNoteTools()) registry.register(t);
    registry.register(calculator);

    const events: AgentEvent[] = [];
    const agent = new AgentEngine(engine, registry, ctx);
    const result = await agent.run('What is 6*7? Save it as a note.', {
      onEvent: collect(events),
    });

    expect(result.status).toBe('ok');
    expect(result.answer).toContain('42');
    expect(result.steps).toHaveLength(2);
    expect(result.steps[0]).toMatchObject({ tool: 'calculator', observation: '= 42' });
    expect(result.steps[1]).toMatchObject({ tool: 'save_note' });
    // note actually persisted
    const keys = await ctx.storage.keysWithPrefix('note:');
    expect(keys).toHaveLength(1);

    const kinds = events.map((e) => e.type);
    expect(kinds).toContain('run_started');
    expect(kinds).toContain('tool_started');
    expect(kinds).toContain('tool_finished');
    expect(kinds).toContain('finished');
  });

  it('recovers from an unknown tool call', async () => {
    const engine = new MockEngine([
      '{"thought":"Oops.","tool":"teleport_user","arguments":{}}',
      '{"thought":"Right, no such tool.","answer":"I could not do that — no tool for it."}',
    ]);
    const agent = new AgentEngine(engine, new ToolRegistry(), makeCtx());
    const result = await agent.run('teleport me');
    expect(result.status).toBe('ok');
    expect(result.steps[0]!.error).toContain('Unknown tool');
    expect(result.answer).toContain('no tool');
  });

  it('reports argument validation failures as observations', async () => {
    const engine = new MockEngine([
      '{"tool":"calculator","arguments":{}}',
      '{"thought":"Fixing.","tool":"calculator","arguments":{"expression":"2+2"}}',
      '{"thought":"Done.","answer":"4"}',
    ]);
    const { calculator } = await import('../src/tools/builtin/calculator');
    const registry = new ToolRegistry();
    registry.register(calculator);
    const agent = new AgentEngine(engine, registry, makeCtx());
    const result = await agent.run('add 2+2');
    expect(result.steps[0]!.error).toContain('Missing required argument');
    expect(result.steps[1]!.observation).toBe('= 4');
  });

  it('stops at max steps and summarizes instead of looping forever', async () => {
    const alwaysTool: Brain = () =>
      '{"thought":"Still working...","tool":"calculator","arguments":{"expression":"1+1"}}';
    const { calculator } = await import('../src/tools/builtin/calculator');
    const registry = new ToolRegistry();
    registry.register(calculator);
    const agent = new AgentEngine(new MockEngine(alwaysTool), registry, makeCtx());
    const result = await agent.run('loop forever', { maxSteps: 3 });
    expect(result.status).toBe('max_steps');
    expect(result.steps).toHaveLength(3);
    expect(result.answer.length).toBeGreaterThan(0);
  });

  it('cancels cleanly when the signal aborts', async () => {
    const controller = new AbortController();
    const engine = new MockEngine([
      '{"thought":"Working.","tool":"calculator","arguments":{"expression":"123456*654321"}}',
      '{"thought":"answer","answer":"done"}',
    ]);
    const { calculator } = await import('../src/tools/builtin/calculator');
    const registry = new ToolRegistry();
    registry.register(calculator);
    const agent = new AgentEngine(engine, registry, makeCtx());

    const result: RunResult = await agent.run('long task', {
      signal: controller.signal,
      onEvent: (e) => {
        if (e.type === 'token') controller.abort();
      },
    });
    expect(result.status).toBe('cancelled');
    expect(result.answer).toBe('');
  });

  it('streams tokens through events', async () => {
    const engine = new MockEngine(['{"thought":"t","answer":"final words here"}']);
    const agent = new AgentEngine(engine, new ToolRegistry(), makeCtx());
    const tokens: string[] = [];
    await agent.run('say hi', {
      onEvent: (e) => {
        if (e.type === 'token') tokens.push(e.delta);
      },
    });
    expect(tokens.length).toBeGreaterThan(1);
    expect(tokens.join('')).toContain('final');
  });

  it('includes the tool docs in the system prompt it builds', async () => {
    const { calculator } = await import('../src/tools/builtin/calculator');
    const registry = new ToolRegistry();
    registry.register(calculator);
    const engine = new MockEngine(['{"thought":"t","answer":"ok"}']);
    let seenSystem = '';
    const spy: Brain = ({ messages }) => {
      seenSystem = messages[0]!.content;
      return '{"thought":"t","answer":"ok"}';
    };
    const spyEngine = new MockEngine(spy);
    const agent = new AgentEngine(spyEngine, registry, makeCtx());
    await agent.run('x');
    expect(seenSystem).toContain('calculator');
    expect(seenSystem).toContain('OUTPUT FORMAT');
  });
});
