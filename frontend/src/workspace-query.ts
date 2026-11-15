import type { paths } from "./api-client";
import type { Filters } from "./types";

type WorkspaceQuery = NonNullable<
  paths["/api/v1/dashboard/workspace"]["get"]["parameters"]["query"]
>;

/** Drop empty filters; the server validates values and answers 400 otherwise. */
export function workspaceQuery(filters: Filters): WorkspaceQuery {
  return Object.fromEntries(
    Object.entries(filters).filter(([, value]) => value !== ""),
  ) as WorkspaceQuery;
}
