import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Command } from "lucide-react";
import { api, unwrap } from "./api-client";
import { primeCsrf, ROLE_LABELS } from "./auth";
import type { Role } from "./auth";
import "./auth.css";

function Card({
  title,
  lead,
  children,
}: {
  title: string;
  lead: string;
  children: ReactNode;
}) {
  return (
    <main className="auth-screen">
      <section className="auth-card" aria-label={title}>
        <div className="auth-brand">
          <span className="brand-mark">
            <Command size={20} />
          </span>
          agent<span className="brand-light">ops</span>
        </div>
        <h1>{title}</h1>
        <p className="auth-lead">{lead}</p>
        {children}
      </section>
    </main>
  );
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "Something went wrong";
}

export function LoginScreen({ expired = false }: { expired?: boolean }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(
    expired ? "Your session ended. Sign in again." : "",
  );
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await primeCsrf();
      await unwrap(
        api.POST("/api/v1/auth/login", { body: { username, password } }),
      );
      // Start clean as the new user: fresh queries, live socket and CSRF token.
      window.location.reload();
    } catch (e) {
      setError(message(e));
      setPassword("");
      setBusy(false);
    }
  }
  return (
    <Card title="Sign in" lead="Use the account an owner invited you to.">
      <form className="auth-form" onSubmit={submit}>
        <label>
          Username
          <input
            autoComplete="username"
            autoFocus
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="auth-error">
            {error}
          </p>
        )}
        <button className="button primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </Card>
  );
}

type Preview = { role: Role; expires_at: string };

export function InviteScreen() {
  // The token travels in the URL fragment so it never reaches server logs.
  const [token] = useState(() =>
    decodeURIComponent(window.location.hash.replace(/^#/, "")),
  );
  const [preview, setPreview] = useState<Preview | null>(null);
  const [problem, setProblem] = useState("");
  const [form, setForm] = useState({
    username: "",
    email: "",
    password: "",
    confirm: "",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    (async () => {
      if (!token) {
        setProblem(
          "This invite link is incomplete. Ask an owner for a new one.",
        );
        return;
      }
      try {
        await primeCsrf();
        const result = await unwrap(
          api.POST("/api/v1/invites/check", { body: { token } }),
        );
        if (alive) setPreview(result);
      } catch (e) {
        if (alive) setProblem(message(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (form.password !== form.confirm) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await unwrap(
        api.POST("/api/v1/invites/accept", {
          body: {
            token,
            username: form.username,
            password: form.password,
            email: form.email,
          },
        }),
      );
      // Drop the token from history before entering the workspace.
      window.location.replace("/");
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const set = (key: keyof typeof form) => (value: string) =>
    setForm((current) => ({ ...current, [key]: value }));
  if (problem)
    return (
      <Card title="Invite unavailable" lead={problem}>
        <a className="button secondary" href="/">
          Go to sign in
        </a>
      </Card>
    );
  if (!preview)
    return (
      <Card title="Checking invite…" lead="One moment.">
        <span />
      </Card>
    );
  return (
    <Card
      title="Join the workspace"
      lead={`You are invited as ${ROLE_LABELS[preview.role]}. Choose your sign-in details.`}
    >
      <form className="auth-form" onSubmit={submit}>
        <label>
          Username
          <input
            autoComplete="username"
            autoFocus
            required
            maxLength={150}
            value={form.username}
            onChange={(e) => set("username")(e.target.value)}
          />
        </label>
        <label>
          <span>
            Email <span className="muted">(optional)</span>
          </span>
          <input
            type="email"
            autoComplete="email"
            value={form.email}
            onChange={(e) => set("email")(e.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="new-password"
            required
            value={form.password}
            onChange={(e) => set("password")(e.target.value)}
          />
        </label>
        <label>
          Confirm password
          <input
            type="password"
            autoComplete="new-password"
            required
            value={form.confirm}
            onChange={(e) => set("confirm")(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="auth-error">
            {error}
          </p>
        )}
        <button className="button primary" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>
      </form>
    </Card>
  );
}
