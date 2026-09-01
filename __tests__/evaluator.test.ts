import { evaluateExpression, formatNumber } from '../src/tools/calc/evaluator';

describe('safe expression evaluator', () => {
  it('handles basic arithmetic and precedence', () => {
    expect(evaluateExpression('2 + 3 * 4')).toBe(14);
    expect(evaluateExpression('(2 + 3) * 4')).toBe(20);
    expect(evaluateExpression('10 / 4')).toBe(2.5);
    expect(evaluateExpression('10 % 3')).toBe(1);
    expect(evaluateExpression('2 ^ 10')).toBe(1024);
  });

  it('handles unary minus correctly', () => {
    expect(evaluateExpression('-2 * 3')).toBe(-6);
    expect(evaluateExpression('2 * -3')).toBe(-6);
    expect(evaluateExpression('-(2 + 3)')).toBe(-5);
    expect(evaluateExpression('-3^2')).toBe(-9); // 0 - 9
    expect(evaluateExpression('2^-3')).toBe(0.125);
    expect(evaluateExpression('--5')).toBe(5);
    expect(evaluateExpression('5 - -3')).toBe(8);
  });

  it('supports functions and constants', () => {
    expect(evaluateExpression('sqrt(144)')).toBe(12);
    expect(evaluateExpression('pow(2, 8)')).toBe(256);
    expect(evaluateExpression('min(3, -2)')).toBe(-2);
    expect(evaluateExpression('max(3, -2)')).toBe(3);
    expect(evaluateExpression('round(3.6)')).toBe(4);
    expect(evaluateExpression('floor(3.9) + ceil(0.1)')).toBe(4);
    expect(evaluateExpression('ln(e)')).toBeCloseTo(1);
    expect(evaluateExpression('log(100)')).toBeCloseTo(2);
    expect(Number(evaluateExpression('sin(pi)'))).toBeCloseTo(0, 10);
  });

  it('tolerates model-style separators', () => {
    expect(evaluateExpression('3 × 4')).toBe(12);
    expect(evaluateExpression('10 ÷ 2')).toBe(5);
    expect(evaluateExpression('234*17')).toBe(3978);
  });

  it('rejects dangerous or malformed input with clear errors', () => {
    expect(() => evaluateExpression('process.exit(1)')).toThrow(/Unknown name/);
    expect(() => evaluateExpression('1/0')).toThrow(/Division by zero/);
    expect(() => evaluateExpression('(2+3')).toThrow(/Unbalanced/);
    expect(() => evaluateExpression('2+')).toThrow();
    expect(() => evaluateExpression('sqrt(-4)')).toThrow(/negative/);
    expect(() => evaluateExpression('foo(1)')).toThrow(/Unknown name/);
  });

  it('formats numbers for observations', () => {
    expect(formatNumber(42)).toBe('42');
    expect(formatNumber(2.5)).toBe('2.5');
    expect(formatNumber(0.1 + 0.2)).toBe('0.3');
  });
});
