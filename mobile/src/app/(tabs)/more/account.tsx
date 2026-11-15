// Account (#81): change your password and manage the devices signed in with
// the mobile app, like the web Account page.
import { useState } from "react";
import { Stack } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import {
  Button,
  Empty,
  Field,
  Heading,
  Label,
  Loading,
  Notice,
  Panel,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";
import { age, errorText } from "@/ui/format";

const ROLE_LABELS: Record<string, string> = { owner: "Owner", member: "Member", viewer: "Viewer" };

function ChangePassword() {
  const { api } = useSession();
  const [form, setForm] = useState({ current: "", next: "", confirm: "" });
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (!api) return;
    setDone(false);
    if (form.next !== form.confirm) {
      setError("The new passwords do not match.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      await unwrap(
        api.POST("/api/v1/auth/password", {
          body: { current_password: form.current, new_password: form.next },
        }),
      );
      setForm({ current: "", next: "", confirm: "" });
      setDone(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const field = (
    key: keyof typeof form,
    label: string,
    auto: "current-password" | "new-password",
  ) => (
    <Field
      label={label}
      secureTextEntry
      autoComplete={auto}
      textContentType={auto === "current-password" ? "password" : "newPassword"}
      autoCapitalize="none"
      value={form[key]}
      onChangeText={(value) => setForm({ ...form, [key]: value })}
    />
  );
  return (
    <Panel>
      <Label size={16} bold>
        Password
      </Label>
      <Label tone="secondary" size={13}>
        Other browsers signed in to your account will be signed out.
      </Label>
      {field("current", "Current password", "current-password")}
      {field("next", "New password", "new-password")}
      {field("confirm", "Confirm new password", "new-password")}
      {error ? <Notice error>{error}</Notice> : null}
      {done ? (
        <Label tone="accent" size={13}>
          Password changed.
        </Label>
      ) : null}
      <Button
        kind="primary"
        label="Change password"
        disabled={busy || !form.current || !form.next || !form.confirm}
        onPress={() => void submit()}
      />
    </Panel>
  );
}

function Devices() {
  const { api, expire } = useSession();
  const client = useQueryClient();
  const tokens = useQuery({
    queryKey: ["tokens"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/auth/tokens")),
  });
  const [error, setError] = useState("");
  async function revoke(id: number, current: boolean) {
    if (!api) return;
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/auth/tokens/{id}/revoke", { params: { path: { id: String(id) } } }),
      );
      if (current) {
        // This phone's own token: nothing left to call the server with.
        await expire();
        return;
      }
      void client.invalidateQueries({ queryKey: ["tokens"] });
    } catch (e) {
      setError(errorText(e));
    }
  }
  const live = tokens.data?.filter((token) => !token.revoked_at) ?? [];
  return (
    <Panel>
      <Label size={16} bold>
        Mobile devices
      </Label>
      <Label tone="secondary" size={13}>
        Devices signed in with the mobile app. Revoking signs them out.
      </Label>
      {error ? <Notice error>{error}</Notice> : null}
      {tokens.isLoading ? (
        <Loading label="Loading devices…" />
      ) : !live.length ? (
        <Empty title="No devices" detail="Sign in from the mobile app to add one." />
      ) : (
        live.map((token) => (
          <Row key={token.id} style={{ justifyContent: "space-between" }} wrap={false}>
            <Label style={{ flex: 1 }}>
              <Label bold>{token.name}</Label>
              {token.current ? " · this device" : ""}
              {"\n"}
              <Label tone="muted" size={12}>
                {token.last_used_at ? `used ${age(token.last_used_at)}` : "never used"}
              </Label>
            </Label>
            <Button
              small
              kind="danger"
              label={token.current ? "Sign out" : "Revoke"}
              accessibilityLabel={`Revoke ${token.name}`}
              onPress={() => void revoke(token.id, token.current)}
            />
          </Row>
        ))
      )}
    </Panel>
  );
}

export default function Account() {
  const { username, role } = useAccess();
  const client = useQueryClient();
  const pull = usePullRefresh(() => client.invalidateQueries({ queryKey: ["tokens"] }));
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Account" }} />
      <Heading eyebrow={ROLE_LABELS[role ?? ""] ?? "Account"}>{username}</Heading>
      <ChangePassword />
      <Devices />
    </Screen>
  );
}
