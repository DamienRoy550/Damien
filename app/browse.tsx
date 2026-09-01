import React from 'react';
import { Pressable, StyleSheet, Text, View, Platform } from 'react-native';
import { useLocalSearchParams, router } from 'expo-router';
import { theme } from '../src/theme';

/**
 * Damien's built-in browser panel.
 *
 * Web: renders the target site in an embedded frame — this is how
 * "open website" actually opens things inside the demo preview (popup
 * blockers can't touch it). Sites that refuse embedding show a hint plus
 * a one-tap external-open button.
 *
 * Native: the site is opened in the OS in-app browser tab by the tool
 * layer directly, so this screen mainly serves the web demo.
 */
export default function BrowseScreen() {
  const params = useLocalSearchParams<{ url?: string }>();
  const url = typeof params.url === 'string' ? params.url : '';
  let display = url;
  let embedUrl = url;
  try {
    const parsed = new URL(url);
    display = parsed.host + (parsed.pathname === '/' ? '' : parsed.pathname);
  } catch {
    // keep raw
  }
  const canEmbed = /^https?:\/\//i.test(url);

  return (
    <View style={styles.screen}>
      <View style={styles.toolbar}>
        <Pressable style={styles.backBtn} onPress={() => router.back()}>
          <Text style={styles.backTxt}>‹ Back</Text>
        </Pressable>
        <Text style={styles.url} numberOfLines={1}>
          {display || 'browser'}
        </Text>
        {canEmbed ? (
          <Pressable
            style={styles.extBtn}
            onPress={() => {
              if (Platform.OS === 'web') {
                window.open(url, '_blank', 'noopener,noreferrer');
              }
            }}
          >
            <Text style={styles.extTxt}>↗</Text>
          </Pressable>
        ) : null}
      </View>

      {canEmbed && Platform.OS === 'web' ? (
        <>
          <iframe
            src={embedUrl}
            title={display}
            style={{ flex: 1, width: '100%', border: '0', backgroundColor: '#fff' }}
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          />
          <View style={styles.hintBar}>
            <Text style={styles.hintTxt}>
              Blank frame? This site refuses embedding — tap ↗ to open it in a real tab.
            </Text>
          </View>
        </>
      ) : (
        <View style={styles.fallback}>
          <Text style={styles.fallbackTitle}>{url || 'No URL'}</Text>
          <Text style={styles.fallbackBody}>
            On this platform the page opens in the system browser. This embedded view is part of
            the web demo experience.
          </Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  toolbar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    paddingHorizontal: 12,
    paddingTop: 54,
    paddingBottom: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: theme.border,
    backgroundColor: theme.surface,
  },
  backBtn: { paddingVertical: 4, paddingHorizontal: 6 },
  backTxt: { color: theme.accent, fontSize: 14, fontWeight: '700' },
  url: { color: theme.text, fontSize: 13, flex: 1, textAlign: 'center' },
  extBtn: { paddingVertical: 4, paddingHorizontal: 8 },
  extTxt: { color: theme.accent, fontSize: 16 },
  hintBar: {
    backgroundColor: theme.surface,
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: theme.border,
  },
  hintTxt: { color: theme.faint, fontSize: 11, textAlign: 'center' },
  fallback: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24, gap: 10 },
  fallbackTitle: { color: theme.text, fontSize: 15, fontWeight: '700' },
  fallbackBody: { color: theme.dim, fontSize: 13, textAlign: 'center', lineHeight: 19 },
});
