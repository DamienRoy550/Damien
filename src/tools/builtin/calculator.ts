import type { Tool } from '../types';
import { ok, err } from '../types';
import { evaluateExpression, formatNumber } from '../calc/evaluator';

/** Evaluate an arithmetic expression safely (no eval — shunting-yard parser). */
export const calculator: Tool = {
  name: 'calculator',
  description:
    'Evaluate a math expression exactly. Supports + - * / % ^, parentheses, and functions sqrt, abs, sin, cos, tan, ln, log, log2, exp, floor, ceil, round, pow(a,b), min(a,b), max(a,b). Constants: pi, e.',
  parameters: [
    {
      name: 'expression',
      type: 'string',
      description: 'The math expression, e.g. "(18.5 * 12) / 3 + sqrt(144)"',
      required: true,
    },
  ],
  runsOffline: true,
  async execute(args) {
    const expr = String(args.expression ?? '').trim();
    if (!expr) return err('expression is empty');
    try {
      const value = evaluateExpression(expr);
      return ok(`= ${formatNumber(value)}`);
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  },
};
