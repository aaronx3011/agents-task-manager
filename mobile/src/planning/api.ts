// Project planning data (#79), mirroring frontend/src/Planning.tsx: one
// revisioned plan per project; every change is an `operation` POSTed with the
// revision it was based on, and a conflict refetches the board.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { Plan, PlanningCard } from "@/api/types";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { errorText } from "@/ui/format";

export function usePlan(slug: string) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["planning", slug],
    enabled: !!api && !!slug,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/projects/{slug}/planning", { params: { path: { slug } } }),
      )) as unknown as Plan,
  });
}

export function editableCard(card: PlanningCard) {
  return {
    id: card.id,
    title: card.title,
    description: card.description,
    column: card.column,
    position: card.position,
    labels: [...new Set(card.labels.map((label) => label.trim()).filter(Boolean))],
    checklist: card.checklist,
    instructions: card.instructions,
  };
}

/** Send planning operations; returns whether the change was saved. */
export function usePlanMutation(slug: string, plan: Plan | undefined) {
  const { api } = useSession();
  const { canWrite } = useAccess();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function mutate(values: Record<string, unknown>, revision = plan?.revision) {
    if (!canWrite) {
      setError("Viewers have read-only access to planning.");
      return false;
    }
    if (busy || revision === undefined || !api) return false;
    setBusy(true);
    setError("");
    try {
      const result = (await unwrap(
        api.POST("/api/v1/projects/{slug}/planning", {
          params: { path: { slug } },
          body: { revision, ...values } as never,
        }),
      )) as unknown as Plan;
      client.setQueryData(["planning", slug], result);
      void client.invalidateQueries({ queryKey: ["workspace"] });
      return true;
    } catch (e) {
      setError(errorText(e, "Could not save changes"));
      void client.invalidateQueries({ queryKey: ["planning", slug] });
      return false;
    } finally {
      setBusy(false);
    }
  }
  return { mutate, busy, error, clearError: () => setError("") };
}

export function slugify(name: string) {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}
