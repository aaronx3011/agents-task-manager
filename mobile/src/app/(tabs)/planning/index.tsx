import { Stack } from "expo-router";
import { Empty, Screen } from "@/ui/kit";

export default function PlanningPlaceholder() {
  return (
    <Screen>
      <Stack.Screen options={{ title: "Planning" }} />
      <Empty title="Planning is on its way" detail="This screen lands with issue #79." />
    </Screen>
  );
}
