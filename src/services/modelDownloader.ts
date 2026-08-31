import type { ModelDefinition } from '../llm/models';

/**
 * Base resolution (web bundle + tooling). Real GGUF downloads are not part
 * of the demo: the simulated engine needs no model files. Emits a quick fake
 * progress so the UI flow is exercised. Native builds override via
 * modelDownloader.native.ts.
 */

export const MODELS_DIR = '(in-app memory)';

export interface DownloadHandle {
  promise: Promise<string>;
  abort(): void;
}

export function ensureModelsDir(): Promise<string> {
  return Promise.resolve(MODELS_DIR);
}

export function localModelPath(model: ModelDefinition): string {
  return `memory://${model.id}`;
}

export function isModelDownloaded(_model: ModelDefinition): Promise<boolean> {
  return Promise.resolve(false);
}

export function deleteModel(_model: ModelDefinition): Promise<void> {
  return Promise.resolve();
}

export function downloadedModelSize(_model: ModelDefinition): Promise<number | null> {
  return Promise.resolve(null);
}

export function downloadModel(
  model: ModelDefinition,
  onProgress: (f: number) => void,
): DownloadHandle {
  onProgress(0.3);
  const promise = new Promise<string>((resolve) => {
    setTimeout(() => {
      onProgress(1);
      resolve(localModelPath(model));
    }, 500);
  });
  return { promise, abort() { /* noop */ } };
}
