import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SessionProvider } from "@/auth/session";
import { usePalette } from "@/ui/theme";

export default function RootLayout() {
  const palette = usePalette();
  return (
    <SessionProvider>
      <StatusBar style="auto" />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: palette.panel },
          headerTintColor: palette.text,
          contentStyle: { backgroundColor: palette.bg },
        }}
      >
        <Stack.Screen name="index" options={{ headerShown: false }} />
        <Stack.Screen name="login" options={{ title: "Sign in", headerShown: false }} />
        <Stack.Screen name="tasks/index" options={{ title: "Tasks" }} />
        <Stack.Screen name="tasks/[id]" options={{ title: "Task" }} />
        <Stack.Screen name="approvals" options={{ title: "Approvals" }} />
      </Stack>
    </SessionProvider>
  );
}
