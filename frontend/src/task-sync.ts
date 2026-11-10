import type { QueryClient } from "@tanstack/react-query";

export const TASK_SYNC_INTERVAL = 5000;

// Invalidate every representation together, including inactive cached views.
// The server task lifecycle remains the source of truth for lane membership.
export async function refreshTaskViews(client: QueryClient) {
  await Promise.all(
    ["planning", "workspace", "tasks", "task"].map((key) =>
      client.invalidateQueries({ queryKey: [key] }),
    ),
  );
}
