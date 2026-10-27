import { useCallback, useEffect, useState } from "react";
import type { LocationState } from "./types";

function read(): LocationState {
  const params = new URLSearchParams(window.location.search);
  const page = Number(params.get("page"));
  return {
    planningTab:
      params.get("planning_tab") === "board"
        ? "board"
        : params.get("planning_tab") === "queue"
          ? "queue"
          : params.get("planning_tab") === "settings"
            ? "settings"
            : "overview",
    newProject: params.get("new_project") === "true",
    section:
      params.get("section") === "planning"
        ? "planning"
        : params.get("section") === "sessions"
          ? "sessions"
          : params.get("section") === "activity"
            ? "activity"
            : params.get("section") === "configuration"
              ? "configuration"
              : params.get("section") === "opencodes"
                ? "opencodes"
                : "tasks",
    worker: params.get("worker") || "",
    session: params.get("session") || "",
    filters: {
      project: params.get("project") || "",
      q: params.get("q") || "",
      claimed_by: params.get("claimed_by") || "",
      status: params.get("status") || "",
      ready: params.get("ready") || "",
    },
    view:
      params.get("view") === "list" || params.get("status") === "cancelled"
        ? "list"
        : "kanban",
    ordering: params.get("ordering") || "priority",
    page: Number.isSafeInteger(page) && page > 0 ? page : 0,
    task: params.get("task") || "",
  };
}
export function useLocationState() {
  const [location, setLocation] = useState(read);
  useEffect(() => {
    const changed = () => setLocation(read());
    window.addEventListener("popstate", changed);
    return () => window.removeEventListener("popstate", changed);
  }, []);
  const navigate = useCallback(
    (
      changes: Record<string, string | number | null>,
      push = false,
      modalEntry = false,
    ) => {
      const url = new URL(window.location.href);
      Object.entries(changes).forEach(([key, value]) => {
        if (value === "" || value === null || (key === "page" && value === 0))
          url.searchParams.delete(key);
        else url.searchParams.set(key, String(value));
      });
      const state = {
        ...window.history.state,
        taskModalEntry: modalEntry || window.history.state?.taskModalEntry,
      };
      if (push) window.history.pushState(state, "", url);
      else window.history.replaceState(state, "", url);
      setLocation(read());
    },
    [],
  );
  return { location, navigate };
}
