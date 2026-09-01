import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { theme } from '../theme';

export function ProgressBar({
  progress,
  color = theme.accent,
  height = 6,
}: {
  progress: number;
  color?: string;
  height?: number;
}) {
  const pct = Math.max(0, Math.min(1, progress));
  return (
    <View style={[styles.track, { height }]}>
      <View style={[styles.fill, { width: `${Math.round(pct * 100)}%`, backgroundColor: color }]} />
    </View>
  );
}

export function Pill({
  label,
  color = theme.dim,
  bg,
}: {
  label: string;
  color?: string;
  bg?: string;
}) {
  return (
    <View style={[styles.pill, bg ? { backgroundColor: bg } : null]}>
      <Text style={[styles.pillText, { color }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  track: {
    backgroundColor: theme.border,
    borderRadius: 3,
    overflow: 'hidden',
  },
  fill: {
    height: '100%',
    borderRadius: 3,
  },
  pill: {
    backgroundColor: theme.surfaceAlt,
    borderRadius: 999,
    paddingHorizontal: 8,
    paddingVertical: 3,
    alignSelf: 'flex-start',
  },
  pillText: {
    fontSize: 11,
    fontWeight: '600',
  },
});
