import React, { useEffect } from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { StyleSheet, View } from 'react-native';
import { theme } from '../src/theme';
import { useSettings } from '../src/state/settings';
import { useModels } from '../src/state/models';
import { useRuns } from '../src/state/runs';

export default function RootLayout() {
  // Hydrate persisted state once at startup.
  useEffect(() => {
    void useSettings.getState().hydrate();
    void useModels.getState().hydrate();
    void useRuns.getState().hydrate();
  }, []);

  return (
    <View style={styles.root}>
      <StatusBar style="light" />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: theme.bg },
          headerTintColor: theme.text,
          headerTitleStyle: { fontWeight: '700' },
          headerShadowVisible: false,
          contentStyle: { backgroundColor: theme.bg },
          animation: 'slide_from_right',
        }}
      >
        <Stack.Screen name="index" options={{ headerShown: false }} />
        <Stack.Screen name="setup" options={{ title: 'Model Setup' }} />
        <Stack.Screen name="history" options={{ title: 'Task History' }} />
        <Stack.Screen name="settings" options={{ title: 'Settings' }} />
        <Stack.Screen name="browse" options={{ title: 'Browser' }} />
      </Stack>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.bg },
});
