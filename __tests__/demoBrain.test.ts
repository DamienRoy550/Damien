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

  it('proactively searches YouTube', () => {
    const r = reply(demoBrain({ messages: user('search youtube for lofi beats') }));
    expect(r.tool).toBe('open_website');
    expect(r.arguments.url).toBe('https://www.youtube.com/results?search_query=lofi%20beats');
  });

  it('proactively googles on request', () => {
    const r = reply(demoBrain({ messages: user('google cat facts') }));
    expect(r.tool).toBe('open_website');
    expect(r.arguments.url).toBe('https://www.google.com/search?q=cat%20facts');
  });

  it('looks things up on wikipedia', () => {
    const r = reply(demoBrain({ messages: user('wikipedia quantum computing') }));
    expect(r.tool).toBe('open_website');
    expect(String(r.arguments.url)).toContain('en.wikipedia.org');
  });

  it('treats "look up X" as a web search', () => {
    const r = reply(demoBrain({ messages: user('look up quantum computing') }));
    expect(r.tool).toBe('open_website');
    expect(String(r.arguments.url)).toContain('google.com/search');
  });

  it('routes "open <app>" to open_app', () => {
    const r = reply(demoBrain({ messages: user('open youtube') }));
    expect(r.tool).toBe('open_app');
    expect(r.arguments.app).toBe('youtube');
  });

  it('routes android packages to open_app', () => {
    const r = reply(demoBrain({ messages: user('launch com.whatsapp') }));
    expect(r.tool).toBe('open_app');
    expect(r.arguments.app).toBe('com.whatsapp');
  });

  it('routes "open <domain>" to open_website', () => {
    const r = reply(demoBrain({ messages: user('open youtube.com') }));
    expect(r.tool).toBe('open_website');
    expect(r.arguments.url).toBe('youtube.com');
  });

  it('routes "visit https://..." to open_website', () => {
    const r = reply(demoBrain({ messages: user('visit https://example.dev/pricing') }));
    expect(r.tool).toBe('open_website');
    expect(r.arguments.url).toBe('example.dev/pricing');
  });
});

describe('demo brain conversation', () => {
  it('greets with time-awareness and the honorific', () => {
    const a = answer(demoBrain({ messages: user('hello') }));
    expect(a.answer).toMatch(/Good (morning|afternoon|evening)/);
    expect(a.answer).toContain('Sir');
  });

  it('confirms presence in style', () => {
    const a = answer(demoBrain({ messages: user('are you there?') }));
    expect(a.answer.toLowerCase()).toContain('always');
  });

  it('introduces itself with the persona', () => {
    const a = answer(demoBrain({ messages: user('who are you?') }));
    expect(a.answer).toContain('Damien');
    expect(a.answer.toLowerCase()).toContain('on-device');
    expect(a.answer).toContain('Sir');
  });

  it('lists capabilities on "help"', () => {
    const a = answer(demoBrain({ messages: user('what can you do?') }));
    expect(a.answer).toContain('Math');
    expect(a.answer).toContain('Diagnostics');
  });

  it('routes "run diagnostics" to system_status', () => {
    const r = reply(demoBrain({ messages: user('run diagnostics') }));
    expect(r.tool).toBe('system_status');
  });

  it('routes "status report" to system_status', () => {
    const r = reply(demoBrain({ messages: user('give me a status report') }));
    expect(r.tool).toBe('system_status');
  });

  it('reports diagnostics observations as a report', () => {
    const a = answer(
      demoBrain({
        messages: [
          { role: 'user', content: 'run diagnostics' },
          { role: 'user', content: '[OBSERVATION] DAMIEN OS v0.1.0 — all systems operational\nTools armed: 15' },
        ],
      }),
    );
    expect(a.answer).toContain('Diagnostic complete');
    expect(a.answer).toContain('Tools armed: 15');
  });

  it('says thanks graciously', () => {
    const a = answer(demoBrain({ messages: user('thanks!') }));
    expect(a.answer.toLowerCase()).toContain('pleasure');
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
          { role: 'assistant', content: 'Good evening, Sir.' },
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

  it('apologizes with charm on tool errors', () => {
    const second = answer(
      demoBrain({
        messages: [
          { role: 'user', content: 'Fetch https://down.example' },
          { role: 'user', content: '[OBSERVATION - ERROR] Fetch failed: DNS' },
        ],
      }),
    );
    expect(second.answer).toContain('complication');
  });
});
