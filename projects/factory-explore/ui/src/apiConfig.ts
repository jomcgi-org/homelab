import { UserManager, WebStorageStateStore } from "oidc-client-ts";

import { ref } from "vue";

const config = import.meta.env;
if (
  config.VITE_AUTH_METHOD !== "oidc" ||
  config.VITE_RUNTIME_AUTH_CONFIG !== "false"
) {
  throw new Error("The explorer requires fixed OIDC configuration");
}
export const userManager = new UserManager({
  automaticSilentRenew: false,
  authority: config.VITE_OIDC_PROVIDER,
  client_id: config.VITE_OIDC_CLIENT_ID,
  redirect_uri: window.location.origin + "/oidc/callback",
  scope: config.VITE_OIDC_SCOPES,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
});

const endKey = "explore.windowEnd";
const closedKey = "explore.nextWindow";
const session = new AbortController();
let windowEnd = Number(sessionStorage.getItem(endKey));
let tokenEnd = Infinity;
let timer: ReturnType<typeof setTimeout>;
export const nextWindow = ref(sessionStorage.getItem(closedKey));
export const countdown = ref("");

async function closeWindow(next?: number) {
  if (nextWindow.value !== null) return;
  clearTimeout(timer);
  nextWindow.value = next === undefined ? "" : new Date(next).toISOString();
  sessionStorage.setItem(closedKey, nextWindow.value);
  session.abort();
  await userManager.removeUser();
}

export function updateClock() {
  clearTimeout(timer);
  if (nextWindow.value !== null) return;
  const now = Date.now();
  const deadline = Math.min(windowEnd || Infinity, tokenEnd);
  if (deadline <= now) {
    // The proxy starts a new hour 50 minutes after the ten-minute window ends.
    // A token that expires first still ends the session, and the next window
    // still opens 50 minutes after the known window end.
    void closeWindow(windowEnd > 0 ? windowEnd + 50 * 60_000 : undefined);
    return;
  }
  if (windowEnd) {
    const seconds = Math.ceil((windowEnd - now) / 1000);
    countdown.value =
      Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  }
  if (Number.isFinite(deadline))
    timer = setTimeout(updateClock, Math.min(deadline - now, 1000));
}

export async function restart() {
  await userManager.signinRedirect();
}

export function completeSignin() {
  sessionStorage.removeItem(closedKey);
  sessionStorage.removeItem(endKey);
}

export async function getIdToken({ initialVisit = false } = {}) {
  const user = await userManager.getUser();
  if (user)
    tokenEnd = user.expired
      ? 0
      : Math.min(user.profile.exp ?? 0, user.expires_at ?? Infinity) * 1000;
  updateClock();
  if (nextWindow.value !== null) throw new Error("Explorer session ended");
  if (!user?.id_token) {
    if (initialVisit && !user && !windowEnd) {
      await userManager.signinRedirect();
      throw new Error("Redirecting to sign in");
    }
    await closeWindow();
    throw new Error("Explorer session ended");
  }
  return user.id_token;
}

export async function read(path: string, accept: string, signal?: AbortSignal) {
  const response = await fetch(config.VITE_KUBERNETES_API + path, {
    headers: { Accept: accept, Authorization: `Bearer ${await getIdToken()}` },
    redirect: "error",
    signal: signal ? AbortSignal.any([signal, session.signal]) : session.signal,
  });
  session.signal.throwIfAborted();
  // explore-proxy sets X-Explore-Window-End to the current time before it
  // authenticates the token, and a 401 returns that placeholder. It is not
  // the caller's window, so a 401 keeps the latest authenticated window end.
  if (response.status === 401) throw new Error("401 Unauthorized");
  const end = Date.parse(response.headers.get("X-Explore-Window-End") ?? "");
  const next = Date.parse(response.headers.get("X-Explore-Next-Window") ?? "");
  if (response.status === 403 && Number.isFinite(next)) await closeWindow(next);
  else if (Number.isFinite(end)) {
    windowEnd = end;
    sessionStorage.setItem(endKey, String(end));
    updateClock();
  }
  session.signal.throwIfAborted();
  if (!response.ok)
    throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
