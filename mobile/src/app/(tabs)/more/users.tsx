// Users & invites (#81, owners): people with their role and status, owner
// password resets, and single-use invite links shared from the phone, like the
// web Users & invites page.
import { useState } from "react";
import { Share } from "react-native";
import { Stack } from "expo-router";
import * as Clipboard from "expo-clipboard";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { Schemas } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import {
  Button,
  Choice,
  Empty,
  ErrorState,
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
import { age, errorText, relative } from "@/ui/format";

type Role = "viewer" | "member" | "owner";
const ROLES: { value: Role; label: string }[] = [
  { value: "viewer", label: "Viewer" },
  { value: "member", label: "Member" },
  { value: "owner", label: "Owner" },
];
const ROLE_LABELS: Record<string, string> = { owner: "Owner", member: "Member", viewer: "Viewer" };
const LIFETIMES = [
  { value: "24", label: "1 day" },
  { value: "72", label: "3 days" },
  { value: "168", label: "7 days" },
  { value: "720", label: "30 days" },
];

function UserRow({ user, self }: { user: Schemas["User"]; self: boolean }) {
  const { api } = useSession();
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [password, setPassword] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  async function update(body: { role?: Role; is_active?: boolean }) {
    if (!api) return;
    setError("");
    setSaved("");
    try {
      await unwrap(api.PATCH("/api/v1/users/{id}", { params: { path: { id: user.id } }, body }));
      void client.invalidateQueries({ queryKey: ["users"] });
      if (self) void client.invalidateQueries({ queryKey: ["me"] });
    } catch (e) {
      setError(errorText(e));
    }
  }
  async function savePassword() {
    if (!api) return;
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/users/{id}/password", {
          params: { path: { id: user.id } },
          body: { password: password || "" },
        }),
      );
      setPassword(null);
      setSaved("Password updated; their sessions and devices were signed out.");
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <Panel>
      <Row style={{ justifyContent: "space-between" }}>
        <Label bold tone={user.is_active ? "text" : "muted"}>
          {user.username}
          {self ? " (you)" : ""}
        </Label>
        <Label tone={user.is_active ? "accent" : "muted"} size={13}>
          {user.is_active ? "Active" : "Deactivated"}
        </Label>
      </Row>
      {user.email ? (
        <Label tone="muted" size={12}>
          {user.email}
        </Label>
      ) : null}
      <Label tone="muted" size={12}>
        Last sign-in: {user.last_login ? age(user.last_login) : "Never"}
      </Label>
      <Choice<Role>
        label={`Role for ${user.username}`}
        value={user.role as Role}
        options={ROLES}
        onChange={(role) => role !== user.role && void update({ role })}
      />
      <Row>
        <Button
          small
          kind={user.is_active ? "danger" : "secondary"}
          label={user.is_active ? "Deactivate" : "Reactivate"}
          onPress={() => void update({ is_active: !user.is_active })}
        />
        <Button
          small
          label="Set password"
          onPress={() => setPassword(password === null ? "" : null)}
        />
      </Row>
      {password !== null ? (
        <>
          <Field
            label={`New password for ${user.username}`}
            secureTextEntry
            autoComplete="new-password"
            textContentType="newPassword"
            autoCapitalize="none"
            value={password}
            onChangeText={setPassword}
          />
          <Row>
            <Button
              small
              kind="primary"
              label="Save password"
              disabled={!password}
              onPress={() => void savePassword()}
            />
            <Button small label="Cancel" onPress={() => setPassword(null)} />
          </Row>
        </>
      ) : null}
      {error ? <Notice error>{error}</Notice> : null}
      {saved ? (
        <Label tone="accent" size={13}>
          {saved}
        </Label>
      ) : null}
    </Panel>
  );
}

