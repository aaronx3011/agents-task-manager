import { Stack } from "expo-router";
import { Empty, Screen } from "@/ui/kit";

export default function ActivityPlaceholder() {
  return (
    <Screen>
      <Stack.Screen options={{ title: "Activity" }} />
      <Empty title="Activity is on its way" detail="This screen lands with issue #80." />
    </Screen>
  );
}
