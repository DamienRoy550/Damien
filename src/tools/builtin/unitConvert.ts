import type { Tool } from '../types';
import { ok, err } from '../types';
import { formatNumber } from '../calc/evaluator';

/**
 * Unit conversion via factor tables relative to a base unit.
 * Temperature uses affine transforms, handled separately.
 */
const LENGTH: Record<string, number> = {
  mm: 0.001, cm: 0.01, m: 1, km: 1000, in: 0.0254, ft: 0.3048, yd: 0.9144, mi: 1609.344,
  millimeter: 0.001, centimeter: 0.01, meter: 1, meter_: 1, metre: 1, kilometer: 1000,
  inch: 0.0254, inches: 0.0254, foot: 0.3048, feet: 0.3048, yard: 0.9144, mile: 1609.344, miles: 1609.344,
  nmi: 1852,
};

const MASS: Record<string, number> = {
  mg: 1e-6, g: 0.001, kg: 1, t: 1000, tonne: 1000,
  oz: 0.028349523125, lb: 0.45359237, lbs: 0.45359237, pound: 0.45359237, pounds: 0.45359237,
  gram: 0.001, grams: 0.001, kilogram: 1, kilograms: 1, stone: 6.35029318,
  milligram: 1e-6, ounce: 0.028349523125, ounces: 0.028349523125, ton: 907.18474,
};

const VOLUME: Record<string, number> = {
  ml: 0.001, l: 1, liter: 1, liters: 1, litre: 1, litres: 1, milliliter: 0.001, millilitre: 0.001,
  gal: 3.785411784, gallon: 3.785411784, gallons: 3.785411784,
  qt: 0.946352946, quart: 0.946352946, pt: 0.473176473, pint: 0.473176473,
  cup: 0.2365882365, floz: 0.0295735295625,
  m3: 1000,
};

const SPEED: Record<string, number> = {
  mps: 1, 'm/s': 1, kmh: 0.277777778, 'km/h': 0.277777778, kph: 0.277777778,
  mph: 0.44704, 'mi/h': 0.44704, knot: 0.514444444, knots: 0.514444444, kn: 0.514444444,
  'ft/s': 0.3048,
};

const DATA: Record<string, number> = {
  b: 1, byte: 1, bytes: 1, kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12,
  kib: 1024, mib: 1024 ** 2, gib: 1024 ** 3, tib: 1024 ** 4,
  kilobyte: 1e3, megabyte: 1e6, gigabyte: 1e9, terabyte: 1e12,
};

const TIME: Record<string, number> = {
  ms: 0.001, s: 1, sec: 1, second: 1, seconds: 1, min: 60, minute: 60, minutes: 60,
  h: 3600, hr: 3600, hour: 3600, hours: 3600, day: 86400, days: 86400,
  week: 604800, weeks: 604800, month: 2629746, year: 31556952, years: 31556952,
};

const TEMPS = new Set(['c', 'celsius', 'f', 'fahrenheit', 'k', 'kelvin']);

const TABLES: Array<{ name: string; table: Record<string, number> }> = [
  { name: 'length', table: LENGTH },
  { name: 'mass', table: MASS },
  { name: 'volume', table: VOLUME },
  { name: 'speed', table: SPEED },
  { name: 'data', table: DATA },
  { name: 'time', table: TIME },
];

function norm(u: string): string {
  return u.toLowerCase().trim().replace(/²/g, '2').replace(/³/g, '3').replace(/[.\s]/g, '');
}

function toCelsius(v: number, unit: string): number {
  if (unit === 'f' || unit === 'fahrenheit') return ((v - 32) * 5) / 9;
  if (unit === 'k' || unit === 'kelvin') return v - 273.15;
  return v;
}
function fromCelsius(c: number, unit: string): number {
  if (unit === 'f' || unit === 'fahrenheit') return (c * 9) / 5 + 32;
  if (unit === 'k' || unit === 'kelvin') return c + 273.15;
  return c;
}

export const unitConvert: Tool = {
  name: 'unit_convert',
  description:
    'Convert a value between units. Categories: length (m, km, mi, ft, in, cm...), mass (kg, g, lb, oz...), volume (l, ml, gal, cup...), speed (kmh, mph, mps, knot...), data (kb, mb, gb, kib, gib...), time (s, min, h, day...), temperature (c, f, k).',
  parameters: [
    { name: 'value', type: 'number', description: 'The numeric value to convert', required: true },
    { name: 'from_unit', type: 'string', description: 'Source unit, e.g. "km"', required: true },
    { name: 'to_unit', type: 'string', description: 'Target unit, e.g. "mi"', required: true },
  ],
  runsOffline: true,
  async execute(args) {
    const value = Number(args.value);
    const from = norm(String(args.from_unit ?? ''));
    const to = norm(String(args.to_unit ?? ''));
    if (!Number.isFinite(value)) return err('value must be a number');
    if (!from || !to) return err('from_unit and to_unit are required');

    // Temperature
    if (TEMPS.has(from) && TEMPS.has(to)) {
      const c = toCelsius(value, from);
      const out = fromCelsius(c, to);
      return ok(`${formatNumber(value)}° ${from} = ${formatNumber(out)} ${to}`);
    }

    for (const { name, table } of TABLES) {
      const normalizedTable: Record<string, number> = {};
      for (const [k, v] of Object.entries(table)) normalizedTable[norm(k)] = v;
      const f = normalizedTable[from];
      const t = normalizedTable[to];
      if (f !== undefined && t !== undefined) {
        const out = (value * f) / t;
        return ok(
          `${formatNumber(value)} ${from} = ${formatNumber(out)} ${to} (${name} conversion)`,
        );
      }
      if (f !== undefined || t !== undefined) {
        return err(`"${f !== undefined ? to : from}" is not a ${name} unit. Both units must be in the same category.`);
      }
    }

    return err(
      `Unknown unit(s): "${from}", "${to}". Supported categories: length, mass, volume, speed, data, time, temperature.`,
    );
  },
};
