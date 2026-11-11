// Shared building blocks for every screen, styled from the app palette.
import { useState } from "react";
import type { ReactNode } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import type { StyleProp, TextInputProps, TextStyle, ViewStyle } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useLiveState } from "@/live/live";
import { STATUS_LABELS, usePalette } from "@/ui/theme";
import { errorText } from "@/ui/format";

/**
 * Pull-to-refresh state for a manual refresh only: background refetches
 * (polling, live updates) must not show the spinner.
 */
export function usePullRefresh(refresh: () => Promise<unknown> | unknown) {
  const [refreshing, setRefreshing] = useState(false);
  return {
    refreshing,
    onRefresh: async () => {
      setRefreshing(true);
      try {
        await refresh();
      } finally {
        setRefreshing(false);
      }
    },
  };
}

export function Screen({
  children,
  refreshing,
  onRefresh,
  padded = true,
}: {
  children: ReactNode;
  refreshing?: boolean;
  onRefresh?: () => void;
  padded?: boolean;
}) {
  const palette = usePalette();
  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: palette.bg }}
      contentContainerStyle={padded ? styles.screen : undefined}
      keyboardShouldPersistTaps="handled"
      contentInsetAdjustmentBehavior="automatic"
      refreshControl={
        onRefresh ? (
          <RefreshControl
            refreshing={!!refreshing}
            onRefresh={onRefresh}
            tintColor={palette.accent}
          />
        ) : undefined
      }
    >
      {children}
    </ScrollView>
  );
}

export function Panel({
  children,
  style,
  onPress,
  accessibilityLabel,
}: {
  children: ReactNode;
  style?: StyleProp<ViewStyle>;
  onPress?: () => void;
  accessibilityLabel?: string;
}) {
  const palette = usePalette();
  const base = [styles.panel, { backgroundColor: palette.panel, borderColor: palette.line }, style];
  if (!onPress) return <View style={base}>{children}</View>;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      onPress={onPress}
      style={({ pressed }) => [base, { opacity: pressed ? 0.75 : 1 }]}
    >
      {children}
    </Pressable>
  );
}

type Tone = "text" | "secondary" | "muted" | "accent" | "danger";

export function Label({
  children,
  tone = "text",
  size = 15,
  bold = false,
  mono = false,
  style,
  numberOfLines,
}: {
  children: ReactNode;
  tone?: Tone;
  size?: number;
  bold?: boolean;
  mono?: boolean;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
}) {
  const palette = usePalette();
  return (
    <Text
      numberOfLines={numberOfLines}
      style={[
        {
          color: palette[tone],
          fontSize: size,
          fontWeight: bold ? "600" : "400",
          fontFamily: mono ? (Platform.OS === "ios" ? "Menlo" : "monospace") : undefined,
        },
        style,
      ]}
    >
      {children}
    </Text>
  );
}

export function Heading({ children, eyebrow }: { children: ReactNode; eyebrow?: string }) {
  return (
    <View style={{ gap: 2 }}>
      {eyebrow ? (
        <Label tone="muted" size={12} bold>
          {eyebrow.toUpperCase()}
        </Label>
      ) : null}
      <Label size={20} bold>
        {children}
      </Label>
    </View>
  );
}

export function Row({
  children,
  gap = 8,
  wrap = true,
  style,
}: {
  children: ReactNode;
  gap?: number;
  wrap?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <View
      style={[
        { flexDirection: "row", alignItems: "center", flexWrap: wrap ? "wrap" : "nowrap", gap },
        style,
      ]}
    >
      {children}
    </View>
  );
}

export function Button({
  label,
  onPress,
  disabled,
  kind = "secondary",
  small = false,
  accessibilityLabel,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  kind?: "primary" | "secondary" | "danger" | "link";
  small?: boolean;
  accessibilityLabel?: string;
}) {
  const palette = usePalette();
  const background =
    kind === "primary" ? palette.accent : kind === "link" ? "transparent" : palette.raised;
  const color =
    kind === "primary"
      ? palette.onAccent
      : kind === "danger"
        ? palette.danger
        : kind === "link"
          ? palette.accent
          : palette.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled: !!disabled }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={6}
      style={({ pressed }) => [
        styles.button,
        small && styles.small,
        kind === "link" && { paddingHorizontal: 0, borderWidth: 0 },
        { backgroundColor: background, borderColor: palette.line },
        { opacity: disabled ? 0.45 : pressed ? 0.75 : 1 },
      ]}
    >
      <Text style={{ color, fontWeight: "600", fontSize: small ? 13 : 15 }}>{label}</Text>
    </Pressable>
  );
}

