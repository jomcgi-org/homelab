import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const config = import.meta.env;
if (
  config.VITE_AUTH_METHOD !== "oidc" ||
  config.VITE_RUNTIME_AUTH_CONFIG !== "false"
) {
  throw new Error("The explorer requires fixed OIDC configuration");
}
export const userManager = new UserManager({
  authority: config.VITE_OIDC_PROVIDER,
  client_id: config.VITE_OIDC_CLIENT_ID,
  redirect_uri: window.location.origin + "/oidc/callback",
  scope: config.VITE_OIDC_SCOPES,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
});

export async function getIdToken() {
  const user = await userManager.getUser();
  if (
    !user?.id_token ||
    user.expired ||
    (user.profile.exp ?? 0) * 1000 <= Date.now()
  ) {
    await userManager.signinRedirect();
    throw new Error("Redirecting to sign in");
  }
  return user.id_token;
}

export async function read(path: string, accept: string, signal?: AbortSignal) {
  const response = await fetch(config.VITE_KUBERNETES_API + path, {
    headers: { Accept: accept, Authorization: `Bearer ${await getIdToken()}` },
    redirect: "error",
    signal,
  });
  if (!response.ok)
    throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
