import { createContext, useContext } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "./api-client";
import type { Schemas } from "./api-client";

export type Me = Schemas["Me"];
export type Role = Me["role"];

const RANK: Record<Role, number> = { viewer: 0, member: 1, owner: 2 };

export interface Access {
  me: Me;
  /** Members and owners change tasks, planning and workers. */
  canWrite: boolean;
  /** Owners manage users, invites and agents. */
  canManage: boolean;
}

const AuthContext = createContext<Access | null>(null);
export const AuthProvider = AuthContext.Provider;

export function access(me: Me): Access {
  return {
    me,
    canWrite: RANK[me.role] >= RANK.member,
    canManage: me.role === "owner",
  };
}

export function useAccess(): Access {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAccess outside AuthProvider");
  return value;
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => unwrap(api.GET("/api/v1/auth/me")),
    retry: (count, error) =>
      !(error instanceof ApiError && [401, 403].includes(error.status)) &&
      count < 2,
    staleTime: 60_000,
  });
}

/** Prime the csrftoken cookie that the session middleware checks. */
export async function primeCsrf() {
  await unwrap(api.GET("/api/v1/auth/csrf"));
}

export function useSignOut() {
  const client = useQueryClient();
  return async () => {
    try {
      await primeCsrf();
      await api.POST("/api/v1/auth/logout");
    } finally {
      client.clear();
      window.location.assign("/dashboard/");
    }
  };
}

export const ROLE_LABELS: Record<Role, string> = {
  owner: "Owner",
  member: "Member",
  viewer: "Viewer",
};
