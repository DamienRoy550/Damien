import AsyncStorage from '@react-native-async-storage/async-storage';
import type { KeyValueStore } from '../tools/types';

/** AsyncStorage-backed KeyValueStore shared by tools and app state. */
export const asyncStorageKv: KeyValueStore = {
  async get(key) {
    return AsyncStorage.getItem(key);
  },
  async set(key, value) {
    await AsyncStorage.setItem(key, value);
  },
  async delete(key) {
    await AsyncStorage.removeItem(key);
  },
  async keysWithPrefix(prefix) {
    const all = await AsyncStorage.getAllKeys();
    return all.filter((k) => k.startsWith(prefix));
  },
};

export async function loadJson<T>(key: string, fallback: T): Promise<T> {
  try {
    const raw = await AsyncStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export async function saveJson(key: string, value: unknown): Promise<void> {
  try {
    await AsyncStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage full / unavailable — non-fatal
  }
}

/** Debounced saver — keeps store→disk writes cheap while streaming. */
export function createDebouncedSaver(delayMs = 400) {
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  return (key: string, value: unknown) => {
    const existing = timers.get(key);
    if (existing) clearTimeout(existing);
    timers.set(
      key,
      setTimeout(() => {
        timers.delete(key);
        void saveJson(key, value);
      }, delayMs),
    );
  };
}
