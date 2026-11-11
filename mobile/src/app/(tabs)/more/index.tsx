import { router, Stack } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { Avatar, Button, Label, LiveStatus, Panel, Row, Screen } from "@/ui/kit";

type Destination = { href: Parameters<typeof router.push>[0]; title: string; detail: string };

export default function More() {
  const { api, server, signOut } = useSession();
  const { username, role, canManage } = useAccess();
  const pending = useQuery({
    queryKey: ["interactions"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/interactions")),
  });
  const waiting = pending.data?.items.length ?? 0;
  const destinations: Destination[] = [
    {
      href: "/more/approvals",
      title: "Approvals",
      detail: waiting ? `${waiting} waiting for an answer` : "Permission and question requests",
    },
  ];
  if (canManage)
    destinations.push({
      href: "/more/configuration",
      title: "Configuration",
      detail: "Register agents and connect them over MCP",
    });
  return (
    <Screen>
      <Stack.Screen options={{ title: "More" }} />
      <Panel>
        <Row>
          <Avatar name={username || "?"} />
          <Label size={16} bold>
            {username}
          </Label>
          <Label tone="muted">{role}</Label>
        </Row>
        <Label tone="muted" size={13}>
          {server}
        </Label>
        <LiveStatus />
      </Panel>
      {destinations.map((destination) => (
        <Panel
          key={destination.title}
          accessibilityLabel={destination.title}
          onPress={() => router.push(destination.href)}
        >
          <Label size={16} bold>
            {destination.title}
          </Label>
          <Label tone="secondary" size={13}>
            {destination.detail}
          </Label>
        </Panel>
      ))}
      <Button
        label="Sign out"
        kind="danger"
        onPress={async () => {
          await signOut();
          router.replace("/login");
        }}
      />
    </Screen>
  );
}
