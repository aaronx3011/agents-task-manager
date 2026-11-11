import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Platform } from "react-native";
import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";
import { useQueryClient } from "@tanstack/react-query";
import { createApi, unwrap } from "@/api/client";
import type { Api, Schemas } from "@/api/client";

const TOKEN_KEY = "atm.deviceToken";
const SERVER_KEY = "atm.server";
const USER_KEY = "atm.user";

export const DEFAULT_SERVER = process.env.EXPO_PUBLIC_API_URL || "http://localhost:8000";

type User = Schemas["Me"];

interface Session {
  ready: boolean;
  server: string;
  user: User | null;
  api: Api | null;
  signIn: (server: string, username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  /** Forget a token the server no longer accepts (revoked, user deactivated). */
  expire: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

function deviceLabel() {
  const name = Constants.deviceName || `${Platform.OS} device`;
  return `${name} (Agent Ops app)`.slice(0, 100);
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [server, setServer] = useState(DEFAULT_SERVER);
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  // Cached data belongs to one user: drop it whenever the signed-in user changes.
  const client = useQueryClient();

  useEffect(() => {
    // The token lives in the platform keychain/keystore, never in plain storage.
    (async () => {
      const [storedToken, storedServer, storedUser] = await Promise.all([
        SecureStore.getItemAsync(TOKEN_KEY),
        SecureStore.getItemAsync(SERVER_KEY),
        SecureStore.getItemAsync(USER_KEY),
      ]);
      if (storedServer) setServer(storedServer);
      if (storedToken && storedUser) {
        setToken(storedToken);
        setUser(JSON.parse(storedUser) as User);
      }
      setReady(true);
    })();
  }, []);

  const forget = useCallback(async () => {
    await Promise.all([
      SecureStore.deleteItemAsync(TOKEN_KEY),
      SecureStore.deleteItemAsync(USER_KEY),
    ]);
    client.clear();
    setToken(null);
    setUser(null);
  }, [client]);

  const signIn = useCallback(
    async (address: string, username: string, password: string) => {
      const base = address.trim().replace(/\/+$/, "");
      const issued = await unwrap(
        createApi(base).POST("/api/v1/auth/tokens", {
          body: { username, password, name: deviceLabel() },
        }),
      );
      await Promise.all([
        SecureStore.setItemAsync(TOKEN_KEY, issued.token),
        SecureStore.setItemAsync(SERVER_KEY, base),
        SecureStore.setItemAsync(USER_KEY, JSON.stringify(issued.user)),
      ]);
      client.clear();
      setServer(base);
      setToken(issued.token);
      setUser(issued.user);
    },
    [client],
  );

  const signOut = useCallback(async () => {
    if (token) {
      // Revoke on the server so a copied token stops working too.
      await createApi(server, token)
        .POST("/api/v1/auth/tokens/{id}/revoke", { params: { path: { id: "current" } } })
        .catch(() => undefined);
    }
    await forget();
  }, [forget, server, token]);

  const api = useMemo(() => (token ? createApi(server, token) : null), [server, token]);
  const value = useMemo(
    () => ({ ready, server, user, api, signIn, signOut, expire: forget }),
    [ready, server, user, api, signIn, signOut, forget],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}
