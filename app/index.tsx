import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Link, router } from 'expo-router';
import { theme } from '../src/theme';
import { RunBlock } from '../src/components/RunBlock';
import { useRuns } from '../src/state/runs';
import { useModels } from '../src/state/models';
import { useSettings } from '../src/state/settings';
import { startTask, cancelActiveTask } from '../src/runtime';
import { useVoiceInput } from '../src/hooks/useVoiceInput';
import { setSpeechEnabled, stopSpeaking } from '../src/services/speech';

const SUGGESTIONS = [
  'What is 234 * 17 + sqrt(961)?',
  'Convert 42 km to miles',
  'Take a note: buy oat milk tomorrow',
  'Remind me to stretch in 45 minutes',
  'Open youtube.com',
  'Run diagnostics',
];

function bootGreeting(honorific: string): string {
  const h = new Date().getHours();
  const tod = h >= 5 && h < 12 ? 'morning' : h >= 12 && h < 18 ? 'afternoon' : 'evening';
  return `Good ${tod}, ${honorific}. All systems are online — how may I be of service?`;
}

export default function ChatScreen() {
  const runs = useRuns((s) => s.runs);
  const modelsHydrated = useModels((s) => s.hydrated);
  const installed = useModels((s) => s.installed);
  const selectedModelId = useModels((s) => s.selectedModelId);
  const voiceOut = useSettings((s) => s.voiceOut);
  const persona = useSettings((s) => s.persona);
  const honorific = useSettings((s) => s.honorific);
  const updateSettings = useSettings((s) => s.update);

  const [input, setInput] = useState('');
  const listRef = useRef<FlatList>(null);

  const running = runs.some((r) => r.status === 'running');
  const isWeb = Platform.OS === 'web';
  const jarvis = persona === 'jarvis';
  const hasModel = isWeb || (selectedModelId !== null && installed[selectedModelId] !== undefined);

  const send = useCallback(
    (text?: string) => {
      const task = (text ?? input).trim();
      if (!task || running) return;
      setInput('');
      stopSpeaking();
      void startTask(task);
    },
    [input, running],
  );

  const voice = useVoiceInput((finalText) => {
    setInput(finalText);
    send(finalText);
  });

  // Show live speech transcript in the input field while listening.
  useEffect(() => {
    if (voice.listening && voice.transcript) {
      setInput(voice.transcript);
    }
  }, [voice.listening, voice.transcript]);

  const data = useMemo(() => runs.slice().reverse(), [runs]);

  useEffect(() => {
    const t = setTimeout(
      () => listRef.current?.scrollToOffset({ offset: 999999, animated: true }),
      60,
    );
    return () => clearTimeout(t);
  }, [runs.length, running]);

  useEffect(() => {
    setSpeechEnabled(voiceOut);
  }, [voiceOut]);

  const toggleVoiceOut = useCallback(() => {
    const next = !voiceOut;
    updateSettings({ voiceOut: next });
    setSpeechEnabled(next);
    if (!next) stopSpeaking();
  }, [voiceOut, updateSettings]);

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      keyboardVerticalOffset={0}
    >
      <View style={styles.header}>
        <View style={styles.headerLeft}>
          <Text style={styles.logo}>◆</Text>
          <View>
            <Text style={styles.title}>Damien</Text>
            <View style={styles.statusRow}>
              <View style={[styles.statusDot, running ? styles.statusDotBusy : styles.statusDotOnline]} />
              <Text style={styles.subtitle}>
                {running
                  ? 'working…'
                  : isWeb
                    ? `online · demo brain${jarvis ? ` · “${honorific}” protocol` : ''}`
                    : selectedModelId
                      ? 'online · on-device'
                      : 'awaiting model'}
              </Text>
            </View>
          </View>
        </View>
        <View style={styles.headerActions}>
          <HeaderButton label={voiceOut ? '🔊' : '🔇'} onPress={toggleVoiceOut} accessibilityLabel="Toggle voice output" />
          <HeaderButton label="Setup" onPress={() => router.push('/setup')} />
          <HeaderButton label="☰" onPress={() => router.push('/history')} accessibilityLabel="History" />
          <HeaderButton label="⚙" onPress={() => router.push('/settings')} accessibilityLabel="Settings" />
          {isWeb ? (
            <HeaderButton
              label="↗"
              accessibilityLabel="Open in a standalone tab (best for voice)"
              onPress={() => {
                if (Platform.OS === 'web') {
                  window.open(window.location.href, '_blank', 'noopener');
                }
              }}
            />
          ) : null}
        </View>
      </View>

      {!modelsHydrated ? (
        <View style={styles.center}>
          <ActivityIndicator color={theme.accent} />
        </View>
      ) : !hasModel ? (
        <GetStartedCard />
      ) : (
        <FlatList
          ref={listRef}
          style={styles.list}
          data={data}
          keyExtractor={(r) => r.id}
          renderItem={({ item }) => <RunBlock run={item} />}
          ListEmptyComponent={
            <View style={styles.emptyWrap}>
              <View style={[styles.bubble, styles.bootBubble]}>
                <Text style={styles.agentText}>{bootGreeting(honorific)}</Text>
              </View>
              <EmptyState onPick={send} isWeb={isWeb} />
            </View>
          }
          contentContainerStyle={styles.listContent}
          maintainVisibleContentPosition={{ minIndexForVisible: 0 }}
        />
      )}

      {voice.error ? (
        <Text style={styles.voiceError}>{voice.error}</Text>
      ) : voice.status ? (
        <Text style={styles.voiceStatus}>{voice.status}</Text>
      ) : null}

      <View style={styles.inputBar}>
        {isWeb ? (
          <Pressable
            onPress={voice.listening ? voice.stop : voice.start}
            style={[styles.micBtn, voice.listening && styles.micActive]}
            accessibilityLabel={voice.listening ? 'Stop listening' : 'Start voice input'}
          >
            <Text style={styles.micTxt}>{voice.listening ? '◉' : '🎙'}</Text>
          </Pressable>
        ) : null}
        <TextInput
          style={styles.input}
          placeholder={
            voice.listening
              ? 'Listening… speak now'
              : running
                ? 'Damien is working…'
                : `Give ${jarvis ? 'Damien a task, ' + honorific : 'Damien a task…'}`
          }
          placeholderTextColor={theme.faint}
          value={input}
          onChangeText={setInput}
          multiline
          editable={!running}
          onSubmitEditing={() => send()}
        />
        {running ? (
          <Pressable style={[styles.sendBtn, styles.stopBtn]} onPress={cancelActiveTask}>
            <Text style={styles.sendTxt}>■</Text>
          </Pressable>
        ) : (
          <Pressable
            style={[styles.sendBtn, !input.trim() && styles.sendDisabled]}
            onPress={() => send()}
            disabled={!input.trim()}
          >
            <Text style={styles.sendTxt}>➤</Text>
          </Pressable>
        )}
      </View>
    </KeyboardAvoidingView>
  );
}

