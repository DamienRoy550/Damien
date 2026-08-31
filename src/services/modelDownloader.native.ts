import { File, Directory, Paths, DownloadTask } from 'expo-file-system';
import type { DownloadProgress } from 'expo-file-system';
import type { ModelDefinition } from '../llm/models';
import type { DownloadHandle } from './modelDownloader';

/**
 * Native implementation on the SDK 57 file-system API.
 * Uses DownloadTask for progress + pause/abort support, downloading the
 * GGUF straight to the app's documents directory.
 */

export const MODELS_DIR = 'models';

export function modelsDirectory(): Directory {
  return new Directory(Paths.document, MODELS_DIR);
}

export function ensureModelsDir(): Directory {
  const dir = modelsDirectory();
  if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
  return dir;
}

export function localModelFile(model: ModelDefinition): File {
  return new File(modelsDirectory(), model.fileName);
}

export function localModelPath(model: ModelDefinition): string {
  return localModelFile(model).uri;
}

export async function isModelDownloaded(model: ModelDefinition): Promise<boolean> {
  const file = localModelFile(model);
  return file.exists && file.size > 0;
}

export async function deleteModel(model: ModelDefinition): Promise<void> {
  const file = localModelFile(model);
  if (file.exists) file.delete();
}

export async function downloadedModelSize(model: ModelDefinition): Promise<number | null> {
  const file = localModelFile(model);
  return file.exists ? file.size : null;
}

/** Start the download with progress + abort support. */
export function downloadModel(
  model: ModelDefinition,
  onProgress: (fraction: number) => void,
): DownloadHandle {
  ensureModelsDir();
  const destination = localModelFile(model);
  // Stream to a temp sibling so partial files are never mistaken for models.
  const temp = new File(modelsDirectory(), `${model.fileName}.part`);

  let task: DownloadTask | null = new DownloadTask(model.url, temp, {
    onProgress: (progress: DownloadProgress) => {
      if (progress.totalBytes > 0) {
        onProgress(Math.min(0.99, progress.bytesWritten / progress.totalBytes));
      }
    },
  });

  const promise = (async () => {
    try {
      const file = await task!.downloadAsync();
      if (!file) throw new Error('Download was paused or cancelled');
      if (destination.exists) destination.delete();
      file.move(destination);
      onProgress(1);
      return destination.uri;
    } finally {
      task?.release();
      task = null;
    }
  })();

  return {
    promise,
    abort() {
      task?.cancel();
    },
  };
}
