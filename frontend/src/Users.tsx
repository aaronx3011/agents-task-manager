import { useState } from "react";
import type { FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  KeyRound,
  Link2,
  ShieldCheck,
  Smartphone,
  UserCog,
} from "lucide-react";
import { api, unwrap } from "./api-client";
import type { Schemas } from "./api-client";
import { ROLE_LABELS, useAccess } from "./auth";
import type { Role } from "./auth";
import { CopyButton } from "./Configuration";
import { Age, Empty, ErrorState, Loading } from "./ui";
import "./configuration.css";
import "./auth.css";

const ROLES: Role[] = ["viewer", "member", "owner"];
const LIFETIMES = [
  { hours: 24, label: "1 day" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days" },
  { hours: 720, label: "30 days" },
];

function message(error: unknown) {
  return error instanceof Error ? error.message : "Something went wrong";
}

function Heading({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <header className="workspace-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>
          {title}
          <span className="heading-dot">.</span>
        </h1>
      </div>
    </header>
  );
}

function Invites() {
  const client = useQueryClient();
  const invites = useQuery({
    queryKey: ["invites"],
    queryFn: () => unwrap(api.GET("/api/v1/invites")),
  });
  const [role, setRole] = useState<Role>("member");
  const [hours, setHours] = useState(72);
  const [note, setNote] = useState("");
  const [created, setCreated] = useState<Schemas["CreatedInvite"] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await unwrap(
        api.POST("/api/v1/invites", {
          body: { role, expires_in_hours: hours, note },
        }),
      );
      setCreated(result);
      setNote("");
      void client.invalidateQueries({ queryKey: ["invites"] });
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: number) {
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/invites/{id}/revoke", { params: { path: { id } } }),
      );
      void client.invalidateQueries({ queryKey: ["invites"] });
    } catch (e) {
      setError(message(e));
    }
  }
  return (
    <section className="config-card" aria-label="Invites">
      <h3>
        <Link2 size={17} /> Invite links
      </h3>
      <p>
        Links are single-use and shown once. Share them yourself — no email is
        sent.
      </p>
      <form onSubmit={create}>
        <label>
          Role
          <select
            className="config-select"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            {ROLES.map((item) => (
              <option key={item} value={item}>
                {ROLE_LABELS[item]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Expires after
          <select
            className="config-select"
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
          >
            {LIFETIMES.map((item) => (
              <option key={item.hours} value={item.hours}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>
            Note <span className="muted">(who it is for)</span>
          </span>
          <input
            maxLength={200}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
        <button className="button primary" disabled={busy}>
          {busy ? "Creating…" : "Create invite link"}
        </button>
      </form>
      {created && (
        <div className="config-credentials" role="status">
          <p>
            Copy this {ROLE_LABELS[created.role].toLowerCase()} link now. It
            will not be shown again.
          </p>
          <input readOnly value={created.url} aria-label="Invite link" />
          <CopyButton label="Copy link" value={created.url} />
          <button
            type="button"
            className="button secondary"
            onClick={() => setCreated(null)}
          >
            Done
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="config-error">
          {error}
        </p>
      )}
      {invites.isLoading ? (
        <Loading label="Loading invites…" />
      ) : invites.isError ? (
        <ErrorState
          error={invites.error}
          retry={() => void invites.refetch()}
        />
      ) : !invites.data?.length ? (
        <Empty
          title="No invites yet"
          description="Create a link to add someone."
        />
      ) : (
        <div className="users-scroll">
          <table className="users-table">
            <thead>
              <tr>
                <th>Link</th>
                <th>Role</th>
                <th>Status</th>
                <th>Expires</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {invites.data.map((invite) => (
                <tr key={invite.id}>
                  <td>
                    <code>{invite.token_prefix}…</code>
                    {invite.note && <div className="muted">{invite.note}</div>}
                  </td>
                  <td>{ROLE_LABELS[invite.role]}</td>
                  <td>
                    {invite.status === "used"
                      ? `Used by ${invite.used_by}`
                      : invite.status}
                  </td>
                  <td>
                    <Age value={invite.expires_at} />
                  </td>
                  <td>
                    {invite.status === "pending" && (
                      <button
                        className="button secondary"
                        onClick={() => void revoke(invite.id)}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function UserRow({ user, self }: { user: Schemas["User"]; self: boolean }) {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [password, setPassword] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  async function update(body: { role?: Role; is_active?: boolean }) {
    setError("");
    setSaved("");
    try {
      await unwrap(
        api.PATCH("/api/v1/users/{id}", {
          params: { path: { id: user.id } },
          body,
        }),
      );
      void client.invalidateQueries({ queryKey: ["users"] });
      if (self) void client.invalidateQueries({ queryKey: ["me"] });
    } catch (e) {
      setError(message(e));
    }
  }
  async function setNewPassword(event: FormEvent) {
    event.preventDefault();
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
      setError(message(e));
    }
  }
  return (
    <>
      <tr className={user.is_active ? "" : "muted"}>
        <td>
          <strong>{user.username}</strong>
          {self && <span className="muted"> (you)</span>}
          {user.email && <div className="muted">{user.email}</div>}
        </td>
        <td>
          <select
            aria-label={`Role for ${user.username}`}
            value={user.role}
            onChange={(e) => void update({ role: e.target.value as Role })}
          >
            {ROLES.map((item) => (
              <option key={item} value={item}>
                {ROLE_LABELS[item]}
              </option>
            ))}
          </select>
        </td>
        <td>{user.is_active ? "Active" : "Deactivated"}</td>
        <td>{user.last_login ? <Age value={user.last_login} /> : "Never"}</td>
        <td>
          <div className="row-actions">
            <button
              className="button secondary"
              onClick={() => void update({ is_active: !user.is_active })}
            >
              {user.is_active ? "Deactivate" : "Reactivate"}
            </button>
            <button
              className="button secondary"
              onClick={() => setPassword(password === null ? "" : null)}
            >
              <KeyRound size={14} /> Set password
            </button>
          </div>
        </td>
      </tr>
      {(password !== null || error || saved) && (
        <tr>
          <td colSpan={5}>
            {password !== null && (
              <form className="auth-dialog" onSubmit={setNewPassword}>
                <label>
                  New password for {user.username}
                  <input
                    type="password"
                    autoComplete="new-password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>
                <div className="row-actions">
                  <button className="button primary">Save password</button>
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => setPassword(null)}
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
            {error && (
              <p role="alert" className="config-error">
                {error}
              </p>
            )}
            {saved && <p role="status">{saved}</p>}
          </td>
        </tr>
      )}
    </>
  );
}

function UserList() {
  const { me } = useAccess();
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => unwrap(api.GET("/api/v1/users")),
  });
  return (
    <section className="config-card" aria-label="Users">
      <h3>
        <UserCog size={17} /> People
      </h3>
      <p>
        Viewers read; members change tasks, planning and workers; owners also
        manage people and agents. At least one active owner always remains.
      </p>
      {users.isLoading ? (
        <Loading label="Loading users…" />
      ) : users.isError ? (
        <ErrorState error={users.error} retry={() => void users.refetch()} />
      ) : (
        <div className="users-scroll">
          <table className="users-table">
            <thead>
              <tr>
                <th>User</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last sign-in</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.data?.map((user) => (
                <UserRow key={user.id} user={user} self={user.id === me.id} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function UsersPage() {
  return (
    <section className="configuration" aria-label="User management">
      <Heading eyebrow="OWNER" title="Users & invites" />
      <div className="users-layout">
        <UserList />
        <Invites />
      </div>
    </section>
  );
}

function ChangePassword() {
  const [form, setForm] = useState({ current: "", next: "", confirm: "" });
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setDone(false);
    if (form.next !== form.confirm) {
      setError("The new passwords do not match.");
      return;
    }
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/auth/password", {
          body: { current_password: form.current, new_password: form.next },
        }),
      );
      setForm({ current: "", next: "", confirm: "" });
      setDone(true);
    } catch (e) {
      setError(message(e));
    }
  }
  const field = (key: keyof typeof form, label: string, auto: string) => (
    <label>
      {label}
      <input
        type="password"
        autoComplete={auto}
        required
        value={form[key]}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </label>
  );
  return (
    <section className="config-card" aria-label="Change password">
      <h3>
        <ShieldCheck size={17} /> Password
      </h3>
      <p>Other browsers signed in to your account will be signed out.</p>
      <form onSubmit={submit}>
        {field("current", "Current password", "current-password")}
        {field("next", "New password", "new-password")}
        {field("confirm", "Confirm new password", "new-password")}
        {error && (
          <p role="alert" className="config-error">
            {error}
          </p>
        )}
        {done && <p role="status">Password changed.</p>}
        <button className="button primary">Change password</button>
      </form>
    </section>
  );
}

function Devices() {
  const client = useQueryClient();
  const tokens = useQuery({
    queryKey: ["tokens"],
    queryFn: () => unwrap(api.GET("/api/v1/auth/tokens")),
  });
  const [error, setError] = useState("");
  async function revoke(id: number) {
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/auth/tokens/{id}/revoke", {
          params: { path: { id: String(id) } },
        }),
      );
      void client.invalidateQueries({ queryKey: ["tokens"] });
    } catch (e) {
      setError(message(e));
    }
  }
  const live = tokens.data?.filter((token) => !token.revoked_at) || [];
  return (
    <section className="config-card" aria-label="Signed-in devices">
      <h3>
        <Smartphone size={17} /> Mobile devices
      </h3>
      <p>Devices signed in with the mobile app. Revoking signs them out.</p>
      {error && (
        <p role="alert" className="config-error">
          {error}
        </p>
      )}
      {tokens.isLoading ? (
        <Loading label="Loading devices…" />
      ) : !live.length ? (
        <Empty
          title="No devices"
          description="Sign in from the mobile app to add one."
        />
      ) : (
        <ul className="config-agent-list">
          {live.map((token) => (
            <li key={token.id}>
              <strong>{token.name}</strong>
              <span className="muted">
                {token.last_used_at ? (
                  <>
                    used <Age value={token.last_used_at} />
                  </>
                ) : (
                  "never used"
                )}
              </span>
              <button
                className="button secondary"
                onClick={() => void revoke(token.id)}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function AccountPage() {
  const { me } = useAccess();
  return (
    <section className="configuration" aria-label="Account">
      <Heading
        eyebrow={ROLE_LABELS[me.role].toUpperCase()}
        title={me.username}
      />
      <div className="config-layout">
        <ChangePassword />
        <Devices />
      </div>
    </section>
  );
}
