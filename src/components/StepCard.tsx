import React from 'react';
import { View, Text, StyleSheet, Pressable } from 'react-native';
import { theme } from '../theme';
import type { LiveStep } from '../state/runs';

/** Renders one agent step: thought → tool + args → observation. */
export function StepCard({ step }: { step: LiveStep }) {
  const isRunning = step.status === 'running';
  const isError = step.status === 'error';
  const accent = isError ? theme.danger : isRunning ? theme.warn : theme.teal;

  return (
    <View style={[styles.card, isError && styles.cardError]}>
      <View style={styles.header}>
        <View style={[styles.dot, { backgroundColor: accent }]} />
        <Text style={[styles.tool, { color: accent }]} numberOfLines={1}>
          {step.tool ?? 'thinking'} {isRunning ? '…' : isError ? '✕' : '✓'}
        </Text>
        <Text style={styles.stepNo}>step {step.index}</Text>
      </View>

      {step.thought ? <Text style={styles.thought}>{step.thought}</Text> : null}

      {step.args && Object.keys(step.args).length > 0 ? (
        <View style={styles.argsBox}>
          <Text style={styles.mono} numberOfLines={4}>
            {formatArgs(step.args)}
          </Text>
        </View>
      ) : null}

      {step.observation ? (
        <Text style={styles.observation} numberOfLines={6}>
          {step.observation}
        </Text>
      ) : null}

      {step.error ? <Text style={styles.error}>⚠ {step.error}</Text> : null}
    </View>
  );
}

function formatArgs(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args, null, 0);
  } catch {
    return String(args);
  }
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: theme.agentBubble,
    borderRadius: theme.radius,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    padding: 10,
    gap: 6,
  },
  cardError: {
    borderColor: 'rgba(255,107,107,0.35)',
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
  dot: {
    width: 7,
    height: 7,
    borderRadius: 4,
  },
  tool: {
    fontSize: 13,
    fontWeight: '700',
    flex: 1,
  },
  stepNo: {
    fontSize: 11,
    color: theme.faint,
  },
  thought: {
    color: theme.dim,
    fontSize: 13,
    fontStyle: 'italic',
  },
  argsBox: {
    backgroundColor: 'rgba(0,0,0,0.35)',
    borderRadius: 8,
    paddingHorizontal: 8,
    paddingVertical: 6,
  },
  mono: {
    color: theme.teal,
    fontSize: 12,
  },
  observation: {
    color: theme.dim,
    fontSize: 12,
    lineHeight: 17,
  },
  error: {
    color: theme.danger,
    fontSize: 12,
  },
});
