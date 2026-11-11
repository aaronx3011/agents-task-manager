import { Stack } from "expo-router";
import { Empty, Screen } from "@/ui/kit";

export default function SessionsPlaceholder() {
  return (
    <Screen>
      <Stack.Screen options={{ title: "Sessions" }} />
      <Empty title="Sessions is on its way" detail="This screen lands with issue #78." />
    </Screen>
  );
}
