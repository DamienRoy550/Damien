import React from 'react';
import { Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import { theme } from '../src/theme';
import { useSettings } from '../src/state/settings';

export default function SettingsScreen() {
  const settings = useSettings();
  const { update } = settings;

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <Section title="Generation">
        <Stepper
          label="Temperature"
          hint="Lower = precise & deterministic. Recommended: 0.2"
          value={settings.temperature}
          step={0.1}
          min={0}
          max={1}
          format={(v) => v.toFixed(1)}
          onChange={(temperature) => update({ temperature })}
        />
        <Stepper
          label="Max tool steps"
          hint="How many plan→act→observe rounds per task"
          value={settings.maxSteps}
          step={1}
          min={1}
          max={12}
          onChange={(maxSteps) => update({ maxSteps })}
        />
        <Stepper
          label="Observation size limit"
          hint="Characters of tool output shown to the model"
          value={settings.observationCharLimit}
          step={100}
          min={300}
          max={2000}
          onChange={(observationCharLimit) => update({ observationCharLimit })}
        />
        <ToggleRow
          label="Strict JSON mode"
          hint="Grammar-constrain the model's output to valid JSON. Leave on unless a model misbehaves."
          value={settings.strictJson}
          onChange={(strictJson) => update({ strictJson })}
        />
      </Section>

      <Section title="Standing instructions">
        <Text style={styles.hint}>
          Extra rules Damien follows on every task (kept in the system prompt).
        </Text>
        <TextInput
          style={styles.multiline}
          multiline
          placeholder="e.g. Always answer in English. Prefer metric units. I'm a nurse — keep health answers careful."
          placeholderTextColor={theme.faint}
          value={settings.extraInstructions}
          onChangeText={(extraInstructions) => update({ extraInstructions })}
        />
      </Section>

      <Section title="Privacy">
        <Text style={styles.body}>
          Damien has no servers. The model, your notes, reminders and task history live only on
          this device. Web fetch and HTTP tools reach the internet only when a task asks for it.
        </Text>
      </Section>

      <Section title="About">
        <Text style={styles.body}>
          Damien v0.1.0 — open source under the MIT License. On-device inference by llama.cpp via
          llama.rn. Models from Hugging Face retain their own licenses (see Model Setup).
        </Text>
        <Text style={styles.repo}>github.com/DamienRoy550/Damien</Text>
      </Section>
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </View>
  );
}

function ToggleRow({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <View style={styles.row}>
      <View style={styles.rowText}>
        <Text style={styles.rowLabel}>{label}</Text>
        <Text style={styles.hint}>{hint}</Text>
      </View>
      <Switch
        value={value}
        onValueChange={onChange}
        trackColor={{ false: theme.border, true: theme.accent }}
        thumbColor="#fff"
      />
    </View>
  );
}

function Stepper({
  label,
  hint,
  value,
  step,
  min,
  max,
  format,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  step: number;
  min: number;
  max: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, Number(v.toFixed(2))));
  return (
    <View style={styles.row}>
      <View style={styles.rowText}>
        <Text style={styles.rowLabel}>{label}</Text>
        {hint ? <Text style={styles.hint}>{hint}</Text> : null}
      </View>
      <View style={styles.stepper}>
        <Pressable style={styles.stepBtn} onPress={() => onChange(clamp(value - step))}>
          <Text style={styles.stepBtnTxt}>−</Text>
        </Pressable>
        <Text style={styles.stepValue}>{format ? format(value) : String(value)}</Text>
        <Pressable style={styles.stepBtn} onPress={() => onChange(clamp(value + step))}>
          <Text style={styles.stepBtnTxt}>+</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  content: { padding: 14, gap: 16, paddingBottom: 40 },
  section: {
    backgroundColor: theme.surface,
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: theme.border,
    padding: 14,
    gap: 12,
  },
  sectionTitle: { color: theme.text, fontSize: 15, fontWeight: '800' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  rowText: { flex: 1, gap: 3 },
  rowLabel: { color: theme.text, fontSize: 14, fontWeight: '600' },
  hint: { color: theme.faint, fontSize: 12, lineHeight: 16 },
  stepper: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    backgroundColor: theme.surfaceAlt,
    borderRadius: 10,
    paddingHorizontal: 8,
    paddingVertical: 6,
  },
  stepBtn: { width: 28, height: 28, alignItems: 'center', justifyContent: 'center' },
  stepBtnTxt: { color: theme.accent, fontSize: 20, fontWeight: '800' },
  stepValue: { color: theme.text, fontSize: 14, fontWeight: '700', minWidth: 42, textAlign: 'center' },
  multiline: {
    backgroundColor: theme.surfaceAlt,
    borderRadius: 12,
    color: theme.text,
    fontSize: 14,
    padding: 12,
    minHeight: 90,
    textAlignVertical: 'top',
  },
  body: { color: theme.dim, fontSize: 13, lineHeight: 19 },
  repo: { color: theme.accent, fontSize: 13, fontWeight: '600' },
});
