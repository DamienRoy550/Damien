import { router } from 'expo-router';
import { Linking } from 'react-native';
import type { DeviceActions } from '../tools/types';

/**
 * Native implementation: apps and schemes launch for real via Linking;
 * websites open in the OS in-app browser tab (expo-web-browser) — an actual
 * browser window over the app, the way an assistant should do it.
 */
export const deviceActions: DeviceActions = {
  async copyToClipboard(text) {
    const Clipboard = await import('expo-clipboard');
    await Clipboard.setStringAsync(text);
  },
  async openUrl(url) {
    await Linking.openURL(url);
  },
  async openInAppBrowser(url) {
    try {
      const WebBrowser = await import('expo-web-browser');
      await WebBrowser.openBrowserAsync(url, { controlsColor: '#6C8CFF' });
      return;
    } catch {
      // Fall back to the default browser.
      await Linking.openURL(url);
    }
  },
};

export const isNativeApp = true;