export function Field({
  label,
  hint,
  multiline,
  style,
  ...props
}: TextInputProps & { label: string; hint?: string }) {
  const palette = usePalette();
  return (
    <View style={{ gap: 6 }}>
      <Label tone="secondary" size={13}>
        {label}
      </Label>
      <TextInput
        accessibilityLabel={label}
        placeholderTextColor={palette.muted}
        multiline={multiline}
        style={[
          styles.input,
          multiline && { minHeight: 96, textAlignVertical: "top" },
          { color: palette.text, borderColor: palette.line, backgroundColor: palette.bg },
          style,
        ]}
        {...props}
      />
      {hint ? (
        <Label tone="muted" size={12}>
          {hint}
        </Label>
      ) : null}
    </View>
  );
}

export interface Option<T extends string> {
  value: T;
  label: string;
}

/** A set of chips: one selected value (or several with `multiple`). */
export function Choice<T extends string>({
  label,
  options,
  value,
  onChange,
  disabled,
}: {
  label?: string;
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  const palette = usePalette();
  return (
    <View style={{ gap: 6 }}>
      {label ? (
        <Label tone="secondary" size={13}>
          {label}
        </Label>
      ) : null}
      <Row gap={6}>
        {options.map((option) => {
          const selected = option.value === value;
          return (
            <Pressable
              key={option.value || "__all__"}
              accessibilityRole="radio"
              accessibilityLabel={label ? `${label}: ${option.label}` : option.label}
              accessibilityState={{ checked: selected, disabled: !!disabled }}
              disabled={disabled}
              onPress={() => onChange(option.value)}
              style={[
                styles.chip,
                {
                  borderColor: selected ? palette.accent : palette.line,
                  backgroundColor: selected ? palette.raised : "transparent",
                  opacity: disabled ? 0.5 : 1,
                },
              ]}
            >
              <Text style={{ color: selected ? palette.accent : palette.text, fontSize: 13 }}>
                {option.label}
              </Text>
            </Pressable>
          );
        })}
      </Row>
    </View>
  );
}

export function Toggle({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  const palette = usePalette();
  return (
    <Pressable
      accessibilityRole="checkbox"
      accessibilityLabel={label}
      accessibilityState={{ checked: value, disabled: !!disabled }}
      disabled={disabled}
      onPress={() => onChange(!value)}
      style={{ flexDirection: "row", alignItems: "center", gap: 10, opacity: disabled ? 0.5 : 1 }}
    >
      <View
        style={[
          styles.box,
          {
            borderColor: value ? palette.accent : palette.line,
            backgroundColor: value ? palette.accent : "transparent",
          },
        ]}
      >
        {value ? (
          <Text style={{ color: palette.onAccent, fontSize: 13, fontWeight: "700" }}>✓</Text>
        ) : null}
      </View>
      <Label style={{ flexShrink: 1 }}>{label}</Label>
    </Pressable>
  );
}

const STATUS_TONES: Record<string, Tone> = {
  pending: "secondary",
  in_progress: "accent",
  blocked: "danger",
  review: "accent",
  done: "muted",
  cancelled: "muted",
};

export function Badge({ status, label }: { status: string; label?: string }) {
  const palette = usePalette();
  return (
    <View style={[styles.badge, { borderColor: palette.line, backgroundColor: palette.raised }]}>
      <Label size={12} bold tone={STATUS_TONES[status] ?? "secondary"}>
        {label ?? STATUS_LABELS[status] ?? status.replaceAll("_", " ")}
      </Label>
    </View>
  );
}

export function Avatar({ name }: { name: string }) {
  const palette = usePalette();
  return (
    <View style={[styles.avatar, { backgroundColor: palette.raised, borderColor: palette.line }]}>
      <Label size={12} bold tone="accent">
        {name.slice(0, 2).toUpperCase()}
      </Label>
    </View>
  );
}

export function Property({ label, children }: { label: string; children: ReactNode }) {
  return (
    <View style={{ gap: 2 }}>
      <Label tone="muted" size={12}>
        {label}
      </Label>
      {typeof children === "string" || typeof children === "number" ? (
        <Label>{children}</Label>
      ) : (
        children
      )}
    </View>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  const palette = usePalette();
  return (
    <View style={styles.center} accessibilityLabel={label}>
      <ActivityIndicator color={palette.accent} />
      <Label tone="muted" size={13}>
        {label}
      </Label>
    </View>
  );
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  return (
    <View style={styles.center}>
      <Label tone="secondary" bold>
        {title}
      </Label>
      {detail ? (
        <Label tone="muted" size={13} style={{ textAlign: "center" }}>
          {detail}
        </Label>
      ) : null}
    </View>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <View style={styles.center}>
      <Label tone="danger" style={{ textAlign: "center" }}>
        {errorText(error, "Could not load this")}
      </Label>
      {retry ? <Button label="Try again" onPress={retry} small /> : null}
    </View>
  );
}

export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  const palette = usePalette();
  return (
    <View
      accessibilityRole={error ? "alert" : "text"}
      style={[
        styles.notice,
        { borderColor: error ? palette.danger : palette.line, backgroundColor: palette.raised },
      ]}
    >
      <Label tone={error ? "danger" : "secondary"} size={13}>
        {children}
      </Label>
    </View>
  );
}

export function LiveStatus({ suffix }: { suffix?: string }) {
  const state = useLiveState();
  return (
    <Label tone="secondary" size={13}>
      {state === "live" ? "● Live" : state === "connecting" ? "Connecting…" : "○ Offline"}
      {suffix ? ` · ${suffix}` : ""}
    </Label>
  );
}

/** A full-height modal sheet with a title bar, for forms and pickers. */
export function Sheet({
  visible,
  title,
  onClose,
  children,
}: {
  visible: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const palette = usePalette();
  const insets = useSafeAreaInsets();
  // Mount the native modal only while open: hidden modals (and pickers nested
  // inside a sheet) otherwise stay attached and stall Android's UI thread.
  if (!visible) return null;
  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onClose}
    >
      <KeyboardAvoidingView
        style={{ flex: 1, backgroundColor: palette.bg }}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <View
          style={[
            styles.sheetBar,
            {
              borderColor: palette.line,
              backgroundColor: palette.panel,
              // The modal already starts below the status bar on Android.
              paddingTop: 14,
            },
          ]}
        >
          <Label size={17} bold style={{ flex: 1 }} numberOfLines={1}>
            {title}
          </Label>
          <Button label="Close" kind="link" onPress={onClose} />
        </View>
        <ScrollView
          contentContainerStyle={[styles.screen, { paddingBottom: insets.bottom + 24 }]}
          keyboardShouldPersistTaps="handled"
        >
          {children}
        </ScrollView>
      </KeyboardAvoidingView>
    </Modal>
  );
}

/** Pick one value from a long list in a sheet (projects, workers, models). */
export function Picker<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
  placeholder = "Choose…",
}: {
  label: string;
  value: T;
  options: Option<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const palette = usePalette();
  const current = options.find((option) => option.value === value);
  return (
    <PickerSheet label={label} options={options} onChange={onChange}>
      {(open) => (
        <View style={{ gap: 6 }}>
          <Label tone="secondary" size={13}>
            {label}
          </Label>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`${label}: ${current?.label ?? placeholder}`}
            disabled={disabled}
            onPress={open}
            style={[
              styles.input,
              {
                borderColor: palette.line,
                backgroundColor: palette.bg,
                opacity: disabled ? 0.5 : 1,
              },
            ]}
          >
            <Text style={{ color: current ? palette.text : palette.muted, fontSize: 16 }}>
              {current?.label ?? placeholder} ▾
            </Text>
          </Pressable>
        </View>
      )}
    </PickerSheet>
  );
}

