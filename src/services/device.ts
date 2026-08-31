import * as Clipboard from 'expo-clipboard';
import { Linking, Platform } from 'react-native';
import type { DeviceActions } from '../tools/types';

export const deviceActions: DeviceActions = {
  async copyToClipboard(text) {
    await Clipboard.setStringAsync(text);
  },
  async openUrl(url) {
    // No canOpenURL gate: on Android 11+ queries are needed for visibility
    // and on iOS it false-negatives for schemes not declared — just try the
    // launch and let failures surface as typed errors for the agent.
    await Linking.openURL(url);
  },
};

export const isNativeApp = Platform.OS === 'ios' || Platform.OS === 'android';
