// Generated from openapi/schema.json by `npm run api:generate --prefix frontend`. Do not edit.
export interface paths {
  "/api/v1/agents": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** @description Owner configuration surface; agent keys never grant provisioning rights. */
    get: operations["agents_list"];
    put?: never;
    /** @description Owner configuration surface; agent keys never grant provisioning rights. */
    post: operations["agents_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/attempts/{id}/link": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** @description Explicit authorized attempt↔session link after ownership checks (#13). */
    post: operations["attempts_link_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/csrf": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Set the csrftoken cookie and return the token for the X-CSRFToken header */
    get: operations["auth_csrf"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/login": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Start a browser session (requires X-CSRFToken) */
    post: operations["auth_login"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/logout": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** End the browser session (requires X-CSRFToken) */
    post: operations["auth_logout"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/me": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** The signed-in user */
    get: operations["auth_me"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/password": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Change your own password (requires the current one) */
    post: operations["auth_change_password"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/tokens": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * Your device tokens (owners: ?all=true for everyone's)
     * @description Issue (public, credentials in the body) and list (signed in) device tokens.
     */
    get: operations["auth_tokens_list"];
    put?: never;
    /**
     * Sign in from a device and receive a token (shown once)
     * @description Issue (public, credentials in the body) and list (signed in) device tokens.
     */
    post: operations["auth_tokens_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/auth/tokens/{id}/revoke": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Revoke one of your tokens ('current' for this device); owners revoke any */
    post: operations["auth_tokens_revoke"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/commands": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** @description Durable command queue APIs (issue #12): members any worker; agents own worker. */
    get: operations["commands_list"];
    put?: never;
    /** @description Durable command queue APIs (issue #12): members any worker; agents own worker. */
    post: operations["commands_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/commands/{id}": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** @description Durable command queue APIs (issue #12): members any worker; agents own worker. */
    get: operations["commands_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/commands/{id}/resolve": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** @description Explicit member reconciliation for uncertain or pending commands (#12). */
    post: operations["commands_resolve_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/dashboard/state": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Dashboard snapshot: counts, projects, agents, recent tasks and events */
    get: operations["dashboard_state"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/dashboard/workspace": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Workspace totals, agent assignments and events for a task filter */
    get: operations["dashboard_workspace"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/interactions": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * Unresolved permission and question requests across workers (#55)
     * @description Pending approvals for the mobile and web inboxes.
     *
     *     Humans see every worker's requests; agent keys only their bound worker's.
     *     Answer with POST /api/v1/commands (permission.reply, question.reply,
     *     question.reject), which requires a member.
     */
    get: operations["interactions_list"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/invites": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["invites_list"];
    put?: never;
    /** Create a single-use invite link (returned once) */
    post: operations["invites_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/invites/{id}/revoke": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["invites_revoke"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/invites/accept": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Accept an invite: create the account and start a session */
    post: operations["invites_accept"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/invites/check": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Preview a pending invite before accepting (requires X-CSRFToken) */
    post: operations["invites_check"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/meta": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * Deployment facts for the SPA (no authentication)
     * @description Public, data-free facts the SPA needs before sign-in (#50).
     */
    get: operations["meta"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/overview": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["overview_list"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/planning/jobs/{id}/report": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["planning_jobs_report_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/projects": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["projects_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/projects/{slug}/planning": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["projects_planning_retrieve"];
    put?: never;
    post: operations["projects_planning_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/realtime/ticket": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Single-use ticket for /ws/dashboard/ (send as WebSocket subprotocols) */
    post: operations["realtime_ticket"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/sessions": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Unified cross-worker session list (#15).
     *
     *     Workers stay isolated: the same remote ID on two workers remains two rows.
     *     Agents read only sessions of their bound worker; humans read everything.
     */
    get: operations["sessions_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["tasks_list"];
    put?: never;
    post: operations["tasks_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["tasks_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch: operations["tasks_partial_update"];
    trace?: never;
  };
  "/api/v1/tasks/{id}/claim": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_claim_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/events": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["tasks_events_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/heartbeat": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_heartbeat_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/launch": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** @description Authorized remote launch (issue #13): atomic-claim-based, idempotent. */
    post: operations["tasks_launch_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/notes": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_notes_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/release": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_release_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/{id}/status": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_status_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/tasks/next": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post: operations["tasks_next_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/users": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get: operations["users_list"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/users/{id}": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    /** Change a user's role and/or active state (keeps an active owner) */
    patch: operations["users_update"];
    trace?: never;
  };
  "/api/v1/users/{id}/password": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Set a new password for a user (their sessions end) */
    post: operations["users_set_password"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/worker/sync": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** @description Outbound bridge sync (#11): one authenticated envelope per cycle. */
    post: operations["worker_sync_create"];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_list"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers/{name}": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers/{name}/events": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_events_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers/{name}/sessions": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_sessions_list"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers/{name}/sessions/{sid}": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_sessions_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  "/api/v1/workers/{name}/sessions/{sid}/messages": {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /**
     * @description Authenticated, credential-free worker registry reads (#10, #15).
     *
     *     The registry stays readable by every agent; session data is scoped to the
     *     bound agent (#15): worker-scoped identity extends the project/task rules.
     */
    get: operations["workers_sessions_messages_retrieve"];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
}
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    AcceptInviteInputRequest: {
      token: string;
      username: string;
      password: string;
      email?: string;
    };
    /**
     * @description * `create` - create
     *     * `prompt` - prompt
     *     * `interrupt` - interrupt
     *     * `permission.reply` - permission.reply
     *     * `question.reply` - question.reply
     *     * `question.reject` - question.reject
     * @enum {string}
     */
    ActionEnum:
      | "create"
      | "prompt"
      | "interrupt"
      | "permission.reply"
      | "question.reply"
      | "question.reject";
    AgentCard: {
      id: number;
      name: string;
      is_active: boolean;
      /** Format: date-time */
      last_seen: string | null;
      /** Format: date-time */
      created_at: string;
    };
    AgentCardPage: {
      count: number;
      items: components["schemas"]["AgentCard"][];
    };
    AgentCredentials: {
      id: number;
      name: string;
      is_active: boolean;
      /** Format: date-time */
      last_seen: string | null;
      /** Format: date-time */
      created_at: string;
      /** @description Shown once. */
      api_key: string;
    };
    AgentRegistrationRequest: {
      name: string;
    };
    Assignment: {
      id: number;
      title: string;
      status: components["schemas"]["TaskStatusEnum"];
      /** Format: date-time */
      heartbeat_at: string | null;
    };
    AttemptLinkRequest: {
      sessionID: string;
    };
    CardInputRequest: {
      id?: number;
      title: string;
      description: string;
      column: string;
      position?: number;
      labels: string[];
      checklist: components["schemas"]["ChecklistItemRequest"][];
      instructions: string;
    };
    ChecklistItemRequest: {
      text: string;
      /** @default false */
      done: boolean;
    };
    ClaimNextRequest: {
      project?: string | null;
    };
    CommandRequest: {
      worker: string;
      action: components["schemas"]["ActionEnum"];
      payload?: {
        [key: string]: unknown;
      };
      sessionID?: string;
      client_request_id?: string;
    };
    /**
     * @description * `done` - done
     *     * `error` - error
     * @enum {string}
     */
    CommandResolutionEnum: "done" | "error";
    CreatedInvite: {
      id: number;
      role: components["schemas"]["RoleEnum"];
      token_prefix: string;
      note: string;
      readonly status: string;
      readonly created_by: string | null;
      /** Format: date-time */
      created_at: string;
      /** Format: date-time */
      expires_at: string;
      /** Format: date-time */
      used_at: string | null;
      readonly used_by: string | null;
      /** Format: date-time */
      revoked_at: string | null;
      /** @description Shown once; only its hash is stored. */
      token: string;
      /** @description Share this link manually. Shown once. */
      url: string;
    };
    CsrfToken: {
      csrfToken: string;
    };
    DashboardAgent: {
      name: string;
      is_active: boolean;
      /** Format: date-time */
      last_seen: string | null;
    };
    DashboardEvent: {
      id: number;
      task_id: number;
      title: string;
      project: string;
      readonly agent: string;
      event_type: string;
      message: string;
      /** Format: date-time */
      created_at: string;
    };
    DashboardState: {
      /** Format: date-time */
      server_time: string;
      counts: components["schemas"]["StatusCounts"];
      projects: components["schemas"]["StateProject"][];
      agents: components["schemas"]["DashboardAgent"][];
      tasks: components["schemas"]["Task"][];
      events: components["schemas"]["DashboardEvent"][];
      ready_count: number;
      total_tasks: number;
      displayed_tasks: number;
      scope_project: string;
      scope_total_tasks: number;
    };
    DependencyProgress: {
      done: number;
      total: number;
    };
    Detail: {
      detail: string;
    };
    /**
     * @description * `created` - Created
     *     * `claimed` - Claimed
     *     * `heartbeat` - Heartbeat
     *     * `status_changed` - Status changed
     *     * `released` - Released
     *     * `note` - Note
     *     * `execution.launched` - Execution launched
     *     * `execution.linked` - Execution session linked
     *     * `execution.done` - Execution outcome
     * @enum {string}
     */
    EventTypeEnum:
      | "created"
      | "claimed"
      | "heartbeat"
      | "status_changed"
      | "released"
      | "note"
      | "execution.launched"
      | "execution.linked"
      | "execution.done";
    HeartbeatResult: {
      ok: boolean;
      /** Format: date-time */
      heartbeat_at: string;
    };
    Invite: {
      id: number;
      role: components["schemas"]["RoleEnum"];
      token_prefix: string;
      note: string;
      readonly status: string;
      readonly created_by: string | null;
      /** Format: date-time */
      created_at: string;
      /** Format: date-time */
      expires_at: string;
      /** Format: date-time */
      used_at: string | null;
      readonly used_by: string | null;
      /** Format: date-time */
      revoked_at: string | null;
    };
    InviteInputRequest: {
      /** @default member */
      role: components["schemas"]["RoleEnum"];
      /** @default 72 */
      expires_in_hours: number;
      /** @default  */
      note: string;
    };
    InvitePreview: {
      role: components["schemas"]["RoleEnum"];
      /** Format: date-time */
      expires_at: string;
    };
    InviteTokenInputRequest: {
      token: string;
    };
    IssuedToken: {
      id: number;
      name: string;
      key_prefix: string;
      username: string;
      /** Format: date-time */
      created_at: string;
      /** Format: date-time */
      last_used_at: string | null;
      /** Format: date-time */
      revoked_at: string | null;
      readonly current: boolean;
      /** @description Shown once; send as `Authorization: Token <key>`. */
      token: string;
      user: components["schemas"]["Me"];
    };
    JobReportRequest: {
      token: string;
      status: components["schemas"]["PlanningJobReportStatusEnum"];
      /** @default  */
      summary: string;
    };
    LaunchRequest: {
      worker: string;
      directory?: string;
      client_request_id?: string;
    };
    LoginError: {
      detail: string;
    };
    LoginRequest: {
      username: string;
      password: string;
    };
    Me: {
      id: number;
      username: string;
      /** Format: email */
      email: string;
      role: components["schemas"]["RoleEnum"];
      is_superuser: boolean;
    };
    Meta: {
      demo: boolean;
      admin_url: string | null;
    };
    ModelChoiceRequest: {
      providerID: string;
      modelID: string;
    };
    NoteRequest: {
      message: string;
    };
    Ok: {
      ok: boolean;
    };
    /**
     * @description * `settings` - settings
     *     * `card` - card
     *     * `enqueue` - enqueue
     *     * `run` - run
     *     * `play` - play
     *     * `pause` - pause
     *     * `stop` - stop
     *     * `reorder` - reorder
     *     * `remove` - remove
     *     * `archive` - archive
     * @enum {string}
     */
    OperationEnum:
      | "settings"
      | "card"
      | "enqueue"
      | "run"
      | "play"
      | "pause"
      | "stop"
      | "reorder"
      | "remove"
      | "archive";
    OwnPasswordInputRequest: {
      current_password: string;
      new_password: string;
    };
    PaginatedTaskList: {
      count: number;
      items: components["schemas"]["Task"][];
    };
    PasswordInputRequest: {
      password: string;
    };
    PatchedTaskRequest: {
      title?: string;
      description?: string;
      project?: string;
      priority?: number;
      depends_on?: number[];
    };
    PatchedUserUpdateRequest: {
      role?: components["schemas"]["RoleEnum"];
      is_active?: boolean;
    };
    PendingInteractionCard: {
      /** @description Request ID; send it back as payload.requestID. */
      id: string;
      kind: components["schemas"]["PendingInteractionCardKindEnum"];
      worker: string;
      sessionID: string | null;
      sessionTitle: string | null;
      lastSeenAt: string | null;
      replyDispatched: boolean;
      replyCommandId: string | null;
      type: string | null;
      title: string | null;
      description: string | null;
      patterns: string[];
      questions: unknown[];
    };
    /**
     * @description * `permission` - permission
     *     * `question` - question
     * @enum {string}
     */
    PendingInteractionCardKindEnum: "permission" | "question";
    PendingInteractionPage: {
      count: number;
      items: components["schemas"]["PendingInteractionCard"][];
    };
    PlanSettingsRequest: {
      worker: string;
      directory: string;
      model: components["schemas"]["ModelChoiceRequest"] | null;
      context: string;
      instructions: string;
      templates: components["schemas"]["PromptTemplateRequest"][];
      columns: string[];
    };
    /**
     * @description * `heartbeat` - heartbeat
     *     * `done` - done
     *     * `blocked` - blocked
     *     * `review` - review
     * @enum {string}
     */
    PlanningJobReportStatusEnum: "heartbeat" | "done" | "blocked" | "review";
    PlanningMutationRequest: {
      revision: number;
      operation: components["schemas"]["OperationEnum"];
      settings?: components["schemas"]["PlanSettingsRequest"];
      card?: components["schemas"]["CardInputRequest"];
      card_id?: number;
      job_id?: number;
      job_ids?: number[];
      instructions?: string;
      template?: string;
    };
    ProjectCreated: {
      slug: string;
      name: string;
      kind: string;
      repo_url: string;
      docs_path: string;
      active_count: number;
    };
    /**
     * @description * `code` - Code
     *     * `design` - Design
     *     * `content` - Content
     *     * `other` - Other
     * @enum {string}
     */
    ProjectInputKindEnum: "code" | "design" | "content" | "other";
    ProjectInputRequest: {
      slug: string;
      name: string;
      /** @default code */
      kind: components["schemas"]["ProjectInputKindEnum"];
      repo_url?: string;
      /** @default  */
      docs_path: string;
    };
    ProjectOverview: {
      project: string;
      name: string;
      kind: string;
      repo_url: string;
      docs_path: string;
      counts: {
        [key: string]: number;
      };
      in_progress: unknown[];
      blocked: unknown[];
      ready_count: number;
      waiting: components["schemas"]["Task"][];
    };
    PromptTemplateRequest: {
      name: string;
      text: string;
    };
    RealtimeTicket: {
      ticket: string;
      expires_in: number;
      subprotocols: string[];
    };
    ReleaseRequest: {
      reason?: string;
    };
    ResolveRequest: {
      status: components["schemas"]["CommandResolutionEnum"];
      result?: unknown;
      error?: string;
    };
    /**
     * @description * `owner` - Owner
     *     * `member` - Member
     *     * `viewer` - Viewer
     * @enum {string}
     */
    RoleEnum: "owner" | "member" | "viewer";
    SessionPage: {
      count: number;
      items: unknown[];
    };
    StateProject: {
      slug: string;
      name: string;
      kind: string;
      docs_path: string;
      counts: components["schemas"]["StatusCounts"];
      ready_count: number;
    };
    StatusCounts: {
      pending: number;
      in_progress: number;
      blocked: number;
      review: number;
      done: number;
      cancelled: number;
    };
    StatusUpdateRequest: {
      status: components["schemas"]["TaskStatusUpdateEnum"];
      reason?: string;
      result_summary?: string;
    };
    Task: {
      readonly id: number;
      title: string;
      description: string;
      project: string;
      readonly status: components["schemas"]["TaskStatusEnum"];
      priority: number;
      readonly claimed_by: string | null;
      /** Format: date-time */
      readonly claimed_at: string | null;
      /** Format: date-time */
      readonly heartbeat_at: string | null;
      depends_on: number[];
      readonly blocked_reason: string;
      readonly result_summary: string;
      readonly is_ready: boolean;
      readonly dependency_progress: components["schemas"]["DependencyProgress"];
      readonly created_by: string | null;
      /** Format: date-time */
      readonly created_at: string;
      /** Format: date-time */
      readonly updated_at: string;
    };
    /** @description Task detail as returned by retrieve, including execution attempts (#13). */
    TaskDetailResponse: {
      readonly id: number;
      title: string;
      description: string;
      project: string;
      readonly status: components["schemas"]["TaskStatusEnum"];
      priority: number;
      readonly claimed_by: string | null;
      /** Format: date-time */
      readonly claimed_at: string | null;
      /** Format: date-time */
      readonly heartbeat_at: string | null;
      depends_on: number[];
      readonly blocked_reason: string;
      readonly result_summary: string;
      readonly is_ready: boolean;
      readonly dependency_progress: components["schemas"]["DependencyProgress"];
      readonly created_by: string | null;
      /** Format: date-time */
      readonly created_at: string;
      /** Format: date-time */
      readonly updated_at: string;
      readonly recent_events: components["schemas"]["TaskEvent"][];
      readonly dependencies: components["schemas"]["Task"][];
      readonly attempts: unknown[];
    };
    TaskEvent: {
      readonly id: number;
      event_type: components["schemas"]["EventTypeEnum"];
      readonly agent: string;
      message: string;
      /** Format: date-time */
      readonly created_at: string;
    };
    TaskEventPage: {
      items: components["schemas"]["TaskEvent"][];
      next_before: number | null;
    };
    TaskLaunch: {
      attempt: unknown;
      createCommand: unknown;
    };
    TaskRequest: {
      title: string;
      description?: string;
      project: string;
      priority?: number;
      depends_on?: number[];
    };
    /**
     * @description * `pending` - Pending
     *     * `in_progress` - In progress
     *     * `blocked` - Blocked
     *     * `review` - Review
     *     * `done` - Done
     *     * `cancelled` - Cancelled
     * @enum {string}
     */
    TaskStatusEnum:
      "pending" | "in_progress" | "blocked" | "review" | "done" | "cancelled";
    /**
     * @description * `in_progress` - in_progress
     *     * `blocked` - blocked
     *     * `review` - review
     *     * `done` - done
     *     * `cancelled` - cancelled
     * @enum {string}
     */
    TaskStatusUpdateEnum:
      "in_progress" | "blocked" | "review" | "done" | "cancelled";
    Token: {
      id: number;
      name: string;
      key_prefix: string;
      username: string;
      /** Format: date-time */
      created_at: string;
      /** Format: date-time */
      last_used_at: string | null;
      /** Format: date-time */
      revoked_at: string | null;
      readonly current: boolean;
    };
    TokenRequestRequest: {
      username: string;
      password: string;
      /** @description Device label, e.g. 'Ana's iPhone'. */
      name: string;
    };
    User: {
      id: number;
      username: string;
      /** Format: email */
      email: string;
      readonly role: string;
      is_active: boolean;
      is_superuser: boolean;
      /** Format: date-time */
      last_login: string | null;
      /** Format: date-time */
      date_joined: string;
    };
    WorkerCommandPage: {
      count: number;
      items: unknown[];
    };
    WorkerPage: {
      count: number;
      items: unknown[];
    };
    WorkerSessionPage: {
      count: number;
      items: unknown[];
    };
    WorkspaceAgent: {
      name: string;
      is_active: boolean;
      /** Format: date-time */
      last_seen: string | null;
      assignments: components["schemas"]["Assignment"][];
    };
    WorkspaceFilterError: {
      detail: {
        [key: string]: string;
      };
    };
    WorkspaceProject: {
      slug: string;
      name: string;
      kind: string;
      docs_path: string;
      active_count: number;
    };
    WorkspaceState: {
      /** Format: date-time */
      server_time: string;
      counts: components["schemas"]["StatusCounts"];
      total: number;
      ready_count: number;
      projects: components["schemas"]["WorkspaceProject"][];
      agents: components["schemas"]["WorkspaceAgent"][];
      events: components["schemas"]["DashboardEvent"][];
    };
  };
  responses: never;
  parameters: never;
  requestBodies: never;
  headers: never;
  pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
  agents_list: {
    parameters: {
      query?: {
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["AgentCardPage"];
        };
      };
    };
  };
  agents_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["AgentRegistrationRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["AgentRegistrationRequest"];
        "multipart/form-data": components["schemas"]["AgentRegistrationRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["AgentCredentials"];
        };
      };
    };
  };
  attempts_link_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: number;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["AttemptLinkRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["AttemptLinkRequest"];
        "multipart/form-data": components["schemas"]["AttemptLinkRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  auth_csrf: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["CsrfToken"];
        };
      };
    };
  };
  auth_login: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["LoginRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["LoginRequest"];
        "multipart/form-data": components["schemas"]["LoginRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Me"];
        };
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  auth_logout: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description No response body */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
    };
  };
  auth_me: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Me"];
        };
      };
    };
  };
  auth_change_password: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["OwnPasswordInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["OwnPasswordInputRequest"];
        "multipart/form-data": components["schemas"]["OwnPasswordInputRequest"];
      };
    };
    responses: {
      /** @description No response body */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  auth_tokens_list: {
    parameters: {
      query?: {
        /** @description Owners only: include every user's tokens */
        all?: boolean;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Token"][];
        };
      };
    };
  };
  auth_tokens_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["TokenRequestRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["TokenRequestRequest"];
        "multipart/form-data": components["schemas"]["TokenRequestRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["IssuedToken"];
        };
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  auth_tokens_revoke: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Token"];
        };
      };
      404: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  commands_list: {
    parameters: {
      query?: {
        /** @description Command action. */
        action?: string;
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
        /** @description Command status. */
        status?: string;
        /** @description Worker name. */
        worker?: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["WorkerCommandPage"];
        };
      };
    };
  };
  commands_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["CommandRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["CommandRequest"];
        "multipart/form-data": components["schemas"]["CommandRequest"];
      };
    };
    responses: {
      202: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  commands_retrieve: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  commands_resolve_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["ResolveRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["ResolveRequest"];
        "multipart/form-data": components["schemas"]["ResolveRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  dashboard_state: {
    parameters: {
      query?: {
        /** @description Scope tasks and events to a slug. */
        project?: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["DashboardState"];
        };
      };
    };
  };
  dashboard_workspace: {
    parameters: {
      query?: {
        /** @description Agent name, or __unclaimed__. */
        claimed_by?: string;
        /** @description Sort field, optionally prefixed with '-'. */
        ordering?:
          | "-claimed_by"
          | "-priority"
          | "-project"
          | "-status"
          | "-title"
          | "-updated_at"
          | "claimed_by"
          | "priority"
          | "project"
          | "status"
          | "title"
          | "updated_at";
        /** @description Project slug. */
        project?: string;
        /** @description Search title/description, or #id. */
        q?: string;
        /** @description true for ready tasks, false for not-ready tasks. */
        ready?: "false" | "true";
        /** @description Task status. */
        status?:
          | "blocked"
          | "cancelled"
          | "done"
          | "in_progress"
          | "pending"
          | "review";
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["WorkspaceState"];
        };
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["WorkspaceFilterError"];
        };
      };
    };
  };
  interactions_list: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["PendingInteractionPage"];
        };
      };
    };
  };
  invites_list: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Invite"][];
        };
      };
    };
  };
  invites_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": components["schemas"]["InviteInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["InviteInputRequest"];
        "multipart/form-data": components["schemas"]["InviteInputRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["CreatedInvite"];
        };
      };
    };
  };
  invites_revoke: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: number;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Invite"];
        };
      };
    };
  };
  invites_accept: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["AcceptInviteInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["AcceptInviteInputRequest"];
        "multipart/form-data": components["schemas"]["AcceptInviteInputRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Me"];
        };
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      410: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  invites_check: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["InviteTokenInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["InviteTokenInputRequest"];
        "multipart/form-data": components["schemas"]["InviteTokenInputRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["InvitePreview"];
        };
      };
      410: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  meta: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Meta"];
        };
      };
    };
  };
  overview_list: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["ProjectOverview"][];
        };
      };
    };
  };
  planning_jobs_report_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: number;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["JobReportRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["JobReportRequest"];
        "multipart/form-data": components["schemas"]["JobReportRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  projects_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["ProjectInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["ProjectInputRequest"];
        "multipart/form-data": components["schemas"]["ProjectInputRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["ProjectCreated"];
        };
      };
    };
  };
  projects_planning_retrieve: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        slug: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  projects_planning_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        slug: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["PlanningMutationRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["PlanningMutationRequest"];
        "multipart/form-data": components["schemas"]["PlanningMutationRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  realtime_ticket: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["RealtimeTicket"];
        };
      };
    };
  };
  sessions_retrieve: {
    parameters: {
      query?: {
        /** @description Only sessions in this worker directory. */
        directory?: string;
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
        /** @description recent for most recently updated first. */
        ordering?: "recent";
        /** @description Only sessions linked to this project id. */
        project?: string;
        /** @description Only sessions with this status. */
        status?: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["SessionPage"];
        };
      };
    };
  };
  tasks_list: {
    parameters: {
      query?: {
        /** @description Agent name, or __unclaimed__. */
        claimed_by?: string;
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
        /** @description Sort field, optionally prefixed with '-'. */
        ordering?:
          | "-claimed_by"
          | "-priority"
          | "-project"
          | "-status"
          | "-title"
          | "-updated_at"
          | "claimed_by"
          | "priority"
          | "project"
          | "status"
          | "title"
          | "updated_at";
        /** @description Project slug. */
        project?: string;
        /** @description Search title/description, or #id. */
        q?: string;
        /** @description true for ready tasks, false for not-ready tasks. */
        ready?: "false" | "true";
        /** @description Task status. */
        status?:
          | "blocked"
          | "cancelled"
          | "done"
          | "in_progress"
          | "pending"
          | "review";
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["PaginatedTaskList"];
        };
      };
    };
  };
  tasks_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["TaskRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["TaskRequest"];
        "multipart/form-data": components["schemas"]["TaskRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
    };
  };
  tasks_retrieve: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["TaskDetailResponse"];
        };
      };
    };
  };
  tasks_partial_update: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": components["schemas"]["PatchedTaskRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["PatchedTaskRequest"];
        "multipart/form-data": components["schemas"]["PatchedTaskRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
    };
  };
  tasks_claim_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
    };
  };
  tasks_events_retrieve: {
    parameters: {
      query?: {
        /** @description Return events older than this event ID. */
        before?: number;
      };
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["TaskEventPage"];
        };
      };
    };
  };
  tasks_heartbeat_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["HeartbeatResult"];
        };
      };
    };
  };
  tasks_launch_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["LaunchRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["LaunchRequest"];
        "multipart/form-data": components["schemas"]["LaunchRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["TaskLaunch"];
        };
      };
      202: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["TaskLaunch"];
        };
      };
    };
  };
  tasks_notes_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["NoteRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["NoteRequest"];
        "multipart/form-data": components["schemas"]["NoteRequest"];
      };
    };
    responses: {
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Ok"];
        };
      };
    };
  };
  tasks_release_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": components["schemas"]["ReleaseRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["ReleaseRequest"];
        "multipart/form-data": components["schemas"]["ReleaseRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
    };
  };
  tasks_status_create: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["StatusUpdateRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["StatusUpdateRequest"];
        "multipart/form-data": components["schemas"]["StatusUpdateRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
    };
  };
  tasks_next_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": components["schemas"]["ClaimNextRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["ClaimNextRequest"];
        "multipart/form-data": components["schemas"]["ClaimNextRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Task"];
        };
      };
      404: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["Detail"];
        };
      };
    };
  };
  users_list: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["User"][];
        };
      };
    };
  };
  users_update: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: number;
      };
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": components["schemas"]["PatchedUserUpdateRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["PatchedUserUpdateRequest"];
        "multipart/form-data": components["schemas"]["PatchedUserUpdateRequest"];
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["User"];
        };
      };
      404: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  users_set_password: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        id: number;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        "application/json": components["schemas"]["PasswordInputRequest"];
        "application/x-www-form-urlencoded": components["schemas"]["PasswordInputRequest"];
        "multipart/form-data": components["schemas"]["PasswordInputRequest"];
      };
    };
    responses: {
      /** @description No response body */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
      404: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["LoginError"];
        };
      };
    };
  };
  worker_sync_create: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: {
      content: {
        "application/json": {
          [key: string]: unknown;
        };
        "application/x-www-form-urlencoded": {
          [key: string]: unknown;
        };
        "multipart/form-data": {
          [key: string]: unknown;
        };
      };
    };
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  workers_list: {
    parameters: {
      query?: {
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["WorkerPage"];
        };
      };
    };
  };
  workers_retrieve: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        /** @description Worker name */
        name: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  workers_events_retrieve: {
    parameters: {
      query?: {
        /** @description Return events after this cursor. */
        after?: string;
        /** @description Maximum number of events. */
        limit?: number;
      };
      header?: never;
      path: {
        /** @description Worker name */
        name: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  workers_sessions_list: {
    parameters: {
      query?: {
        /** @description Only sessions in this worker directory. */
        directory?: string;
        /** @description Page size, 1-500 (default 100). */
        limit?: number;
        /** @description Number of items to skip. */
        offset?: number;
        /** @description Only sessions linked to this project id. */
        project?: string;
        /** @description Only sessions with this status. */
        status?: string;
      };
      header?: never;
      path: {
        /** @description Worker name */
        name: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": components["schemas"]["WorkerSessionPage"];
        };
      };
    };
  };
  workers_sessions_retrieve: {
    parameters: {
      query?: {
        /** @description true for a compact detail. */
        compact?: string;
      };
      header?: never;
      path: {
        /** @description Worker name */
        name: string;
        /** @description Remote session ID */
        sid: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
  workers_sessions_messages_retrieve: {
    parameters: {
      query?: {
        /** @description Return messages after this cursor. */
        after?: string;
        /** @description Return messages before this cursor. */
        before?: string;
        /** @description true for the most recent messages. */
        latest?: "true";
        /** @description Maximum number of messages. */
        limit?: number;
      };
      header?: never;
      path: {
        /** @description Worker name */
        name: string;
        /** @description Remote session ID */
        sid: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          "application/json": {
            [key: string]: unknown;
          };
        };
      };
    };
  };
}
