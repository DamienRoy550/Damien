import { router } from 'expo-router';
import type { DeviceActions } from '../tools/types';

/**
 * Web implementation: "actually open it" means navigating INSIDE Damien —
 * our own browser panel (app/browse). No popup blockers, works in embedded
 * previews. The ↗ button in the panel opens a real tab from a user gesture.
 */
export const deviceActions: DeviceActions = {
  async copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard permission denied — non-fatal
    }
  },
  async openUrl(url) {
    if (/^https?:\/\//i.test(url)) {
      router.push(`/browse?url=${encodeURIComponent(url)}`);
      return;
    }
    // Non-http schemes have no meaning in the browser demo.
    throw new Error(`"${url}" cannot be launched inside the web demo — try it on your phone.`);
  },
  async openInAppBrowser(url) {
    router.push(`/browse?url=${encodeURIComponent(url)}`);
  },
};

export const isNativeApp = false;
