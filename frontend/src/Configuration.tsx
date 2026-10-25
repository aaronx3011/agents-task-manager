import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, RefreshCw, UserPlus, Users } from "lucide-react";
import { get, post } from "./api";
import { Age, Empty, ErrorState, Loading } from "./ui";
import type { Page } from "./types";
import "./configuration.css";

interface RegisteredAgent {
  id: number;
  name: string;
  is_active: boolean;
  last_seen: string | null;
  created_at: string;
}
interface Credentials extends RegisteredAgent {
  api_key: string;
}

function CopyButton({ label, value }: { label: string; value: string }) {
  const [feedback, setFeedback] = useState("");
  return (
    <div className="config-copy">
      <button
        type="button"
        className="button secondary"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setFeedback("Copied");
          } catch {
            setFeedback("Select and copy the text manually.");
          }
        }}
      >
        <Copy size={14} />
        {label}
      </button>
      <span role="status">{feedback}</span>
    </div>
  );
}

function ConnectionDetails({ credentials }: { credentials: Credentials }) {
  const [checkout, setCheckout] = useState(
    "/absolute/path/to/agents-task-manager",
  );
  const directory = checkout.trim().replace(/\/+$/, "");
  const configuration = JSON.stringify(
    {
      mcp: {
        "task-coordinator": {
          type: "local",
          command: [
            `${directory}/.venv/bin/python`,
            `${directory}/mcp_server.py`,
          ],
          enabled: true,
          environment: {
            COORDINATOR_URL: window.location.origin,
            COORDINATOR_API_KEY: "{env:COORDINATOR_API_KEY}",
          },
        },
      },
    },
    null,
    2,
  );
  const environment = `export COORDINATOR_URL=${JSON.stringify(window.location.origin)}\nexport COORDINATOR_API_KEY=${JSON.stringify(credentials.api_key)}`;
  return (
    <div className="config-connection">
      <h3>MCP connection</h3>
      <p>
        This key identifies <strong>{credentials.name}</strong>. Load the
        environment variables privately on the agent’s machine, then add the
        OpenCode MCP entry.
      </p>
      <label>
        Coordinator checkout on the agent’s machine
        <input
          aria-label="Coordinator checkout path"
          value={checkout}
          onChange={(event) => setCheckout(event.target.value)}
          spellCheck={false}
        />
      </label>
      <p className="muted">
        Replace the example with that machine’s absolute checkout path. Python
        dependencies must already be installed.
      </p>
      <h4>Private environment</h4>
      <pre aria-label="Private MCP environment">{environment}</pre>
      <CopyButton label="Copy environment" value={environment} />
      <h4>OpenCode configuration</h4>
      <pre aria-label="OpenCode MCP configuration">{configuration}</pre>
      <CopyButton label="Copy MCP configuration" value={configuration} />
      <p className="muted">
        The configuration references your private environment variable. Restart
        the client and call get_overview to verify the connection. Registering
        an agent does not create a worker bridge.
      </p>
    </div>
  );
}

