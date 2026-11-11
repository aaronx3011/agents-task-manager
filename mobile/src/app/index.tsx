import { ActivityIndicator, View } from "react-native";
import { Redirect } from "expo-router";
import { useSession } from "@/auth/session";
import { usePalette } from "@/ui/theme";

export default function Start() {
  const { ready, user } = useSession();
  const palette = usePalette();
  if (!ready)
    return (
      <View style={{ flex: 1, justifyContent: "center", backgroundColor: palette.bg }}>
        <ActivityIndicator color={palette.accent} />
      </View>
    );
  return <Redirect href={user ? "/tasks" : "/login"} />;
}
