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
  // The proxy opens the next window one hour after the ten-minute window starts.
  const opens = next ?? (windowEnd ? windowEnd + 50 * 60_000 : undefined);
  nextWindow.value = opens ? new Date(opens).toISOString() : "";
  sessionStorage.setItem(closedKey, nextWindow.value);
  session.abort();
  await userManager.removeUser();
}

function tick() {
  clearTimeout(timer);
  if (nextWindow.value !== null) return;
  const now = Date.now();
  const deadline = Math.min(windowEnd || Infinity, tokenEnd);
  if (deadline <= now) {
    void closeWindow();
    return;
  }
  if (windowEnd) {
    const seconds = Math.ceil((windowEnd - now) / 1000);
    countdown.value =
      Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  }
  if (Number.isFinite(deadline))
    timer = setTimeout(tick, Math.min(deadline - now, 1000));
}

export async function restart() {
  await userManager.signinRedirect();
}

export function completeSignin() {
  sessionStorage.removeItem(closedKey);
  sessionStorage.removeItem(endKey);
}

export async function getIdToken() {
  const user = await userManager.getUser();
  if (user)
    tokenEnd = user.expired
      ? 0
      : Math.min(user.profile.exp ?? 0, user.expires_at ?? Infinity) * 1000;
  tick();
  if (nextWindow.value !== null) throw new Error("Explorer session ended");
  if (!user?.id_token) {
    await userManager.signinRedirect();
    throw new Error("Redirecting to sign in");
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
  if (response.status === 401) throw new Error("401 Unauthorized");
  const end = Date.parse(response.headers.get("X-Explore-Window-End") ?? "");
  const next = Date.parse(response.headers.get("X-Explore-Next-Window") ?? "");
  if (response.status === 403 && Number.isFinite(next)) await closeWindow(next);
  else if (Number.isFinite(end)) {
    windowEnd = end;
    sessionStorage.setItem(endKey, String(end));
    tick();
  }
  session.signal.throwIfAborted();
  if (!response.ok)
    throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
