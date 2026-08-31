import type { KeyValueStore, JsonRecord } from '../src/tools/types';
import { coerceArguments } from '../src/tools/types';
import { calculator } from '../src/tools/builtin/calculator';
import { unitConvert } from '../src/tools/builtin/unitConvert';
import { dateTime } from '../src/tools/builtin/dateTime';
import { createNoteTools } from '../src/tools/builtin/notes';
import { textStats } from '../src/tools/builtin/textStats';
import { createReminderTool } from '../src/tools/builtin/reminder';
import { stripHtml, truncateText } from '../src/tools/builtin/html';

class FakeStore implements KeyValueStore {
  private map = new Map<string, string>();
  async get(key: string) {
    return this.map.get(key) ?? null;
  }
  async set(key: string, value: string) {
    this.map.set(key, value);
  }
  async delete(key: string) {
    this.map.delete(key);
  }
  async keysWithPrefix(prefix: string) {
    return Array.from(this.map.keys()).filter((k) => k.startsWith(prefix));
  }
  get size() {
    return this.map.size;
  }
}

const ctx = () => ({
  now: () => new Date('2026-03-01T12:00:00Z'),
  storage: new FakeStore(),
  fetchFn: (async () => {
    throw new Error('network disabled in tests');
  }) as typeof fetch,
});

describe('calculator tool', () => {
  it('computes and formats', async () => {
    const res = await calculator.execute({ expression: '(18.5 * 12) / 3' }, ctx());
    expect(res.ok).toBe(true);
    expect(res.output).toBe('= 74');
  });

  it('surfaces errors as observations, not throws', async () => {
    const res = await calculator.execute({ expression: '1/0' }, ctx());
    expect(res.ok).toBe(false);
    expect(res.output).toContain('Division by zero');
  });
});

describe('unit_convert tool', () => {
  it('converts length', async () => {
    const res = await unitConvert.execute({ value: 42, from_unit: 'km', to_unit: 'mi' }, ctx());
    expect(res.ok).toBe(true);
    expect(res.output).toMatch(/26\.09/);
  });

  it('converts temperature affinely', async () => {
    const res = await unitConvert.execute({ value: 100, from_unit: 'c', to_unit: 'f' }, ctx());
    expect(res.output).toContain('212');
  });

  it('converts data units', async () => {
    const res = await unitConvert.execute({ value: 1, from_unit: 'gib', to_unit: 'mb' }, ctx());
    expect(res.output).toMatch(/1073\.7/);
  });

  it('rejects cross-category conversions', async () => {
    const res = await unitConvert.execute({ value: 5, from_unit: 'kg', to_unit: 'm' }, ctx());
    expect(res.ok).toBe(false);
  });
});

describe('datetime tool', () => {
  it('returns now', async () => {
    const res = await dateTime.execute({ operation: 'now' }, ctx());
    expect(res.output).toContain('2026');
    expect(res.output).toContain('Unix seconds: 1772366400');
  });

  it('adds durations', async () => {
    const res = await dateTime.execute({ operation: 'add', days: 3, hours: 2 }, ctx());
    expect(res.output).toContain('2026-03-04T14:00');
  });

  it('diffs two dates', async () => {
    const res = await dateTime.execute(
      { operation: 'diff', date: '2026-03-01', date2: '2026-03-11' },
      ctx(),
    );
    expect(res.output).toContain('10 days');
  });
});

describe('notes tools', () => {
  it('save → list → search → delete round-trips', async () => {
    const store = ctx();
    const tools = createNoteTools();
    const save = tools[0]!;
    const list = tools[1]!;
    const search = tools[2]!;
    const del = tools[3]!;

    const saved = await save.execute(
      { title: 'Groceries', body: 'oat milk, rye bread, apples' },
      store,
    );
    expect(saved.ok).toBe(true);

    const listed = await list.execute({}, store);
    expect(listed.output).toContain('Groceries');

    const found = await search.execute({ query: 'OAT MILK' }, store);
    expect(found.output).toContain('Groceries');

    const miss = await search.execute({ query: 'chocolate' }, store);
    expect(miss.output).toContain('No notes match');

    const idMatch = /\[(n[a-z0-9]+)\]/.exec(listed.output);
    expect(idMatch).not.toBeNull();
    const removed = await del.execute({ id: idMatch![1] }, store);
    expect(removed.ok).toBe(true);
    expect((await list.execute({}, store)).output).toContain('No notes saved yet');
  });
});

describe('text_stats tool', () => {
  it('counts words and keywords', async () => {
    const res = await textStats.execute(
      { text: 'Damien runs fully offline. Damien is private. Offline is the point.' },
      ctx(),
    );
    expect(res.output).toContain('Words: 11');
    expect(res.output).toContain('damien(2)');
    expect(res.output).toContain('offline(2)');
  });
});

describe('reminder tool', () => {
  it('schedules relative reminders', async () => {
    const scheduled: Array<{ fireAt: Date; body: string }> = [];
    const toolCtx = {
      ...ctx(),
      scheduler: {
        requestPermissions: async () => true,
        schedule: async (input: { fireAt: Date; body: string }) => {
          scheduled.push(input);
          return 'notif-1';
        },
      },
    };
    const tool = createReminderTool();
    const res = await tool.execute(
      { message: 'stretch', when: '+45m' },
      toolCtx as never,
    );
    expect(res.ok).toBe(true);
    expect(scheduled).toHaveLength(1);
    const mins = (scheduled[0]!.fireAt.getTime() - toolCtx.now().getTime()) / 60000;
    expect(mins).toBeCloseTo(45, 0);
  });

  it('rejects past times', async () => {
    const tool = createReminderTool();
    const res = await tool.execute(
      { message: 'x', when: '2001-01-01T00:00:00Z' },
      {
        ...ctx(),
        scheduler: { requestPermissions: async () => true, schedule: async () => 'x' },
      } as never,
    );
    expect(res.ok).toBe(false);
    expect(res.output).toContain('past');
  });
});

describe('html utils', () => {
  it('strips tags, scripts and entities', () => {
    const html = '<html><head><style>.x{}</style><script>evil()</script></head><body><h1>Title</h1><p>Hello&nbsp;&amp; <b>world</b></p><!-- c --></body></html>';
    expect(stripHtml(html)).toBe('Title\nHello & world');
  });

  it('truncates at line boundaries', () => {
    const t = truncateText('line1\nline2\nline3', 11);
    expect(t).toContain('…');
    expect(t.startsWith('line1\nline2')).toBe(true);
  });
});

describe('argument coercion', () => {
  const tool = {
    ...calculator,
    parameters: [
      { name: 'expression', type: 'string' as const, description: '', required: true },
      { name: 'precision', type: 'number' as const, description: '' },
    ],
  };

  it('coerces numeric strings and fills missing optionals', () => {
    const res = coerceArguments(tool, { expression: '2+2', precision: '4' } as JsonRecord);
    expect(res).toEqual({ ok: true, args: { expression: '2+2', precision: 4 } });
  });

  it('rejects missing required args', () => {
    const res = coerceArguments(tool, {} as JsonRecord);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toContain('expression');
  });

  it('rejects invalid enums', () => {
    const enumTool = {
      name: 't',
      description: '',
      parameters: [{ name: 'op', type: 'string' as const, description: '', required: true, enum: ['a', 'b'] }],
      runsOffline: true,
      execute: async () => ({ ok: true, output: '' }),
    };
    const res = coerceArguments(enumTool, { op: 'z' } as JsonRecord);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toContain('Allowed');
  });
});
