import { Text } from "react-native";
import type { ColorValue } from "react-native";
import { Redirect, Tabs } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useSession } from "@/auth/session";
import { SessionLibraryProvider } from "@/sessions/library";
import { usePalette } from "@/ui/theme";

function Glyph({ symbol, color }: { symbol: string; color: ColorValue }) {
  return <Text style={{ color, fontSize: 20, lineHeight: 24 }}>{symbol}</Text>;
}

export default function TabsLayout() {
  const { ready, user, api } = useSession();
  const palette = usePalette();
  // Pending approvals badge on More, like the web's attention counts.
  const pending = useQuery({
    queryKey: ["interactions"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/interactions")),
    refetchInterval: 30_000,
  });
  if (ready && !user) return <Redirect href="/login" />;
  const waiting = pending.data?.items.length ?? 0;
  return (
    <SessionLibraryProvider>
      <Tabs
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: palette.accent,
          tabBarInactiveTintColor: palette.muted,
          tabBarStyle: { backgroundColor: palette.panel, borderTopColor: palette.line },
        }}
      >
        <Tabs.Screen
          name="tasks"
          options={{
            title: "Tasks",
            tabBarIcon: ({ color }) => <Glyph symbol="☑" color={color} />,
          }}
        />
        <Tabs.Screen
          name="planning"
          options={{
            title: "Planning",
            tabBarIcon: ({ color }) => <Glyph symbol="▦" color={color} />,
          }}
        />
        <Tabs.Screen
          name="sessions"
          options={{
            title: "Sessions",
            tabBarIcon: ({ color }) => <Glyph symbol="◉" color={color} />,
          }}
        />
        <Tabs.Screen
          name="activity"
          options={{
            title: "Activity",
            tabBarIcon: ({ color }) => <Glyph symbol="≋" color={color} />,
          }}
        />
        <Tabs.Screen
          name="more"
          options={{
            title: "More",
            tabBarIcon: ({ color }) => <Glyph symbol="⋯" color={color} />,
            tabBarBadge: waiting ? waiting : undefined,
          }}
        />
      </Tabs>
    </SessionLibraryProvider>
  );
}
