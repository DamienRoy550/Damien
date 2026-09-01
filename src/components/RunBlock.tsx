import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { theme } from '../theme';
import type { RunView } from '../state/runs';
import { StepCard } from './StepCard';

/** One task = one run: user bubble, step cards, final answer. */
export function RunBlock({ run }: { run: RunView }) {
  const running = run.status === 'running';

  return (
    <View style={styles.block}>
      <View style={[styles.bubble, styles.userBubble]}>
        <Text style={styles.userText}>{run.task}</Text>
      </View>

      {run.steps.map((step) => (
        <StepCard key={`${run.id}-step-${step.index}`} step={step} />
      ))}

      {running && run.streamingText ? (
        <View style={[styles.bubble, styles.agentBubble]}>
          <Text style={styles.agentText}>{run.streamingText}</Text>
        </View>
      ) : null}

      {running && !run.streamingText ? (
        <View style={[styles.bubble, styles.agentBubble, styles.statusRow]}>
          <Text style={styles.statusText}>{run.statusNote ?? 'working…'}</Text>
        </View>
      ) : null}

      {!running && run.answer ? (
        <View style={[styles.bubble, styles.agentBubble]}>
          <Text style={styles.agentText}>{run.answer}</Text>
          {run.status === 'max_steps' ? (
            <Text style={styles.tag}>reached step limit</Text>
          ) : null}
        </View>
      ) : null}

      {!running && run.status === 'error' && !run.answer ? (
        <View style={[styles.bubble, styles.agentBubble, styles.errorBubble]}>
          <Text style={styles.errorText}>{run.answer ?? 'Something went wrong.'}</Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  block: {
    gap: 8,
    paddingHorizontal: 14,
    paddingVertical: 6,
  },
  bubble: {
    borderRadius: theme.radius,
    paddingVertical: 10,
    paddingHorizontal: 12,
  },
  userBubble: {
    backgroundColor: theme.userBubble,
    alignSelf: 'flex-end',
    maxWidth: '92%',
    borderBottomRightRadius: 4,
  },
  agentBubble: {
    backgroundColor: theme.agentBubble,
    alignSelf: 'flex-start',
    maxWidth: '96%',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    borderBottomLeftRadius: 4,
  },
  userText: {
    color: theme.text,
    fontSize: 15,
    lineHeight: 21,
  },
  agentText: {
    color: theme.text,
    fontSize: 15,
    lineHeight: 22,
  },
  statusRow: {
    flexDirection: 'row',
  },
  statusText: {
    color: theme.faint,
    fontSize: 13,
    fontStyle: 'italic',
  },
  errorBubble: {
    borderColor: 'rgba(255,107,107,0.4)',
  },
  errorText: {
    color: theme.danger,
    fontSize: 14,
  },
  tag: {
    marginTop: 6,
    color: theme.warn,
    fontSize: 11,
  },
});