function PickerSheet<T extends string>({
  label,
  options,
  onChange,
  children,
}: {
  label: string;
  options: Option<T>[];
  onChange: (value: T) => void;
  children: (open: () => void) => ReactNode;
}) {
  const [visible, setVisible] = useState(false);
  const palette = usePalette();
  return (
    <>
      {children(() => setVisible(true))}
      <Sheet visible={visible} title={label} onClose={() => setVisible(false)}>
        {options.map((option) => (
          <Pressable
            key={option.value || "__none__"}
            accessibilityRole="button"
            onPress={() => {
              onChange(option.value);
              setVisible(false);
            }}
            style={[styles.option, { borderColor: palette.line }]}
          >
            <Label>{option.label}</Label>
          </Pressable>
        ))}
      </Sheet>
    </>
  );
}

export const styles = StyleSheet.create({
  screen: { padding: 16, gap: 12 },
  panel: { borderWidth: 1, borderRadius: 10, padding: 14, gap: 8 },
  button: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  small: { paddingHorizontal: 10, paddingVertical: 6 },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 16,
  },
  chip: { borderWidth: 1, borderRadius: 16, paddingHorizontal: 12, paddingVertical: 6 },
  badge: {
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 8,
    paddingVertical: 2,
    alignSelf: "flex-start",
  },
  avatar: {
    width: 26,
    height: 26,
    borderRadius: 13,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  box: {
    width: 22,
    height: 22,
    borderRadius: 5,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  center: { alignItems: "center", justifyContent: "center", gap: 8, paddingVertical: 32 },
  notice: { borderWidth: 1, borderRadius: 8, padding: 10 },
  sheetBar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 16,
    paddingBottom: 12,
    borderBottomWidth: 1,
  },
  option: { paddingVertical: 14, borderBottomWidth: StyleSheet.hairlineWidth },
});
