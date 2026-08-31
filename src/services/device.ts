import * as Clipboard from 'expo-clipboard';
import { Linking, Platform } from 'react-native';
import type { DeviceActions } from '../tools/types';

export const deviceActions: DeviceActions = {
  async copyToClipboard(text) {
    await Clipboard.setStringAsync(text);
  },
  async openUrl(url) {
    const supported = await Linking.canOpenURL(url);
    if (!supported && !/^https?:\/\//i.test(url)) {
      throw new Error(`No app can open "${url}"`);
    }
    await Linking.openURL(url);
  },
};

export const isNativeApp = Platform.OS === 'ios' || Platform.OS === 'android';
