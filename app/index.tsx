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
import { startTask, cancelActiveTask } from '../src/runtime';

const SUGGESTIONS = [
  'What is 234 * 17 + sqrt(961)?',
  'Convert 42 km to miles',
  'Take a note: buy oat milk tomorrow',
  'Remind me to stretch in 45 minutes',
  'Fetch https://example.com and summarize it',
];

export default function ChatScreen() {
  const runs = useRuns((s) => s.runs);
  const modelsHydrated = useModels((s) => s.hydrated);
  const installed = useModels((s) => s.installed);
  const selectedModelId = useModels((s) => s.selectedModelId);
  const [input, setInput] = useState('');
  const listRef = useRef<FlatList>(null);

  const running = runs.some((r) => r.status === 'running');
  const isWeb = Platform.OS === 'web';
  const hasModel = isWeb || (selectedModelId !== null && installed[selectedModelId] !== undefined);

  const data = useMemo(() => runs.slice().reverse(), [runs]);

  useEffect(() => {
    // keep the newest content visible
    const t = setTimeout(() => listRef.current?.scrollToOffset({ offset: 999999, animated: true }), 60);
    return () => clearTimeout(t);
  }, [runs.length, running]);

  const send = useCallback(
    (text?: string) => {
      const task = (text ?? input).trim();
      if (!task || running) return;
      setInput('');
      void startTask(task);
    },
    [input, running],
  );

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
            <Text style={styles.subtitle}>
              {isWeb
                ? 'demo mode · simulated on-device brain'
                : selectedModelId
                  ? 'offline AI agent'
                  : 'no model installed'}
            </Text>
          </View>
        </View>
        <View style={styles.headerActions}>
          <HeaderButton label="Setup" onPress={() => router.push('/setup')} />
          <HeaderButton label="History" onPress={() => router.push('/history')} />
          <HeaderButton label="Settings" onPress={() => router.push('/settings')} />
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
          ListEmptyComponent={<EmptyState onPick={send} isWeb={isWeb} />}
          contentContainerStyle={styles.listContent}
          maintainVisibleContentPosition={{ minIndexForVisible: 0 }}
        />
      )}

      <View style={styles.inputBar}>
        <TextInput
          style={styles.input}
          placeholder={running ? 'Damien is working…' : 'Give Damien a task…'}
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

function HeaderButton({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={styles.headerBtn}>
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
      <Text style={styles.emptyTitle}>Give me a task</Text>
      <Text style={styles.emptyBody}>
        {isWeb
          ? 'You are in the web demo — the brain is simulated, but every tool, step and result below is real.'
          : 'I plan, call tools, check results and iterate — all on-device.'}
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
  subtitle: { color: theme.faint, fontSize: 11, marginTop: 1 },
  headerActions: { flexDirection: 'row', gap: 6 },
  headerBtn: {
    backgroundColor: theme.surface,
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
  },
  headerBtnTxt: { color: theme.dim, fontSize: 12, fontWeight: '600' },
  list: { flex: 1 },
  listContent: { paddingVertical: 12 },
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
  empty: { alignItems: 'center', paddingTop: 40, paddingHorizontal: 24, gap: 8 },
  emptyTitle: { color: theme.text, fontSize: 20, fontWeight: '800' },
  emptyBody: { color: theme.dim, fontSize: 13, textAlign: 'center', lineHeight: 19 },
  chips: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
    justifyContent: 'center',
    marginTop: 12,
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