function HeaderButton({
  label,
  onPress,
  accessibilityLabel,
}: {
  label: string;
  onPress: () => void;
  accessibilityLabel?: string;
}) {
  return (
    <Pressable onPress={onPress} style={styles.headerBtn} accessibilityLabel={accessibilityLabel}>
      <Text style={styles.headerBtnTxt}>{label}</Text>
    </Pressable>
  );
}

function GetStartedCard() {
  return (
    <View style={styles.center}>
      <View style={styles.startCard}>
        <Text style={styles.startTitle}>Welcome to Damien</Text>
        <Text style={styles.startBody}>
          Damien is an AI agent that runs entirely on your phone — no cloud, no API keys, total
          privacy. First, download a small language model (~0.4–2 GB, one time).
        </Text>
        <Link href="/setup" asChild>
          <Pressable style={styles.startBtn}>
            <Text style={styles.startBtnTxt}>Choose a model →</Text>
          </Pressable>
        </Link>
      </View>
    </View>
  );
}

function EmptyState({ onPick, isWeb }: { onPick: (t: string) => void; isWeb: boolean }) {
  return (
    <View style={styles.empty}>
      <Text style={styles.emptyTitle}>At your service</Text>
      <Text style={styles.emptyBody}>
        {isWeb
          ? 'Web demo — the brain is simulated, but every tool, step and result is real. Tap the mic to speak, or try:'
          : 'I plan, call tools, check results and iterate — all on-device. Tap the mic or try:'}
      </Text>
      <View style={styles.chips}>
        {SUGGESTIONS.map((s) => (
          <Pressable key={s} style={styles.chip} onPress={() => onPick(s)}>
            <Text style={styles.chipTxt} numberOfLines={1}>
              {s}
            </Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingTop: 54,
    paddingBottom: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: theme.border,
  },
  headerLeft: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  logo: { color: theme.accent, fontSize: 22 },
  title: { color: theme.text, fontSize: 18, fontWeight: '800' },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 1 },
  statusDot: { width: 6, height: 6, borderRadius: 3 },
  statusDotOnline: { backgroundColor: theme.teal },
  statusDotBusy: { backgroundColor: theme.warn },
  subtitle: { color: theme.faint, fontSize: 11 },
  headerActions: { flexDirection: 'row', gap: 5 },
  headerBtn: {
    backgroundColor: theme.surface,
    borderRadius: 999,
    paddingHorizontal: 9,
    paddingVertical: 6,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
  },
  headerBtnTxt: { color: theme.dim, fontSize: 12, fontWeight: '600' },
  list: { flex: 1 },
  listContent: { paddingVertical: 12 },
  emptyWrap: { gap: 8 },
  bubble: {
    borderRadius: theme.radius,
    paddingVertical: 10,
    paddingHorizontal: 12,
  },
  bootBubble: {
    backgroundColor: theme.agentBubble,
    alignSelf: 'flex-start',
    maxWidth: '96%',
    marginHorizontal: 14,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    borderBottomLeftRadius: 4,
  },
  agentText: { color: theme.text, fontSize: 15, lineHeight: 22 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 8,
    padding: 12,
    paddingBottom: 28,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: theme.border,
    backgroundColor: theme.surface,
  },
  micBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: theme.surfaceAlt,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
  },
  micActive: {
    backgroundColor: theme.tealSoft,
    borderColor: theme.teal,
  },
  micTxt: { fontSize: 16 },
  input: {
    flex: 1,
    backgroundColor: theme.surfaceAlt,
    borderRadius: 18,
    paddingHorizontal: 14,
    paddingTop: 10,
    paddingBottom: 10,
    color: theme.text,
    fontSize: 15,
    maxHeight: 110,
  },
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: theme.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendDisabled: { opacity: 0.35 },
  stopBtn: { backgroundColor: theme.danger },
  sendTxt: { color: '#fff', fontSize: 16 },
  voiceError: {
    color: theme.warn,
    fontSize: 11,
    textAlign: 'center',
    paddingHorizontal: 12,
  },
  voiceStatus: {
    color: theme.teal,
    fontSize: 11,
    textAlign: 'center',
    paddingHorizontal: 12,
  },
  startCard: {
    backgroundColor: theme.surface,
    margin: 24,
    padding: 20,
    borderRadius: 18,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    gap: 10,
  },
  startTitle: { color: theme.text, fontSize: 18, fontWeight: '800' },
  startBody: { color: theme.dim, fontSize: 14, lineHeight: 20 },
  startBtn: {
    backgroundColor: theme.accentSoft,
    borderRadius: 12,
    paddingVertical: 12,
    alignItems: 'center',
    marginTop: 4,
  },
  startBtnTxt: { color: theme.accent, fontWeight: '700', fontSize: 14 },
  empty: { alignItems: 'center', paddingTop: 10, paddingHorizontal: 24, gap: 8 },
  emptyTitle: { color: theme.text, fontSize: 18, fontWeight: '800' },
  emptyBody: { color: theme.dim, fontSize: 13, textAlign: 'center', lineHeight: 19 },
  chips: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
    justifyContent: 'center',
    marginTop: 10,
  },
  chip: {
    backgroundColor: theme.surface,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    borderRadius: 999,
    paddingHorizontal: 12,
    paddingVertical: 8,
    maxWidth: '100%',
  },
  chipTxt: { color: theme.dim, fontSize: 12 },
});
