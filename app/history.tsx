import React, { useState } from 'react';
import { Alert, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { theme } from '../src/theme';
import { useRuns, type RunView } from '../src/state/runs';
import { Pill } from '../src/components/ProgressRing';

const STATUS_COLORS: Record<string, string> = {
  ok: theme.teal,
  error: theme.danger,
  cancelled: theme.warn,
  max_steps: theme.warn,
  running: theme.accent,
};

export default function HistoryScreen() {
  const runs = useRuns((s) => s.runs).filter((r) => r.status !== 'running');
  const clearAll = useRuns((s) => s.clearAll);
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <View style={styles.screen}>
      <View style={styles.topRow}>
        <Text style={styles.count}>
          {runs.length} completed task{runs.length === 1 ? '' : 's'}
        </Text>
        {runs.length > 0 ? (
          <Pressable
            onPress={() =>
              Alert.alert('Clear history', 'Delete all saved tasks?', [
                { text: 'Cancel', style: 'cancel' },
                { text: 'Clear', style: 'destructive', onPress: clearAll },
              ])
            }
          >
            <Text style={styles.clear}>Clear all</Text>
          </Pressable>
        ) : null}
      </View>

      <FlatList
        data={runs.slice().reverse()}
        keyExtractor={(r) => r.id}
        contentContainerStyle={styles.list}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTxt}>Completed tasks will appear here.</Text>
          </View>
        }
        renderItem={({ item }) => (
          <HistoryCard
            run={item}
            open={openId === item.id}
            onToggle={() => setOpenId(openId === item.id ? null : item.id)}
          />
        )}
      />
    </View>
  );
}

function HistoryCard({ run, open, onToggle }: { run: RunView; open: boolean; onToggle: () => void }) {
  const color = STATUS_COLORS[run.status] ?? theme.dim;
  const when = new Date(run.startedAt).toLocaleString();

  return (
    <Pressable style={styles.card} onPress={onToggle}>
      <View style={styles.cardHead}>
        <Text style={styles.task} numberOfLines={open ? undefined : 2}>
          {run.task}
        </Text>
        <Pill label={run.status.toUpperCase()} color={color} />
      </View>
      <Text style={styles.when}>{when}</Text>
      {open ? (
        <View style={styles.details}>
          {run.steps.map((s) => (
            <View key={s.index} style={styles.stepRow}>
              <Text style={styles.stepTool}>
                {s.index}. {s.tool ?? 'think'} {s.error ? '✕' : '✓'}
              </Text>
              {s.observation ? (
                <Text style={styles.stepObs} numberOfLines={3}>
                  {s.observation}
                </Text>
              ) : null}
            </View>
          ))}
          {run.answer ? <Text style={styles.answer}>{run.answer}</Text> : null}
        </View>
      ) : (
        <Text style={styles.preview} numberOfLines={1}>
          {run.answer ?? '(no answer)'}
        </Text>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  topRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  count: { color: theme.dim, fontSize: 13 },
  clear: { color: theme.danger, fontSize: 13, fontWeight: '700' },
  list: { padding: 12, gap: 10 },
  card: {
    backgroundColor: theme.surface,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    padding: 12,
    gap: 6,
  },
  cardHead: { flexDirection: 'row', gap: 8, alignItems: 'flex-start' },
  task: { color: theme.text, fontSize: 14, fontWeight: '700', flex: 1 },
  when: { color: theme.faint, fontSize: 11 },
  preview: { color: theme.dim, fontSize: 12 },
  details: { gap: 8, marginTop: 4 },
  stepRow: { gap: 2 },
  stepTool: { color: theme.teal, fontSize: 12, fontWeight: '700' },
  stepObs: { color: theme.dim, fontSize: 11 },
  answer: { color: theme.text, fontSize: 13, lineHeight: 18 },
  empty: { alignItems: 'center', paddingVertical: 60 },
  emptyTxt: { color: theme.faint, fontSize: 13 },
});
