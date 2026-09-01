import { create } from 'zustand';
import { loadJson, saveJson, createDebouncedSaver } from '../services/storage';
import type { AgentEvent, RunResult, StepRecord } from '../agent/types';

const KEY = 'damien.runs.v1';
const MAX_PERSISTED_RUNS = 60;

export type LiveStepStatus = 'pending' | 'running' | 'done' | 'error';

export interface LiveStep {
  index: number;
  status: LiveStepStatus;
  thought?: string;
  tool?: string;
  args?: Record<string, unknown>;
  observation?: string;
  error?: string;
}

export type RunStatus = 'running' | 'ok' | 'error' | 'cancelled' | 'max_steps';

export interface RunView {
  id: string;
  task: string;
  status: RunStatus;
  startedAt: number;
  finishedAt?: number;
  answer?: string;
  steps: LiveStep[];
  streamingText?: string;
  activeStep?: number;
  statusNote?: string;
}

interface RunsState {
  runs: RunView[];
  hydrated: boolean;

  hydrate(): Promise<void>;
  beginRun(task: string): string;
  applyEvent(runId: string, event: AgentEvent): void;
  finishRun(runId: string, result: RunResult): void;
  failRun(runId: string, error: string): void;
  clearAll(): void;
}

const debouncedSave = createDebouncedSaver(500);

function nowId(): string {
  return `run_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

function toLiveStep(step: StepRecord): LiveStep {
  return {
    index: step.index,
    status: step.error ? 'error' : 'done',
    thought: step.thought,
    tool: step.tool,
    args: step.toolArguments as Record<string, unknown> | undefined,
    observation: step.observation,
    error: step.error,
  };
}

export const useRuns = create<RunsState>((set, get) => ({
  runs: [],
  hydrated: false,

  async hydrate() {
    if (get().hydrated) return;
    const saved = await loadJson<RunView[]>(KEY, []);
    // Drop anything that was mid-flight when the app died.
    const runs = saved
      .filter((r) => r.status !== 'running')
      .map((r) => ({ ...r, streamingText: undefined }));
    set({ runs, hydrated: true });
  },

  beginRun(task) {
    const id = nowId();
    const run: RunView = {
      id,
      task,
      status: 'running',
      startedAt: Date.now(),
      steps: [],
    };
    set((s) => ({ runs: [...s.runs, run] }));
    return id;
  },

  applyEvent(runId, event) {
    set((s) => ({
      runs: s.runs.map((run) => {
        if (run.id !== runId) return run;
        switch (event.type) {
          case 'reply_started':
            return { ...run, activeStep: event.step, statusNote: 'thinking…' };
          case 'token':
            return { ...run, streamingText: event.full, statusNote: 'generating…' };
          case 'model_reply':
            return { ...run, streamingText: undefined };
          case 'tool_started':
            return {
              ...run,
              statusNote: `using ${event.tool}…`,
              steps: upsertStep(run.steps, {
                index: event.step,
                status: 'running',
                thought: undefined,
                tool: event.tool,
                args: event.args as Record<string, unknown>,
              }),
            };
          case 'tool_finished':
            return {
              ...run,
              statusNote: `${event.tool} done`,
              steps: markStep(run.steps, event.step, {
                status: 'done',
                observation: event.observation,
              }),
            };
          case 'tool_error':
            return {
              ...run,
              statusNote: `${event.tool} failed`,
              steps: markStep(run.steps, event.step, { status: 'error', error: event.error }),
            };
          default:
            return run;
        }
      }),
    }));
  },

  finishRun(runId, result) {
    set((s) => {
      const runs = s.runs.map((run) => {
        if (run.id !== runId) return run;
        return {
          ...run,
          status: result.status,
          answer: result.answer || undefined,
          finishedAt: Date.now(),
          streamingText: undefined,
          statusNote: undefined,
          steps: result.steps.map(toLiveStep),
        };
      });
      debouncedSave(KEY, runs.filter((r) => r.status !== 'running').slice(-MAX_PERSISTED_RUNS));
      return { runs };
    });
  },

  failRun(runId, error) {
    set((s) => {
      const runs = s.runs.map((run) =>
        run.id === runId
          ? {
              ...run,
              status: 'error' as const,
              answer: error,
              finishedAt: Date.now(),
              streamingText: undefined,
              statusNote: undefined,
            }
          : run,
      );
      debouncedSave(KEY, runs.filter((r) => r.status !== 'running').slice(-MAX_PERSISTED_RUNS));
      return { runs };
    });
  },

  clearAll() {
    set({ runs: [] });
    void saveJson(KEY, []);
  },
}));

function upsertStep(steps: LiveStep[], step: LiveStep): LiveStep[] {
  const idx = steps.findIndex((s) => s.index === step.index);
  if (idx === -1) return [...steps, step];
  const next = steps.slice();
  next[idx] = { ...(next[idx] as LiveStep), ...step };
  return next;
}

function markStep(
  steps: LiveStep[],
  index: number,
  patch: Partial<LiveStep>,
): LiveStep[] {
  return steps.map((s) => (s.index === index ? { ...s, ...patch } : s));
}
