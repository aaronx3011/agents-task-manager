import { useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { router } from "expo-router";
import { DEFAULT_SERVER, useSession } from "@/auth/session";
import { usePalette } from "@/ui/theme";

export default function Login() {
  const { signIn, server: savedServer } = useSession();
  const palette = usePalette();
  const [server, setServer] = useState(savedServer || DEFAULT_SERVER);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await signIn(server, username.trim(), password);
      setPassword("");
      router.replace("/tasks");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not sign in");
    } finally {
      setBusy(false);
    }
  }

  const input = [styles.input, { borderColor: palette.line, color: palette.text }];
  return (
    <KeyboardAvoidingView
      style={[styles.screen, { backgroundColor: palette.bg }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <View style={[styles.card, { backgroundColor: palette.panel, borderColor: palette.line }]}>
        <Text style={[styles.brand, { color: palette.text }]}>agentops.</Text>
        <Text style={[styles.lead, { color: palette.secondary }]}>
          Sign in with the account an owner invited you to. This device gets its own token, which
          you can revoke from the web app.
        </Text>
        <Text style={[styles.label, { color: palette.secondary }]}>Server</Text>
        <TextInput
          accessibilityLabel="Server address"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          placeholder="https://coordinator.example.com"
          placeholderTextColor={palette.muted}
          style={input}
          value={server}
          onChangeText={setServer}
        />
        <Text style={[styles.label, { color: palette.secondary }]}>Username</Text>
        <TextInput
          accessibilityLabel="Username"
          autoCapitalize="none"
          autoCorrect={false}
          autoComplete="username"
          textContentType="username"
          style={input}
          value={username}
          onChangeText={setUsername}
        />
        <Text style={[styles.label, { color: palette.secondary }]}>Password</Text>
        <TextInput
          accessibilityLabel="Password"
          secureTextEntry
          autoComplete="current-password"
          textContentType="password"
          style={input}
          value={password}
          onChangeText={setPassword}
          onSubmitEditing={submit}
        />
        {error ? (
          <Text accessibilityRole="alert" style={[styles.error, { color: palette.danger }]}>
            {error}
          </Text>
        ) : null}
        <Pressable
          accessibilityRole="button"
          disabled={busy || !username || !password}
          onPress={submit}
          style={({ pressed }) => [
            styles.button,
            {
              backgroundColor: palette.accent,
              opacity: busy || !username || !password ? 0.5 : pressed ? 0.8 : 1,
            },
          ]}
        >
          {busy ? (
            <ActivityIndicator color={palette.onAccent} />
          ) : (
            <Text style={[styles.buttonText, { color: palette.onAccent }]}>Sign in</Text>
          )}
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, justifyContent: "center", padding: 20 },
  card: { borderWidth: 1, borderRadius: 14, padding: 22, gap: 8 },
  brand: { fontSize: 24, fontWeight: "700", marginBottom: 4 },
  lead: { lineHeight: 20, marginBottom: 12 },
  label: { fontSize: 13, marginTop: 6 },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 11,
    fontSize: 16,
  },
  error: { marginTop: 8 },
  button: { marginTop: 16, borderRadius: 8, paddingVertical: 13, alignItems: "center" },
  buttonText: { fontSize: 16, fontWeight: "600" },
});
