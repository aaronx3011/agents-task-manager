// Role rules shared with the web app (frontend/src/auth.tsx): viewers read,
// members also write, owners also manage users, invites and agents.
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, unwrap } from "@/api/client";
import { useSession } from "@/auth/session";

export interface Access {
  role: "viewer" | "member" | "owner" | null;
  username: string;
  canWrite: boolean;
  canManage: boolean;
}

/** The signed-in user's current role, refreshed from /api/v1/auth/me. */
export function useAccess(): Access {
  const { api, user, expire } = useSession();
  const me = useQuery({
    queryKey: ["me"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/auth/me")),
    staleTime: 60_000,
    retry: (count, error) =>
      !(error instanceof ApiError && [401, 403].includes(error.status)) && count < 2,
  });
  useEffect(() => {
    // A revoked token or deactivated user signs the device out.
    if (me.error instanceof ApiError && me.error.status === 401) void expire();
  }, [me.error, expire]);
  const current = me.data ?? user;
  const role = (current?.role ?? null) as Access["role"];
  return {
    role,
    username: current?.username ?? "",
    canWrite: role === "member" || role === "owner",
    canManage: role === "owner",
  };
}
