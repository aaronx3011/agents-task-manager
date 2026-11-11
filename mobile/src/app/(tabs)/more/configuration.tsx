// Configuration (#80, owners): registered agents and registering a new one,
// like the web Configuration page. The new API key is shown once: it lives
// only in this screen's state, is never cached or logged, and is cleared when
// dismissed, when the screen closes, or when the app goes to the background.
import { useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { Stack } from "expo-router";
import * as Clipboard from "expo-clipboard";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { Schemas } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import {
  Button,
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
import { age, errorText } from "@/ui/format";
import { Markdown } from "@/ui/markdown";

type Credentials = Schemas["AgentCredentials"];

function CopyButton({
  label,
  value,
  sensitive = false,
}: {
  label: string;
  value: string;
  sensitive?: boolean;
}) {
  const [feedback, setFeedback] = useState("");
  return (
    <Row>
      <Button
        small
        label={label}
        onPress={async () => {
          try {
            await Clipboard.setStringAsync(
              value,
              sensitive ? { android: { isSensitive: true } } : undefined,
            );
            setFeedback("Copied");
          } catch {
            setFeedback("Select and copy the text manually.");
          }
        }}
      />
      {feedback ? (
        <Label tone="accent" size={12}>
          {feedback}
        </Label>
      ) : null}
    </Row>
  );
}

function ConnectionDetails({ credentials, server }: { credentials: Credentials; server: string }) {
  const [checkout, setCheckout] = useState("/absolute/path/to/agents-task-manager");
  const directory = checkout.trim().replace(/\/+$/, "");
  const configuration = JSON.stringify(
    {
      mcp: {
        "task-coordinator": {
          type: "local",
          command: [`${directory}/.venv/bin/python`, `${directory}/mcp_server.py`],
          enabled: true,
          environment: {
            COORDINATOR_URL: server,
            COORDINATOR_API_KEY: "{env:COORDINATOR_API_KEY}",
          },
        },
      },
    },
    null,
    2,
  );
  const environment = `export COORDINATOR_URL=${JSON.stringify(server)}\nexport COORDINATOR_API_KEY=${JSON.stringify(credentials.api_key)}`;
  return (
    <Panel>
      <Label size={15} bold>
        MCP connection
      </Label>
      <Label tone="secondary" size={13}>
        This key identifies {credentials.name}. Load the environment variables privately on the
        agent’s machine, then add the OpenCode MCP entry.
      </Label>
      <Field
        label="Coordinator checkout on the agent’s machine"
        value={checkout}
        onChangeText={setCheckout}
        autoCapitalize="none"
        autoCorrect={false}
        hint="Replace the example with that machine’s absolute checkout path. Python dependencies must already be installed."
      />
      <Label tone="muted" size={12} bold>
        PRIVATE ENVIRONMENT
      </Label>
      <Markdown size={12} text={"```\n" + environment + "\n```"} />
      <CopyButton label="Copy environment" value={environment} sensitive />
      <Label tone="muted" size={12} bold>
        OPENCODE CONFIGURATION
      </Label>
      <Markdown size={12} text={"```\n" + configuration + "\n```"} />
      <CopyButton label="Copy MCP configuration" value={configuration} />
      <Label tone="muted" size={12}>
        The configuration references your private environment variable. Restart the client and call
        get_overview to verify the connection. Registering an agent does not create a worker bridge.
      </Label>
    </Panel>
  );
}

export default function Configuration() {
  const { api, server } = useSession();
  const { canManage } = useAccess();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const busy = useRef(false);
  useEffect(() => {
    // Never keep a one-time key around while the app is in the background.
    const subscription = AppState.addEventListener("change", (status) => {
      if (status !== "active") setCredentials(null);
    });
    return () => subscription.remove();
  }, []);
  const agents = useQuery({
    queryKey: ["configuration", "agents", offset],
    enabled: !!api && canManage,
    refetchInterval: 15_000,
    queryFn: () => unwrap(api!.GET("/api/v1/agents", { params: { query: { limit: 50, offset } } })),
  });
  const pull = usePullRefresh(() => agents.refetch());

  async function register() {
    if (!api || busy.current || credentials || !name.trim()) return;
    busy.current = true;
    setSubmitting(true);
    setError("");
    try {
      const created = await unwrap(api.POST("/api/v1/agents", { body: { name: name.trim() } }));
      setCredentials(created);
      setName("");
      setOffset(0);
      void client.invalidateQueries({ queryKey: ["configuration", "agents"] });
      void client.invalidateQueries({ queryKey: ["workspace"] });
    } catch (cause) {
      setError(errorText(cause, "Registration failed."));
    } finally {
      busy.current = false;
      setSubmitting(false);
    }
  }

  if (!canManage)
    return (
      <Screen>
        <Stack.Screen options={{ title: "Configuration" }} />
        <Notice>Only owners manage agent configuration.</Notice>
      </Screen>
    );
  const count = agents.data?.count ?? 0;
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Configuration" }} />
      <Heading eyebrow="Workspace settings">Configuration</Heading>
      <Label tone="secondary" size={13}>
        Register agent identities and connect them to your coordinator.
      </Label>
      <Panel>
        <Label size={16} bold>
          Register an agent
        </Label>
        <Label tone="secondary" size={13}>
          Choose a unique name for this client. A new API key is generated when you register it.
        </Label>
        <Field
          label="Agent name"
          value={name}
          onChangeText={setName}
          placeholder="e.g. laptop-code-reviewer"
          maxLength={100}
          autoCapitalize="none"
          autoCorrect={false}
          editable={!submitting && !credentials}
        />
        <Button
          kind="primary"
          label={submitting ? "Registering…" : "Register agent"}
          disabled={submitting || !!credentials || !name.trim()}
          onPress={() => void register()}
        />
        {error ? (
          <Notice error>
            {error} Check the registered-agent list before retrying a lost response. Existing keys
            cannot be displayed again.
          </Notice>
        ) : null}
      </Panel>
      {credentials ? (
        <Panel>
          <Label size={16} bold>
            Agent registered: {credentials.name}
          </Label>
          <Notice>
            Copy your API key now. It is available only until you dismiss this panel, leave
            Configuration or switch apps.
          </Notice>
          <Label tone="muted" size={12} bold>
            API KEY · SHOWN ONCE
          </Label>
          <Label mono selectable size={13}>
            {credentials.api_key}
          </Label>
          <CopyButton label="Copy API key" value={credentials.api_key} sensitive />
          <CopyButton label="Copy agent name" value={credentials.name} />
          <ConnectionDetails credentials={credentials} server={server} />
          <Button label="Done · dismiss credentials" onPress={() => setCredentials(null)} />
        </Panel>
      ) : null}
      <Label tone="muted" size={12} bold>
        REGISTERED AGENTS{agents.data ? ` · ${count}` : ""}
      </Label>
      <Label tone="secondary" size={13}>
        API keys identify these agents when they claim tasks and report results. Existing keys are
        never returned by this list.
      </Label>
      {agents.isLoading ? (
        <Loading label="Loading agents" />
      ) : agents.error ? (
        <ErrorState error={agents.error} retry={() => void agents.refetch()} />
      ) : !count ? (
        <Empty title="No agents registered yet" />
      ) : (
        agents.data!.items.map((agent) => (
          <Panel key={agent.id}>
            <Row style={{ justifyContent: "space-between" }}>
              <Label bold>{agent.name}</Label>
              <Label tone={agent.is_active ? "accent" : "muted"} size={13}>
                {agent.is_active ? "Active" : "Inactive"}
              </Label>
            </Row>
            <Label tone="muted" size={12}>
              Last seen: {age(agent.last_seen)}
            </Label>
          </Panel>
        ))
      )}
      {count > 50 ? (
        <Row style={{ justifyContent: "space-between" }}>
          <Button
            small
            label="Previous"
            disabled={!offset}
            onPress={() => setOffset(Math.max(0, offset - 50))}
          />
          <Label tone="muted" size={13}>
            {offset + 1}–{Math.min(offset + 50, count)} of {count}
          </Label>
          <Button
            small
            label="Next"
            disabled={offset + 50 >= count}
            onPress={() => setOffset(offset + 50)}
          />
        </Row>
      ) : null}
    </Screen>
  );
}
