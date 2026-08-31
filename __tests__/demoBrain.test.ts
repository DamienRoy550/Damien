import { demoBrain } from '../src/llm/demo/brain';
import { parseModelReply } from '../src/agent/parser';
import type { ChatMessage } from '../src/llm/types';

function reply(text: string) {
  const parsed = parseModelReply(text);
  expect(parsed.kind).toBe('tool');
  return parsed as { kind: 'tool'; tool: string; arguments: Record<string, unknown>; thought?: string };
}

const user = (content: string): ChatMessage[] => [{ role: 'user', content }];

describe('demo brain speaks the wire protocol', () => {
  it('routes math to the calculator', () => {
    const r = reply(demoBrain({ messages: user('What is 234 * 17?') }));
    expect(r.tool).toBe('calculator');
    expect(r.arguments.expression).toBe('234*17');
    expect(r.thought).toBeTruthy();
  });

  it('routes conversions to unit_convert', () => {
    const r = reply(demoBrain({ messages: user('Convert 42 km to miles') }));
    expect(r.tool).toBe('unit_convert');
    expect(r.arguments).toEqual({ value: 42, from_unit: 'km', to_unit: 'miles' });
  });

  it('routes note-taking to save_note', () => {
    const r = reply(demoBrain({ messages: user('Take a note: present at 3pm') }));
    expect(r.tool).toBe('save_note');
    expect(String(r.arguments.body)).toContain('present at 3pm');
  });

  it('routes URLs to web_fetch', () => {
    const r = reply(demoBrain({ messages: user('Fetch https://example.com and tell me about it') }));
    expect(r.tool).toBe('web_fetch');
    expect(r.arguments.url).toBe('https://example.com');
  });

  it('greets without tools', () => {
    const parsed = parseModelReply(demoBrain({ messages: user('hello there') }));
    expect(parsed.kind).toBe('answer');
  });

  it('answers from observations on the second turn', () => {
    const first = reply(demoBrain({ messages: user('What is 12*12?') }));
    expect(first.tool).toBe('calculator');
    const second = parseModelReply(
      demoBrain({
        messages: [
          { role: 'user', content: 'What is 12*12?' },
          { role: 'assistant', content: first.tool },
          { role: 'user', content: '[OBSERVATION] = 144' },
        ],
      }),
    );
    expect(second.kind).toBe('answer');
    expect((second as { answer: string }).answer).toContain('144');
  });

  it('apologizes honestly on tool errors', () => {
    const second = parseModelReply(
      demoBrain({
        messages: [
          { role: 'user', content: 'Fetch https://down.example' },
          { role: 'user', content: '[OBSERVATION - ERROR] Fetch failed: DNS' },
        ],
      }),
    );
    expect(second.kind).toBe('answer');
    expect((second as { answer: string }).answer).toContain('problem');
  });
});
