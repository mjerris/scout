import type { ReactNode } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import type { TextInputProps } from 'react-native';

import { useTheme } from './theme';

export function Section({ title, children }: { title?: string; children: ReactNode }) {
  const t = useTheme();
  return (
    <View style={[styles.section, { backgroundColor: t.panel, borderColor: t.line }]}>
      {title ? <Text style={[styles.h2, { color: t.ink }]}>{title}</Text> : null}
      {children}
    </View>
  );
}

type Kind = 'plain' | 'go' | 'stop' | 'on';

export function Button(props: {
  title: string;
  onPress: () => void;
  kind?: Kind;
  disabled?: boolean;
  testID?: string;
  big?: boolean;
  accessibilityLabel?: string;
}) {
  const t = useTheme();
  const kind = props.kind ?? 'plain';
  const fill = kind === 'go' ? t.ok : kind === 'stop' ? t.bad : kind === 'on' ? t.accent : t.panel;
  const ink = kind === 'plain' ? t.ink : '#fff';
  return (
    <Pressable
      testID={props.testID}
      accessibilityRole="button"
      accessibilityLabel={props.accessibilityLabel ?? props.title}
      accessibilityState={{ disabled: !!props.disabled, selected: kind === 'on' }}
      disabled={props.disabled}
      onPress={props.onPress}
      style={({ pressed }) => [
        styles.button,
        props.big && styles.big,
        { backgroundColor: fill, borderColor: kind === 'plain' ? t.line : fill },
        (pressed || props.disabled) && { opacity: props.disabled ? 0.45 : 0.75 },
      ]}
    >
      <Text style={[styles.buttonText, props.big && styles.bigText, { color: ink }]}>{props.title}</Text>
    </Pressable>
  );
}

/**
 * A labelled text field. Uncontrolled (give it `defaultValue`, read `onChangeText`): on
 * the New Architecture a controlled field's JS echo of each change can drop keys typed
 * while it is applied (react-native#58904), and nothing here rewrites what is typed.
 */
export function Field(props: Omit<TextInputProps, 'value'> & { label: string }) {
  const t = useTheme();
  const { label, style, ...rest } = props;
  return (
    <View style={styles.field}>
      <Text style={[styles.label, { color: t.muted }]}>{label}</Text>
      <TextInput
        placeholderTextColor={t.muted}
        autoCapitalize="none"
        autoCorrect={false}
        {...rest}
        style={[styles.input, { color: t.ink, borderColor: t.line, backgroundColor: t.bg }, style]}
      />
    </View>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  const t = useTheme();
  return <Text style={[styles.muted, { color: t.muted }]}>{children}</Text>;
}

export function ErrorText({ children }: { children: ReactNode }) {
  const t = useTheme();
  return children ? (
    <Text testID="error" style={[styles.error, { color: t.bad }]}>
      {children}
    </Text>
  ) : null;
}

export function Row({ children }: { children: ReactNode }) {
  return <View style={styles.row}>{children}</View>;
}

const styles = StyleSheet.create({
  section: { borderWidth: StyleSheet.hairlineWidth, borderRadius: 12, padding: 14, gap: 10 },
  h2: { fontSize: 15, fontWeight: '600' },
  button: {
    flex: 1,
    borderWidth: 1,
    borderRadius: 10,
    paddingVertical: 12,
    paddingHorizontal: 14,
    alignItems: 'center',
  },
  big: { paddingVertical: 18 },
  buttonText: { fontSize: 16, fontWeight: '500' },
  bigText: { fontSize: 19, fontWeight: '600' },
  field: { gap: 4 },
  label: { fontSize: 13 },
  input: { fontSize: 16, borderWidth: 1, borderRadius: 8, paddingHorizontal: 10, paddingVertical: 9 },
  muted: { fontSize: 13, lineHeight: 18 },
  error: { fontSize: 14 },
  row: { flexDirection: 'row', gap: 8, alignItems: 'center' },
});
