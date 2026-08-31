import { create } from 'zustand';
import { loadJson, saveJson, createDebouncedSaver } from '../services/storage';

const KEY = 'damien.settings.v1';

export interface SettingsState {
  temperature: number;
  maxSteps: number;
  strictJson: boolean;
  extraInstructions: string;
  observationCharLimit: number;
  hydrated: boolean;

  update(patch: Partial<Omit<SettingsState, 'hydrated'>>): void;
  hydrate(): Promise<void>;
}

const debouncedSave = createDebouncedSaver(300);

export const useSettings = create<SettingsState>((set, get) => ({
  temperature: 0.2,
  maxSteps: 6,
  strictJson: true,
  extraInstructions: '',
  observationCharLimit: 900,
  hydrated: false,

  update(patch) {
    set(patch);
    const { temperature, maxSteps, strictJson, extraInstructions, observationCharLimit } = get();
    debouncedSave(KEY, { temperature, maxSteps, strictJson, extraInstructions, observationCharLimit });
  },

  async hydrate() {
    if (get().hydrated) return;
    const saved = await loadJson<Partial<SettingsState>>(KEY, {});
    set({ ...saved, hydrated: true });
  },
}));
