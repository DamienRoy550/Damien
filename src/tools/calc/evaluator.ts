/**
 * Safe arithmetic evaluator — a hand-rolled tokenizer + shunting-yard parser.
 * NO eval(), NO new Function(). Whatever the model throws at us, the worst
 * outcome is an error string.
 *
 * Grammar:
 *   expr   := term (('+'|'-') term)*
 *   term   := unary (('*'|'/'|'%') unary)*
 *   unary  := ('-')? power
 *   power  := atom ('^' unary)?        // right associative
 *   atom   := NUMBER | CONST | FN '(' args ')' | '(' expr ')'
 *
 * Implemented as shunting-yard over tokens with a unary-minus operator
 * ('u-') at the highest precedence, right-associative.
 */

type Token =
  | { t: 'num'; v: number }
  | { t: 'op'; v: string }
  | { t: 'fn'; v: string }
  | { t: 'lparen' }
  | { t: 'rparen' }
  | { t: 'comma' };

type FnDef = { fn: (...xs: number[]) => number; arity: 1 | 2 };

const FUNCTIONS: Record<string, FnDef> = {
  sqrt: { fn: Math.sqrt, arity: 1 },
  abs: { fn: Math.abs, arity: 1 },
  sin: { fn: Math.sin, arity: 1 },
  cos: { fn: Math.cos, arity: 1 },
  tan: { fn: Math.tan, arity: 1 },
  asin: { fn: Math.asin, arity: 1 },
  acos: { fn: Math.acos, arity: 1 },
  atan: { fn: Math.atan, arity: 1 },
  ln: { fn: Math.log, arity: 1 },
  log: { fn: (x) => Math.log10(x), arity: 1 },
  log2: { fn: (x) => Math.log2(x), arity: 1 },
  exp: { fn: Math.exp, arity: 1 },
  floor: { fn: Math.floor, arity: 1 },
  ceil: { fn: Math.ceil, arity: 1 },
  round: { fn: Math.round, arity: 1 },
  pow: { fn: Math.pow, arity: 2 },
  min: { fn: Math.min, arity: 2 },
  max: { fn: Math.max, arity: 2 },
};

const CONSTANTS: Record<string, number> = {
  pi: Math.PI,
  e: Math.E,
};

const PRECEDENCE: Record<string, number> = {
  '+': 1,
  '-': 1,
  '*': 2,
  '/': 2,
  '%': 2,
  '^': 3,
  // Unary minus shares ^'s precedence tier but is right-associative and
  // never pops a right-associative operator off the stack, which yields
  // the mathematical convention: -3^2 = -(3^2) yet 2^-3 = 2^(-3).
  'u-': 3,
};

const RIGHT_ASSOC = new Set(['^', 'u-']);

export function evaluateExpression(input: string): number {
  const tokens = tokenize(input);
  const postfix = toPostfix(tokens);
  return evalPostfix(postfix);
}

function tokenize(src: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const s = src.toLowerCase().replace(/\s+/g, '');

  while (i < s.length) {
    const ch = s[i] as string;

    if (/[0-9.]/.test(ch)) {
      let j = i;
      while (j < s.length && /[0-9.]/.test(s[j] as string)) j++;
      const numStr = s.slice(i, j);
      const v = Number(numStr);
      if (!Number.isFinite(v)) throw new Error(`Invalid number "${numStr}"`);
      tokens.push({ t: 'num', v });
      i = j;
      continue;
    }

    if (ch === '*' || ch === '/' || ch === '%' || ch === '+' || ch === '-' || ch === '^') {
      tokens.push({ t: 'op', v: ch });
      i++;
      continue;
    }

    if (ch === '×' || ch === 'x' || ch === '·') {
      tokens.push({ t: 'op', v: '*' });
      i++;
      continue;
    }
    if (ch === '÷') {
      tokens.push({ t: 'op', v: '/' });
      i++;
      continue;
    }

    if (ch === '(') {
      tokens.push({ t: 'lparen' });
      i++;
      continue;
    }
    if (ch === ')') {
      tokens.push({ t: 'rparen' });
      i++;
      continue;
    }
    if (ch === ',') {
      tokens.push({ t: 'comma' });
      i++;
      continue;
    }

    if (/[a-z]/.test(ch)) {
      let j = i;
      while (j < s.length && /[a-z0-9_]/.test(s[j] as string)) j++;
      const word = s.slice(i, j);
      if (word in FUNCTIONS) {
        tokens.push({ t: 'fn', v: word });
      } else if (word in CONSTANTS) {
        tokens.push({ t: 'num', v: CONSTANTS[word] as number });
      } else {
        throw new Error(`Unknown name "${word}"`);
      }
      i = j;
      continue;
    }

    throw new Error(`Unexpected character "${ch}"`);
  }
  return tokens;
}