function Invites() {
  const { api, server } = useSession();
  const client = useQueryClient();
  const invites = useQuery({
    queryKey: ["invites"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/invites")),
  });
  const [role, setRole] = useState<Role>("member");
  const [hours, setHours] = useState("72");
  const [note, setNote] = useState("");
  const [created, setCreated] = useState<Schemas["CreatedInvite"] | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  // The server may answer with a path; links are shared outside the app.
  const link = created
    ? created.url.startsWith("/")
      ? `${server}${created.url}`
      : created.url
    : "";
  async function create() {
    if (!api) return;
    setBusy(true);
    setError("");
    setCopied(false);
    try {
      const result = await unwrap(
        api.POST("/api/v1/invites", { body: { role, expires_in_hours: Number(hours), note } }),
      );
      setCreated(result);
      setNote("");
      void client.invalidateQueries({ queryKey: ["invites"] });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: number) {
    if (!api) return;
    setError("");
    try {
      await unwrap(api.POST("/api/v1/invites/{id}/revoke", { params: { path: { id } } }));
      void client.invalidateQueries({ queryKey: ["invites"] });
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <>
      <Panel>
        <Label size={16} bold>
          Invite links
        </Label>
        <Label tone="secondary" size={13}>
          Links are single-use and shown once. Share them yourself — no email is sent.
        </Label>
        <Choice<Role> label="Role" value={role} options={ROLES} onChange={setRole} />
        <Choice label="Expires after" value={hours} options={LIFETIMES} onChange={setHours} />
        <Field label="Note (who it is for)" maxLength={200} value={note} onChangeText={setNote} />
        <Button
          kind="primary"
          label={busy ? "Creating…" : "Create invite link"}
          disabled={busy}
          onPress={() => void create()}
        />
        {created ? (
          <Panel>
            <Label tone="secondary" size={13}>
              Share this {ROLE_LABELS[created.role].toLowerCase()} link now. It will not be shown
              again.
            </Label>
            <Label mono selectable size={12}>
              {link}
            </Label>
            <Row>
              <Button
                small
                kind="primary"
                label="Share link"
                onPress={() =>
                  void Share.share({ message: link }).catch(() =>
                    setError("Could not open the share sheet."),
                  )
                }
              />
              <Button
                small
                label={copied ? "Copied" : "Copy link"}
                onPress={async () => {
                  await Clipboard.setStringAsync(link, { android: { isSensitive: true } });
                  setCopied(true);
                }}
              />
              <Button small label="Done" onPress={() => setCreated(null)} />
            </Row>
          </Panel>
        ) : null}
        {error ? <Notice error>{error}</Notice> : null}
      </Panel>
      {invites.isLoading ? (
        <Loading label="Loading invites…" />
      ) : invites.isError ? (
        <ErrorState error={invites.error} retry={() => void invites.refetch()} />
      ) : !invites.data?.length ? (
        <Empty title="No invites yet" detail="Create a link to add someone." />
      ) : (
        invites.data.map((invite) => (
          <Panel key={invite.id}>
            <Row style={{ justifyContent: "space-between" }}>
              <Label mono size={13}>
                {invite.token_prefix}…
              </Label>
              <Label tone="secondary" size={13}>
                {ROLE_LABELS[invite.role]}
              </Label>
            </Row>
            {invite.note ? (
              <Label tone="muted" size={12}>
                {invite.note}
              </Label>
            ) : null}
            <Label tone="secondary" size={13}>
              {invite.status === "used" ? `Used by ${invite.used_by}` : invite.status} · expires{" "}
              {relative(invite.expires_at)}
            </Label>
            {invite.status === "pending" ? (
              <Button
                small
                kind="danger"
                label="Revoke"
                accessibilityLabel={`Revoke invite ${invite.token_prefix}`}
                onPress={() => void revoke(invite.id)}
              />
            ) : null}
          </Panel>
        ))
      )}
    </>
  );
}

export default function Users() {
  const { api } = useSession();
  const { canManage, username } = useAccess();
  const client = useQueryClient();
  const [section, setSection] = useState<"people" | "invites">("people");
  const users = useQuery({
    queryKey: ["users"],
    enabled: !!api && canManage,
    queryFn: () => unwrap(api!.GET("/api/v1/users")),
  });
  const pull = usePullRefresh(() =>
    Promise.all([
      client.invalidateQueries({ queryKey: ["users"] }),
      client.invalidateQueries({ queryKey: ["invites"] }),
    ]),
  );
  if (!canManage)
    return (
      <Screen>
        <Stack.Screen options={{ title: "Users & invites" }} />
        <Notice>Only owners manage people and invites.</Notice>
      </Screen>
    );
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Users & invites" }} />
      <Heading eyebrow="Owner">Users & invites</Heading>
      <Choice
        value={section}
        options={[
          { value: "people", label: `People${users.data ? ` ${users.data.length}` : ""}` },
          { value: "invites", label: "Invites" },
        ]}
        onChange={setSection}
      />
      {section === "people" ? (
        <>
          <Label tone="secondary" size={13}>
            Viewers read; members change tasks, planning and workers; owners also manage people and
            agents. At least one active owner always remains.
          </Label>
          {users.isLoading ? (
            <Loading label="Loading users…" />
          ) : users.isError ? (
            <ErrorState error={users.error} retry={() => void users.refetch()} />
          ) : (
            users.data?.map((user) => (
              <UserRow key={user.id} user={user} self={user.username === username} />
            ))
          )}
        </>
      ) : (
        <Invites />
      )}
    </Screen>
  );
}
