import { extractJsonObject, parseJsonObjectLoose, stripCodeFences, repairJson } from '../src/core/json';

describe('json extraction', () => {
  it('finds a balanced object inside chatter', () => {
    const text = 'Sure! {"a":1,"b":{"c":"}"}} hope that helps';
    expect(extractJsonObject(text)).toBe('{"a":1,"b":{"c":"}"}}');
  });

  it('ignores braces inside strings', () => {
    const text = '{"s":"has { weird } braces","n":2}';
    expect(extractJsonObject(text)).toBe(text);
  });

  it('returns null when unbalanced', () => {
    expect(extractJsonObject('{"a": 1')).toBeNull();
    expect(extractJsonObject('no json at all')).toBeNull();
  });

  it('strips code fences', () => {
    expect(stripCodeFences('```json\n{"a":1}\n```')).toBe('{"a":1}');
    expect(stripCodeFences('```\n{"a":1}\n```')).toBe('{"a":1}');
  });

  it('repairs trailing commas and smart quotes', () => {
    expect(repairJson('{"a":1,}')).toBe('{"a":1}');
    expect(repairJson('{“a”: “b”}')).toBe('{"a": "b"}');
  });

  it('parses loose objects end-to-end', () => {
    expect(parseJsonObjectLoose('blah ```json\n{"tool":"x"}\n```')).toEqual({ tool: 'x' });
    expect(parseJsonObjectLoose('{"a":1,}')).toEqual({ a: 1 });
    expect(parseJsonObjectLoose('total garbage')).toBeNull();
    expect(parseJsonObjectLoose('[1,2,3]')).toBeNull(); // arrays are not objects
  });
});
