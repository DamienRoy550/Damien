import { demoBrain } from '../src/llm/demo/brain';
import { parseModelReply } from '../src/agent/parser';
import type { ChatMessage } from '../src/llm/types';

function reply(text: string) {
  const parsed = parseModelReply(text);
  expect(parsed.kind).toBe('tool');
  return parsed as { kind: 'tool'; tool: string; arguments: Record<string, unknown>; thought?: string };
}

function answer(text: string) {
  const parsed = parseModelReply(text);
  expect(parsed.kind).toBe('answer');
  return parsed as { kind: 'answer'; answer: string };
}

const user = (content: string): ChatMessage[] => [{ role: 'user', content }];

describe('demo brain speaks the wire protocol', () => {
  it('routes math to the calculator', () => {
    const r = reply(demoBrain({ messages: user('What is 234 * 17?') }));
    expect(r.tool).toBe('calculator');
    expect(r.arguments.expression).toBe('234*17');
    expect(r.thought).toBeTruthy();
  });

  it('handles word math like "12 times 8"', () => {
    const r = reply(demoBrain({ messages: user('what is 12 times 8') }));
    expect(r.tool).toBe('calculator');
    expect(r.arguments.expression).toBe('12*8');
  });

  it('handles percentages like "15 percent of 80"', () => {
    const r = reply(demoBrain({ messages: user('15 percent of 80') }));
    expect(r.tool).toBe('calculator');
    expect(r.arguments.expression).toBe('(15/100)*80');
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

  it('routes "show my notes" to list_notes', () => {
    const r = reply(demoBrain({ messages: user('show my notes') }));
    expect(r.tool).toBe('list_notes');
  });

  it('routes time questions to the datetime tool', () => {
    const r = reply(demoBrain({ messages: user('what time is it?') }));
    expect(r.tool).toBe('datetime');
    expect(r.arguments.operation).toBe('now');
  });

  it('routes reminder requests', () => {
    const r = reply(demoBrain({ messages: user('Remind me to stretch in 45 minutes') }));
    expect(r.tool).toBe('schedule_reminder');
    expect(r.arguments.message).toBe('stretch');
  });

  it('routes URLs to web_fetch', () => {
    const r = reply(demoBrain({ messages: user('Fetch https://example.com and tell me about it') }));
    expect(r.tool).toBe('web_fetch');
    expect(r.arguments.url).toBe('https://example.com');
  });
});

describe('demo brain conversation', () => {
  it('greets like a person', () => {
    const a = answer(demoBrain({ messages: user('hello') }));
    expect(a.answer.toLowerCase()).toContain('hey');
  });

  it('introduces itself', () => {
    const a = answer(demoBrain({ messages: user('who are you?') }));
    expect(a.answer).toContain('Damien');
    expect(a.answer.toLowerCase()).toContain('offline');
  });

  it('lists capabilities on "help"', () => {
    const a = answer(demoBrain({ messages: user('what can you do?') }));
    expect(a.answer).toContain('Math');
    expect(a.answer).toContain('note');
  });

  it('says thanks gracefully', () => {
    const a = answer(demoBrain({ messages: user('thanks!') }));
    expect(a.answer.toLowerCase()).toContain('anytime');
  });

  it('is honest about open-ended questions', () => {
    const a = answer(demoBrain({ messages: user('what is the meaning of life?') }));
    expect(a.answer).toContain('demo');
  });

  it('uses the newest user message as the task when history is present', () => {
    const r = reply(
      demoBrain({
        messages: [
          { role: 'user', content: 'hello' },
          { role: 'assistant', content: 'Hi! What can I do?' },
          { role: 'user', content: 'What is 6*7?' },
        ],
      }),
    );
    expect(r.tool).toBe('calculator');
    expect(r.arguments.expression).toBe('6*7');
  });

  it('retries the previous request on a "yes" follow-up', () => {
    const r = reply(
      demoBrain({
        messages: [
          { role: 'user', content: 'Convert 10 km to mi' },
          { role: 'assistant', content: 'About 6.2 miles.' },
          { role: 'user', content: 'yes' },
        ],
      }),
    );
    expect(r.tool).toBe('unit_convert');
    expect(r.arguments.value).toBe(10);
  });

  it('answers from observations on the second turn', () => {
    const first = reply(demoBrain({ messages: user('What is 12*12?') }));
    expect(first.tool).toBe('calculator');
    const second = answer(
      demoBrain({
        messages: [
          { role: 'user', content: 'What is 12*12?' },
          { role: 'assistant', content: first.tool },
          { role: 'user', content: '[OBSERVATION] = 144' },
        ],
      }),
    );
    expect(second.answer).toContain('144');
  });

  it('apologizes honestly on tool errors', () => {
    const second = answer(
      demoBrain({
        messages: [
          { role: 'user', content: 'Fetch https://down.example' },
          { role: 'user', content: '[OBSERVATION - ERROR] Fetch failed: DNS' },
        ],
      }),
    );
    expect(second.answer).toContain('problem');
  });
});
