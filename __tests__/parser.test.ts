import { parseModelReply } from '../src/agent/parser';

describe('wire protocol parser', () => {
  it('parses a clean tool call', () => {
    const reply = parseModelReply(
      '{"thought":"I will calculate.","tool":"calculator","arguments":{"expression":"2+2"}}',
    );
    expect(reply).toMatchObject({
      kind: 'tool',
      thought: 'I will calculate.',
      tool: 'calculator',
      arguments: { expression: '2+2' },
    });
  });

  it('parses an answer', () => {
    const reply = parseModelReply('{"thought":"Done.","answer":"It is 4."}');
    expect(reply).toMatchObject({ kind: 'answer', thought: 'Done.', answer: 'It is 4.' });
  });

  it('survives code fences and surrounding chatter', () => {
    const reply = parseModelReply('Here you go:\n```json\n{"tool":"calculator","arguments":{}}\n```');
    expect(reply.kind).toBe('tool');
    expect((reply as { tool: string }).tool).toBe('calculator');
  });

  it('repairs trailing commas in arguments', () => {
    const reply = parseModelReply('{"tool":"save_note","arguments":{"title":"x","body":"y",}}');
    expect(reply.kind).toBe('tool');
    expect((reply as { arguments: object }).arguments).toEqual({ title: 'x', body: 'y' });
  });

  it('treats malformed JSON as a plain answer', () => {
    const reply = parseModelReply('The answer is 42, no tools needed.');
    expect(reply).toMatchObject({ kind: 'answer', answer: 'The answer is 42, no tools needed.' });
  });

  it('handles a thought-only object', () => {
    const reply = parseModelReply('{"thought":"I know this one."}');
    expect(reply.kind).toBe('answer');
  });

  it('handles non-object arguments gracefully', () => {
    const reply = parseModelReply('{"tool":"calculator","arguments":"2+2"}');
    expect(reply.kind).toBe('tool');
    expect((reply as { arguments: object }).arguments).toEqual({});
  });
});