function toPostfix(tokens: Token[]): Token[] {
  const output: Token[] = [];
  const stack: Token[] = [];
  let prev: Token | null = null;

  for (const tok of tokens) {
    switch (tok.t) {
      case 'num':
        output.push(tok);
        break;

      case 'fn':
        stack.push(tok);
        break;

      case 'comma':
        while (stack.length && stack[stack.length - 1]!.t !== 'lparen') {
          output.push(stack.pop() as Token);
        }
        break;

      case 'op': {
        const isUnaryMinus =
          tok.v === '-' &&
          (prev === null ||
            prev.t === 'op' ||
            prev.t === 'lparen' ||
            prev.t === 'comma');
        const isUnaryPlus =
          tok.v === '+' &&
          (prev === null ||
            prev.t === 'op' ||
            prev.t === 'lparen' ||
            prev.t === 'comma');

        if (isUnaryPlus) break; // no-op
        if (isUnaryMinus) {
          const uminus: Token = { t: 'op', v: 'u-' };
          // Right-assoc: only pop strictly-higher-precedence LEFT-assoc ops.
          while (stack.length) {
            const top = stack[stack.length - 1] as Token;
            if (
              top.t === 'op' &&
              !RIGHT_ASSOC.has(top.v) &&
              (PRECEDENCE[top.v] as number) > (PRECEDENCE['u-'] as number)
            ) {
              output.push(stack.pop() as Token);
              continue;
            }
            break;
          }
          stack.push(uminus);
          prev = tok;
          continue;
        }

        const p1 = PRECEDENCE[tok.v] as number;
        while (stack.length) {
          const top = stack[stack.length - 1] as Token;
          if (top.t === 'op') {
            const p2 = PRECEDENCE[top.v] as number;
            if (p2 > p1 || (p2 === p1 && !RIGHT_ASSOC.has(tok.v))) {
              output.push(stack.pop() as Token);
              continue;
            }
          } else if (top.t === 'fn') {
            output.push(stack.pop() as Token);
            continue;
          }
          break;
        }
        stack.push(tok);
        break;
      }

      case 'lparen':
        stack.push(tok);
        break;

      case 'rparen': {
        let matched = false;
        while (stack.length) {
          const top = stack.pop() as Token;
          if (top.t === 'lparen') {
            matched = true;
            break;
          }
          output.push(top);
        }
        if (!matched) throw new Error('Unbalanced parentheses');
        const top = stack[stack.length - 1];
        if (top && top.t === 'fn') output.push(stack.pop() as Token);
        break;
      }
    }
    prev = tok;
  }

  while (stack.length) {
    const top = stack.pop() as Token;
    if (top.t === 'lparen') throw new Error('Unbalanced parentheses');
    output.push(top);
  }
  return output;
}

function evalPostfix(postfix: Token[]): number {
  const stack: number[] = [];

  for (const tok of postfix) {
    if (tok.t === 'num') {
      stack.push(tok.v);
    } else if (tok.t === 'op') {
      if (tok.v === 'u-') {
        const a = stack.pop();
        if (a === undefined) throw new Error('Malformed expression');
        stack.push(-a);
        continue;
      }
      const b = stack.pop();
      const a = stack.pop();
      if (a === undefined || b === undefined) throw new Error('Malformed expression');
      switch (tok.v) {
        case '+': stack.push(a + b); break;
        case '-': stack.push(a - b); break;
        case '*': stack.push(a * b); break;
        case '/':
          if (b === 0) throw new Error('Division by zero');
          stack.push(a / b);
          break;
        case '%':
          if (b === 0) throw new Error('Division by zero');
          stack.push(a % b);
          break;
        case '^': stack.push(Math.pow(a, b)); break;
        default: throw new Error(`Unknown operator "${tok.v}"`);
      }
    } else if (tok.t === 'fn') {
      const def = FUNCTIONS[tok.v];
      if (!def) throw new Error(`Unknown function "${tok.v}"`);
      if (def.arity === 2) {
        const second = stack.pop();
        const first = stack.pop();
        if (first === undefined || second === undefined) {
          throw new Error(`${tok.v} needs two arguments`);
        }
        stack.push(def.fn(first, second));
      } else {
        const a = stack.pop();
        if (a === undefined) throw new Error(`Missing argument for ${tok.v}`);
        if (tok.v === 'sqrt' && a < 0) throw new Error('sqrt of a negative number');
        if ((tok.v === 'ln' || tok.v === 'log' || tok.v === 'log2') && a <= 0) {
          throw new Error(`${tok.v} needs a positive number`);
        }
        stack.push(def.fn(a));
      }
    }
  }

  if (stack.length !== 1) throw new Error('Malformed expression');
  const result = stack[0] as number;
  if (!Number.isFinite(result)) throw new Error('Result is not a finite number');
  return result;
}

/** Pretty-format a float for observation output. */
export function formatNumber(n: number): string {
  if (Number.isInteger(n)) return String(n);
  const rounded = Number(n.toPrecision(12));
  return String(rounded);
}
