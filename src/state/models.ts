import { create } from 'zustand';
import { loadJson, saveJson } from '../services/storage';
import * as downloader from '../services/modelDownloader';
import type { ModelDefinition } from '../llm/models';

const KEY = 'damien.models.v1';

export type DownloadStatus = 'idle' | 'downloading' | 'downloaded' | 'error';

export interface DownloadState {
  status: DownloadStatus;
  progress: number;
  error?: string;
}

export interface InstalledModel {
  path: string;
  sizeBytes?: number;
  installedAt: string;
}

interface ModelsState {
  downloads: Record<string, DownloadState>;
  installed: Record<string, InstalledModel>;
  selectedModelId: string | null;
  hydrated: boolean;

  hydrate(): Promise<void>;
  startDownload(model: ModelDefinition): void;
  progressDownload(id: string, fraction: number): void;
  failDownload(id: string, error: string): void;
  completeDownload(model: ModelDefinition, path: string): void;
  remove(model: ModelDefinition): void;
  select(id: string): void;
}

const activeJobs = new Map<string, downloader.DownloadHandle>();

export const useModels = create<ModelsState>((set, get) => ({
  downloads: {},
  installed: {},
  selectedModelId: null,
  hydrated: false,

  async hydrate() {
    if (get().hydrated) return;
    const saved = await loadJson<Pick<ModelsState, 'installed' | 'selectedModelId'>>(KEY, {
      installed: {},
      selectedModelId: null,
    });
    set({ ...saved, hydrated: true });
  },

  startDownload(model) {
    if (activeJobs.has(model.id)) return;
    set((s) => ({
      downloads: {
        ...s.downloads,
        [model.id]: { status: 'downloading', progress: 0 },
      },
    }));
    const handle = downloader.downloadModel(model, (fraction) => {
      get().progressDownload(model.id, fraction);
    });
    activeJobs.set(model.id, handle);
    handle.promise
      .then((path) => get().completeDownload(model, path))
      .catch((e: unknown) => {
        get().failDownload(model.id, e instanceof Error ? e.message : String(e));
      })
      .finally(() => activeJobs.delete(model.id));
  },

  progressDownload(id, fraction) {
    set((s) => {
      const current = s.downloads[id];
      if (!current || current.status !== 'downloading') return s;
      return {
        downloads: {
          ...s.downloads,
          [id]: { ...current, progress: fraction },
        },
      };
    });
  },

  failDownload(id, error) {
    set((s) => ({
      downloads: { ...s.downloads, [id]: { status: 'error', progress: 0, error } },
    }));
  },

  completeDownload(model, path) {
    set((s) => {
      const installed = { ...s.installed };
      void downloader.downloadedModelSize(model)
        .then((size) => {
          const entry = installed[model.id];
          if (entry) {
            useModels.setState((st) => ({
              installed: {
                ...st.installed,
                [model.id]: { ...entry, sizeBytes: size ?? undefined },
              },
            }));
          }
        })
        .catch(() => undefined);
      installed[model.id] = { path, installedAt: new Date().toISOString() };
      const next: Partial<ModelsState> = {
        installed,
        downloads: { ...s.downloads, [model.id]: { status: 'downloaded', progress: 1 } },
      };
      // Auto-select the first model ever downloaded.
      if (!s.selectedModelId) next.selectedModelId = model.id;
      persist(next);
      return next as ModelsState;
    });
  },

  remove(model) {
    void downloader.deleteModel(model);
    set((s) => {
      const installed = { ...s.installed };
      delete installed[model.id];
      const next: Partial<ModelsState> = {
        installed,
        downloads: { ...s.downloads, [model.id]: { status: 'idle', progress: 0 } },
      };
      if (s.selectedModelId === model.id) next.selectedModelId = null;
      persist(next);
      return next as ModelsState;
    });
  },

  select(id) {
    set({ selectedModelId: id });
    persist({ selectedModelId: id });
  },
}));

function persist(patch: Partial<Pick<ModelsState, 'installed' | 'selectedModelId'>>): void {
  const state = useModels.getState();
  void saveJson(KEY, {
    installed: { ...state.installed, ...(patch.installed ?? {}) },
    selectedModelId: patch.selectedModelId ?? state.selectedModelId,
  });
}
