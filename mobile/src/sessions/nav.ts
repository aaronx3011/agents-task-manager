import { router } from "expo-router";

/** Open a session's chat from any tab. */
export function openSession(session: { worker: string; sessionID: string }) {
  router.push({
    pathname: "/sessions/chat",
    params: { worker: session.worker, sid: session.sessionID },
  });
}

export function openWorker(name: string) {
  router.push({ pathname: "/sessions/worker", params: { name } });
}
