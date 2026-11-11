import { useState } from "react";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SessionProvider } from "@/auth/session";
import { LiveProvider } from "@/live/live";
import { usePalette } from "@/ui/theme";

export default function RootLayout() {
  const palette = usePalette();
  // Live `changed` notices invalidate queries, so cached data stays fresh
  // without aggressive polling.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: 10_000, retry: 1 } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <SessionProvider>
        <LiveProvider>
          <StatusBar style="auto" />
          <Stack
            screenOptions={{
              headerShown: false,
              contentStyle: { backgroundColor: palette.bg },
            }}
          >
            <Stack.Screen name="index" />
            <Stack.Screen name="login" />
            <Stack.Screen name="(tabs)" />
          </Stack>
        </LiveProvider>
      </SessionProvider>
    </QueryClientProvider>
  );
}
