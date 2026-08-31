import React, { useEffect, useState } from 'react';
import { Alert, FlatList, Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { theme } from '../src/theme';
import { MODEL_CATALOG, formatBytes, type ModelDefinition } from '../src/llm/models';
import { useModels } from '../src/state/models';
import { ProgressBar, Pill } from '../src/components/ProgressRing';
import * as downloader from '../src/services/modelDownloader';

export default function SetupScreen() {
  const downloads = useModels((s) => s.downloads);
  const installed = useModels((s) => s.installed);
  const selectedModelId = useModels((s) => s.selectedModelId);
  const { startDownload, remove, select } = useModels.getState();
  const [sizes, setSizes] = useState<Record<string, number | null>>({});

  const isWeb = Platform.OS === 'web';

  useEffect(() => {
    // Refresh real on-disk state (a file may have been removed by the OS).
    for (const model of MODEL_CATALOG) {
      void downloader.isModelDownloaded(model).then((exists: boolean) => {
        const registered = useModels.getState().installed[model.id] !== undefined;
        if (exists && !registered) {
          useModels.getState().completeDownload(model, downloader.localModelPath(model));
        }
      });
      void downloader.downloadedModelSize(model).then((size: number | null) => {
        setSizes((prev) => ({ ...prev, [model.id]: size }));
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <View style={styles.screen}>
      {isWeb ? (
        <View style={styles.demoBanner}>
          <Text style={styles.demoBannerTxt}>
            Web demo: downloads are simulated and the demo brain needs no model. On your phone,
            these are real GGUF files fetched straight from Hugging Face.
          </Text>
        </View>
      ) : null}

      <FlatList
        data={MODEL_CATALOG}
        keyExtractor={(m) => m.id}
        contentContainerStyle={styles.list}
        renderItem={({ item }) => {
          const dl = downloads[item.id];
          const inst = installed[item.id];
          const isSelected = selectedModelId === item.id;
          const realSize = sizes[item.id] ?? inst?.sizeBytes ?? item.sizeBytes;

          return (
            <View style={[styles.card, isSelected && styles.cardSelected]}>
              <View style={styles.cardHead}>
                <Text style={styles.cardTitle}>{item.name}</Text>
                {item.recommended ? <Pill label="RECOMMENDED" color={theme.teal} bg={theme.tealSoft} /> : null}
              </View>
              <Text style={styles.cardDesc}>{item.description}</Text>
              <View style={styles.metaRow}>
                <Pill label={item.parameterCount} />
                <Pill label={item.quantization} />
                <Pill label={formatBytes(realSize)} />
                <Pill label={item.license} />
                <Pill label={item.minDevice} />
              </View>

              {dl?.status === 'downloading' ? (
                <View style={styles.progressWrap}>
                  <ProgressBar progress={dl.progress} />
                  <Text style={styles.progressTxt}>{Math.round(dl.progress * 100)}%</Text>
                </View>
              ) : dl?.status === 'error' ? (
                <View style={styles.actionRow}>
                  <Text style={styles.errorTxt} numberOfLines={2}>
                    {dl.error}
                  </Text>
                  <ActionButton label="Retry" onPress={() => startDownload(item)} />
                </View>
              ) : inst ? (
                <View style={styles.actionRow}>
                  {isSelected ? (
                    <Text style={styles.selectedTxt}>✓ Active brain</Text>
                  ) : (
                    <ActionButton label="Select" primary onPress={() => select(item.id)} />
                  )}
                  <ActionButton
                    label="Delete"
                    danger
                    onPress={() =>
                      Alert.alert('Delete model', `Remove ${item.name} from this device?`, [
                        { text: 'Cancel', style: 'cancel' },
                        { text: 'Delete', style: 'destructive', onPress: () => remove(item) },
                      ])
                    }
                  />
                </View>
              ) : (
                <View style={styles.actionRow}>
                  <ActionButton label={`Download · ${formatBytes(item.sizeBytes)}`} primary onPress={() => startDownload(item)} />
                </View>
              )}
            </View>
          );
        }}
      />
    </View>
  );
}

function ActionButton({
  label,
  onPress,
  primary,
  danger,
}: {
  label: string;
  onPress: () => void;
  primary?: boolean;
  danger?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={[
        styles.btn,
        primary && styles.btnPrimary,
        danger && styles.btnDanger,
      ]}
    >
      <Text style={[styles.btnTxt, primary && styles.btnTxtPrimary, danger && styles.btnTxtDanger]}>
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  demoBanner: {
    margin: 12,
    marginBottom: 0,
    backgroundColor: theme.tealSoft,
    borderRadius: 12,
    padding: 12,
  },
  demoBannerTxt: { color: theme.teal, fontSize: 12, lineHeight: 17 },
  list: { padding: 12, gap: 12 },
  card: {
    backgroundColor: theme.surface,
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    padding: 14,
    gap: 8,
  },
  cardSelected: { borderColor: theme.accent, borderWidth: 1.5 },
  cardHead: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  cardTitle: { color: theme.text, fontSize: 16, fontWeight: '800', flexShrink: 1 },
  cardDesc: { color: theme.dim, fontSize: 13, lineHeight: 18 },
  metaRow: { flexDirection: 'row', gap: 6, flexWrap: 'wrap' },
  progressWrap: { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 4 },
  progressTxt: { color: theme.accent, fontSize: 12, fontWeight: '700', width: 38 },
  actionRow: { flexDirection: 'row', gap: 8, marginTop: 4, alignItems: 'center', flexWrap: 'wrap' },
  btn: {
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 10,
    backgroundColor: theme.surfaceAlt,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
  },
  btnPrimary: { backgroundColor: theme.accentSoft, borderColor: 'transparent' },
  btnDanger: { borderColor: 'rgba(255,107,107,0.4)' },
  btnTxt: { color: theme.dim, fontSize: 13, fontWeight: '700' },
  btnTxtPrimary: { color: theme.accent },
  btnTxtDanger: { color: theme.danger },
  selectedTxt: { color: theme.teal, fontWeight: '800', fontSize: 13 },
  errorTxt: { color: theme.danger, fontSize: 12, flex: 1 },
});