export default function Configuration() {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  // Keep the one-time response out of React Query's query/mutation caches.
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const mounted = useRef(false);
  const busy = useRef(false);
  useEffect(() => {
    mounted.current = true;
    const clear = () => setCredentials(null);
    window.addEventListener("pagehide", clear);
    return () => {
      mounted.current = false;
      window.removeEventListener("pagehide", clear);
    };
  }, []);
  const agents = useQuery({
    queryKey: ["configuration", "agents", offset],
    queryFn: () =>
      get<Page<RegisteredAgent>>(`/api/agents?limit=50&offset=${offset}`),
    refetchInterval: 15000,
  });
  async function register(event: React.FormEvent) {
    event.preventDefault();
    if (busy.current || credentials || !name.trim()) return;
    busy.current = true;
    setSubmitting(true);
    setError("");
    try {
      const created = await post<Credentials>("/api/agents", {
        name: name.trim(),
      });
      if (mounted.current) {
        setCredentials(created);
        setName("");
        setOffset(0);
        void client.invalidateQueries({
          queryKey: ["configuration", "agents"],
        });
        void client.invalidateQueries({ queryKey: ["workspace"] });
      }
    } catch (cause) {
      if (mounted.current)
        setError(
          cause instanceof Error ? cause.message : "Registration failed.",
        );
    } finally {
      busy.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }
  return (
    <section className="configuration" aria-label="Agent configuration">
      <header className="workspace-heading">
        <div>
          <div className="eyebrow">WORKSPACE SETTINGS</div>
          <h1>
            Configuration<span className="heading-dot">.</span>
          </h1>
          <p>Register agent identities and connect them to your coordinator.</p>
        </div>
      </header>
      <h2 className="config-heading">
        <Users size={18} /> Agents
      </h2>
      <div className="config-layout">
        <section className="config-card" aria-label="Register agent">
          <h3>
            <UserPlus size={17} /> Register an agent
          </h3>
          <p>
            Choose a unique name for this client. A new API key is generated
            when you register it.
          </p>
          <form onSubmit={(event) => void register(event)}>
            <label>
              Agent name
              <input
                aria-label="Agent name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. laptop-code-reviewer"
                maxLength={100}
                autoComplete="off"
                required
                disabled={submitting || Boolean(credentials)}
              />
            </label>
            <button
              className="button secondary"
              disabled={submitting || Boolean(credentials) || !name.trim()}
            >
              <KeyRound size={14} />
              {submitting ? "Registering…" : "Register agent"}
            </button>
          </form>
          {error && (
            <p role="alert" className="config-error">
              {error} Check the registered-agent list before retrying a lost
              response. Existing keys cannot be displayed again.
            </p>
          )}
          {credentials && (
            <section
              className="config-credentials"
              aria-label="New agent credentials"
            >
              <h3>Agent registered: {credentials.name}</h3>
              <p>
                Copy your API key now. It is available only until you dismiss
                this panel or leave Configuration.
              </p>
              <label>
                Agent name
                <input
                  aria-label="Registered agent name"
                  value={credentials.name}
                  readOnly
                />
              </label>
              <CopyButton label="Copy agent name" value={credentials.name} />
              <label>
                API key · shown once
                <input
                  aria-label="New agent API key"
                  value={credentials.api_key}
                  readOnly
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <CopyButton label="Copy API key" value={credentials.api_key} />
              <ConnectionDetails credentials={credentials} />
              <button
                type="button"
                className="button secondary"
                onClick={() => setCredentials(null)}
              >
                Done · dismiss credentials
              </button>
            </section>
          )}
        </section>
        <section className="config-card" aria-label="Registered agents">
          <div className="config-list-header">
            <h3>
              Registered agents{" "}
              <span className="muted">({agents.data?.count ?? "—"})</span>
            </h3>
            <button
              className="icon-button"
              aria-label="Refresh registered agents"
              onClick={() => void agents.refetch()}
            >
              <RefreshCw size={15} />
            </button>
          </div>
          <p>
            API keys identify these agents when they claim tasks and report
            results. Existing keys are never returned by this list.
          </p>
          {agents.isLoading ? (
            <Loading />
          ) : agents.error ? (
            <ErrorState
              error={agents.error}
              retry={() => void agents.refetch()}
            />
          ) : !agents.data?.count ? (
            <Empty title="No agents registered yet" />
          ) : (
            <ul className="config-agent-list">
              {agents.data.items.map((agent) => (
                <li key={agent.id}>
                  <strong>{agent.name}</strong>
                  <span className={agent.is_active ? "config-active" : "muted"}>
                    {agent.is_active ? "Active" : "Inactive"}
                  </span>
                  <span className="muted">
                    Last seen: <Age value={agent.last_seen} />
                  </span>
                </li>
              ))}
            </ul>
          )}
          {(agents.data?.count || 0) > 50 && (
            <div className="config-pagination">
              <button
                className="button secondary"
                disabled={!offset}
                onClick={() => setOffset(Math.max(0, offset - 50))}
              >
                Previous agents
              </button>
              <span>
                {offset + 1}–{Math.min(offset + 50, agents.data?.count || 0)}
              </span>
              <button
                className="button secondary"
                disabled={offset + 50 >= (agents.data?.count || 0)}
                onClick={() => setOffset(offset + 50)}
              >
                Next agents
              </button>
            </div>
          )}
        </section>
      </div>
    </section>
  );
}
