export type JsonValue = string | number | boolean | null | JsonValue[] | JsonRecord;
export interface JsonRecord {
  [key: string]: JsonValue | undefined;
}
export type ToolName = string;

export type ToolParamType = 'string' | 'number' | 'boolean';

export interface ToolParam {
  name: string;
  type: ToolParamType;
  description: string;
  required?: boolean;
  enum?: string[];
  /** Optional default, surfaced to the model in docs. */
  default?: string | number | boolean;
}

export interface ToolResult {
  ok: boolean;
  /** Machine-readable output handed back to the model as the OBSERVATION. */
  output: string;
  error?: string;
}

export interface KeyValueStore {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
  /** All keys with a given prefix, e.g. `note:`. */
  keysWithPrefix(prefix: string): Promise<string[]>;
}

export interface ReminderScheduler {
  /** Schedule a notification. Returns a platform notification id. */
  schedule(input: {
    id: string;
    title: string;
    body: string;
    fireAt: Date;
  }): Promise<string>;
  requestPermissions(): Promise<boolean>;
}

export interface DeviceActions {
  copyToClipboard(text: string): Promise<void>;
  openUrl(url: string): Promise<void>;
  /** Open a website in the platform's in-app browser (panel on web, tab on native). */
  openInAppBrowser(url: string): Promise<void>;
}

/** Snapshot for the system_status diagnostic tool. */
export interface SystemSnapshot {
  platform: string;
  engine: string;
  model?: string;
  engineLoaded: boolean;
  toolCount: number;
  noteCount: number;
  reminderCount?: number;
}

/** Everything a tool may touch, injected so tools stay testable in Node. */
export interface ToolContext {
  now(): Date;
  timeZone?: string;
  storage: KeyValueStore;
  fetchFn: typeof fetch;
  scheduler?: ReminderScheduler;
  device?: DeviceActions;
  /** Diagnostics provider — injected by the runtime. */
  systemInfo?: () => Promise<SystemSnapshot>;
  /** True when running in the demo/simulated engine (web preview, tests). */
  isDemo?: boolean;
}

export interface Tool {
  name: ToolName;
  description: string;
  parameters: ToolParam[];
  /** Hint for the UI: can this tool run without any connectivity? */
  runsOffline: boolean;
  execute(args: JsonRecord, ctx: ToolContext): Promise<ToolResult>;
}

export function ok(output: string): ToolResult {
  return { ok: true, output };
}

export function err(error: string): ToolResult {
  return { ok: false, output: `ERROR: ${error}`, error };
}

/** Validate + coerce arguments against a tool's declared params. */
export function coerceArguments(
  tool: Tool,
  args: JsonRecord,
): { ok: true; args: JsonRecord } | { ok: false; error: string } {
  const out: JsonRecord = {};
  for (const p of tool.parameters) {
    const raw = args[p.name];
    if (raw === undefined || raw === null || raw === '') {
      if (p.required) {
        return { ok: false, error: `Missing required argument "${p.name}" for tool "${tool.name}".` };
      }
      continue;
    }
    if (p.enum && !p.enum.includes(String(raw))) {
      return {
        ok: false,
        error: `Invalid value "${String(raw)}" for "${p.name}". Allowed: ${p.enum.join(', ')}.`,
      };
    }
    if (p.type === 'number') {
      const n = typeof raw === 'number' ? raw : Number(String(raw).trim());
      if (!Number.isFinite(n)) {
        return { ok: false, error: `Argument "${p.name}" must be a number, got "${String(raw)}".` };
      }
      out[p.name] = n;
    } else if (p.type === 'boolean') {
      if (typeof raw === 'boolean') out[p.name] = raw;
      else if (raw === 'true') out[p.name] = true;
      else if (raw === 'false') out[p.name] = false;
      else return { ok: false, error: `Argument "${p.name}" must be true or false.` };
    } else {
      out[p.name] = String(raw);
    }
  }
  return { ok: true, args: out };
}
