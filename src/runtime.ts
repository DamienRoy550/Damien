import { AgentEngine } from './agent/AgentEngine';
import { ToolRegistry } from './tools/registry';
import { createDefaultRegistry } from './tools';
import type { ToolContext } from './tools/types';
import { getEngine } from './llm/engineFactory';
import type { LLMEngine, ModelDefinition } from './llm/types';
import { findModel } from './llm/models';
import { asyncStorageKv } from './services/storage';
import { deviceActions } from './services/device';
import { getScheduler } from './services/scheduler';
import { setSpeechEnabled, speak, stopSpeaking } from './services/speech';
import { useModels } from './state/models';
import { useSettings } from './state/settings';
import { useRuns } from './state/runs';
import type { AgentEvent, RunResult } from './agent/types';
import { Platform } from 'react-native';

/**
 * Runtime glue: owns the engine instance, tool context and the in-flight
 * abort controller. UI calls startTask() and renders from useRuns.
 */

let toolContextPromise: Promise<ToolContext> | null = null;
let activeRegistry: ToolRegistry | null = null;

async function buildSystemInfo() {
  const engine = getEngine();
  const { selectedModelId } = useModels.getState();
  const model = findModel(selectedModelId);
  const noteKeys = await asyncStorageKv.keysWithPrefix('note:');
  return {
    platform: Platform.OS === 'web' ? 'web (demo)' : `${Platform.OS} (on-device)`,
    engine: engine.info
      ? 'on-device LLM'
      : Platform.OS === 'web'
        ? 'demo brain (simulated)'
        : 'not loaded',
    model: engine.info?.name ?? model?.name,
    engineLoaded: engine.info !== null,
    toolCount: activeRegistry?.all.length ?? 0,
    noteCount: noteKeys.length,
  };
}

export async function getToolContext(): Promise<ToolContext> {
  if (!toolContextPromise) {
    toolContextPromise = (async () => {
      const scheduler = await getScheduler();
      return {
        now: () => new Date(),
        storage: asyncStorageKv,
        fetchFn: fetch,
        scheduler,
        device: deviceActions,
        systemInfo: buildSystemInfo,
        isDemo: false,
      };
    })();
  }
  return toolContextPromise;
}

export function getRegistry(ctx: ToolContext): ToolRegistry {
  activeRegistry = createDefaultRegistry(ctx);
  return activeRegistry;
}

const DEMO_MODEL: ModelDefinition = {
  id: 'demo-brain',
  name: 'Demo Brain (simulated)',
  description: 'Rule-based stand-in for the on-device model (web demo only).',
  url: '',
  sizeBytes: 0,
  fileName: 'demo-brain.bin',
  parameterCount: 'rules',
  quantization: '—',
  license: 'MIT',
  contextSize: 3072,
  minDevice: 'any',
};

/** Make sure the selected model is loaded into the engine. Throws if not ready. */
export async function ensureEngine(onProgress?: (f: number) => void): Promise<LLMEngine> {
  const engine = getEngine();
  if (engine.info && !engine.busy) return engine;

  // Web demo: the simulated brain needs no model file.
  if (Platform.OS !== 'ios' && Platform.OS !== 'android') {
    await engine.load({ ...DEMO_MODEL, localPath: 'memory://demo' }, onProgress);
    return engine;
  }

  const { installed, selectedModelId } = useModels.getState();
  const model = findModel(selectedModelId);
  if (!model) throw new Error('No model selected — open Setup and download a model first.');

  const installedEntry = installed[model.id];
  if (!installedEntry) throw new Error(`Model "${model.name}" is not downloaded yet.`);

  await engine.load({ ...model, localPath: installedEntry.path }, onProgress);
  return engine;
}

let activeAbort: AbortController | null = null;

export interface StartTaskHandlers {
  onEvent?: (event: AgentEvent) => void;
}

export async function startTask(task: string): Promise<void> {
  const trimmed = task.trim();
  if (!trimmed || activeAbort) return;

  const runs = useRuns.getState();
  const runId = runs.beginRun(trimmed);
  const dispatch = (event: AgentEvent) => {
    useRuns.getState().applyEvent(runId, event);
  };

  activeAbort = new AbortController();
  const signal = activeAbort.signal;

  const settings = useSettings.getState();
  setSpeechEnabled(settings.voiceOut);
  stopSpeaking();

  // Conversation memory: last few completed exchanges, oldest first.
  const priorRuns = useRuns
    .getState()
    .runs.filter((r) => (r.status === 'ok' || r.status === 'max_steps') && r.answer)
    .slice(-3);
  const history = priorRuns.flatMap((r) => [
    { role: 'user' as const, content: r.task },
    { role: 'assistant' as const, content: r.answer as string },
  ]);

  try {
    const ctx = await getToolContext();
    const registry = getRegistry(ctx);
    const engine = await ensureEngine((f) => {
      useRuns.setState((s) => ({
        runs: s.runs.map((r) =>
          r.id === runId ? { ...r, statusNote: `loading model… ${Math.round(f * 100)}%` } : r,
        ),
      }));
    });

    const agent = new AgentEngine(engine, registry, ctx, {
      timeZone: undefined, // Intl device default
      extraInstructions: settings.extraInstructions || undefined,
      persona: settings.persona,
      honorific: settings.honorific,
    });

    const result = await agent.run(trimmed, {
      maxSteps: settings.maxSteps,
      temperature: settings.temperature,
      observationCharLimit: settings.observationCharLimit,
      history,
      signal,
      onEvent: dispatch,
    });
    useRuns.getState().finishRun(runId, result);
    if (result.answer) void speak(result.answer);
  } catch (e) {
    useRuns.getState().failRun(runId, e instanceof Error ? e.message : String(e));
  } finally {
    activeAbort = null;
  }
}

export function cancelActiveTask(): void {
  activeAbort?.abort();
  getEngine().cancel();
}

export function isTaskRunning(): boolean {
  return activeAbort !== null;
}

export type { RunResult };
