import { Stack } from "expo-router";
import { usePalette } from "@/ui/theme";

/** The Stack inside each tab, themed alike on iOS and Android. */
export function TabStack() {
  const palette = usePalette();
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: palette.panel },
        headerTintColor: palette.text,
        headerShadowVisible: false,
        // Android left-aligns titles by default, which collided with header buttons.
        headerTitleAlign: "center",
        contentStyle: { backgroundColor: palette.bg },
      }}
    />
  );
}
