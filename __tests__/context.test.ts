import { buildMessages, trimToBudget, serializeStepForModel } from '../src/agent/context';
import type { StepRecord } from '../src/agent/types';
import { estimateMessagesTokens } from '../src/core/tokenizer';

function makeSteps(n: number): StepRecord[] {
  return Array.from({ length: n }, (_, i) => ({
    index: i + 1,
    thought: `step ${i + 1} thought`,
    tool: 'calculator',
    toolArguments: { expression: '1+1' },
    observation: `OBSERVATION TEXT NUMBER ${i + 1} ${'x'.repeat(100)}`,
  }));
}

describe('context builder', () => {
  it('produces system + task + assistant/observation pairs', () => {
    const msgs = buildMessages({ systemPrompt: 'SYS', task: 'TASK', steps: makeSteps(2) }, 10_000);
    expect(msgs).toHaveLength(2 + 2 * 2);
    expect(msgs[0]).toMatchObject({ role: 'system', content: 'SYS' });
    expect(msgs[1]).toMatchObject({ role: 'user', content: 'TASK' });
    expect(msgs[2]!.role).toBe('assistant');
    expect(msgs[3]!.content).toContain('[OBSERVATION]');
  });

  it('serializes steps back into wire JSON', () => {
    const s = serializeStepForModel(makeSteps(1)[0]!);
    expect(JSON.parse(s)).toEqual({
      thought: 'step 1 thought',
      tool: 'calculator',
      arguments: { expression: '1+1' },
    });
  });

  it('inserts conversation history between the task and the steps', () => {
    const history = [
      { role: 'user' as const, content: 'hello' },
      { role: 'assistant' as const, content: 'Hi!' },
    ];
    const msgs = buildMessages(
      { systemPrompt: 'SYS', task: 'TASK', steps: makeSteps(1), history },
      10_000,
    );
    expect(msgs.map((m) => m.role)).toEqual([
      'system',
      'user',
      'user',
      'assistant',
      'assistant',
      'user',
    ]);
    expect(msgs[2]).toEqual({ role: 'user', content: 'hello' });
  });
});

describe('context trimming', () => {
  it('returns messages untouched when under budget', () => {
    const msgs = buildMessages({ systemPrompt: 'SYS', task: 'TASK', steps: makeSteps(3) }, 100_000);
    expect(msgs).toHaveLength(8);
  });

  it('omits old observations before dropping steps', () => {
    const steps = makeSteps(4);
    const msgs = buildMessages({ systemPrompt: 'SYS', task: 'TASK', steps }, 120);
    expect(msgs[0]!.content).toBe('SYS');
    expect(msgs[1]!.content).toBe('TASK');
    // newest observation must survive verbatim
    const observations = msgs.filter((m) => m.content.startsWith('[OBSERVATION'));
    const last = observations[observations.length - 1]!;
    expect(last.content).toContain('OBSERVATION TEXT NUMBER 4');
    // older ones must be stubs
    if (observations.length > 1) {
      expect(observations[0]!.content).toContain('[omitted');
    }
    // and the whole thing should fit the budget
    expect(estimateMessagesTokens(msgs)).toBeLessThanOrEqual(120 + estimateMessagesTokens(msgs.slice(0, 2)));
  });

  it('never drops head + final pair even under extreme pressure', () => {
    const steps = makeSteps(10);
    const msgs = buildMessages({ systemPrompt: 'SYS', task: 'TASK', steps }, 20);
    expect(msgs.length).toBeGreaterThanOrEqual(4);
    expect(msgs[0]!.content).toBe('SYS');
    expect(msgs[1]!.content).toBe('TASK');
    const lastPair = msgs.slice(-2);
    expect(lastPair[0]!.role).toBe('assistant');
    expect(lastPair[1]!.content).toContain('[OBSERVATION');
  });

  it('trimToBudget is stable on already-small inputs', () => {
    const msgs = [
      { role: 'system' as const, content: 'a' },
      { role: 'user' as const, content: 'b' },
    ];
    expect(trimToBudget(msgs, 1000)).toEqual(msgs);
  });
});
