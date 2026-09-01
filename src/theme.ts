export const theme = {
  bg: '#0A0C10',
  surface: '#12161F',
  surfaceAlt: '#171C27',
  border: '#232B3A',
  text: '#E8ECF4',
  dim: '#8B95A8',
  faint: '#5A6478',
  accent: '#6C8CFF',
  accentSoft: 'rgba(108,140,255,0.14)',
  teal: '#38E1C6',
  tealSoft: 'rgba(56,225,198,0.12)',
  danger: '#FF6B6B',
  warn: '#FFC24B',
  userBubble: '#24304A',
  agentBubble: '#151A24',
  radius: 14,
  /** Works on iOS and Android without importing react-native here. */
  mono: 'Menlo',
} as const;

export type Theme = typeof theme;
