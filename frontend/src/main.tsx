import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { access, AuthProvider, useMe } from "./auth";
import { InviteScreen, LoginScreen } from "./AuthScreens";
import { ApiError } from "./api-client";
import { Loading } from "./ui";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5000, retry: 1, refetchOnWindowFocus: true },
  },
});

function Gate() {
  const me = useMe();
  if (window.location.pathname.replace(/\/$/, "") === "/invite")
    return <InviteScreen />;
  if (me.isPending) return <Loading label="Checking your session…" />;
  if (me.isError) {
    if (me.error instanceof ApiError && [401, 403].includes(me.error.status))
      return <LoginScreen />;
    return <LoginScreen expired />;
  }
  return (
    <AuthProvider value={access(me.data)}>
      <App />
    </AuthProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <Gate />
  </QueryClientProvider>,
);
