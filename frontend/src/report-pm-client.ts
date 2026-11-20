import { api, unwrap } from "./api-client";
import { buildPmPrompt, classifyPmReply, type PmReport } from "./report-pm";

/** Existing owner-gated PM path: session capability -> chat turn -> revoke. */
export async function sendReportToPm(report: PmReport, signal?: AbortSignal) {
  await unwrap(api.GET("/api/v1/auth/csrf", { signal }));
  const grant = await unwrap(
    api.POST("/api/v1/voice/sessions", { body: { text_only: true }, signal }),
  );
  try {
    const reply = await unwrap(
      api.POST("/api/v1/voice/chat", {
        headers: { "X-ATM-Voice-Token": grant.tool_token },
        body: { text: buildPmPrompt(report) },
        signal,
      }),
    );
    return classifyPmReply(report.action, reply);
  } finally {
    // The capability is single-purpose; revoke even when the turn failed.
    await api
      .DELETE("/api/v1/voice/sessions/{id}", {
        params: { path: { id: grant.id } },
      })
      .catch(() => {});
  }
}
