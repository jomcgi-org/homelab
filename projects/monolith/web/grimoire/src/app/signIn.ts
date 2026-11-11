/**
 * Sign-in state for the whole app.
 *
 * The gateway renews tokens in place. When it cannot, a fetch gets a 401. One
 * such answer can be transient, so the state is "reconnecting" until a second
 * one confirms it (see lib/session-expiry.js). Only a full page load can pass
 * the login again, so a confirmed expiry reloads the tab, at most once a
 * minute, after every registered `beforeReload` hook has saved its work.
 */
import { claimReload, expiryCheck } from "../lib/session-expiry.js";

export type SignInState = "live" | "reconnecting" | "signed-out";

type Listener = () => void;
/** Return false to hold the reload, for example while a send is in flight. */
type ReloadHook = () => boolean | void;

const check = expiryCheck();
const listeners = new Set<Listener>();
const hooks = new Set<ReloadHook>();
let state: SignInState = "live";

function set(next: SignInState) {
  if (next === state) return;
  state = next;
  for (const listener of listeners) listener();
}

function expired() {
  for (const hook of hooks) if (hook() === false) return;
  if (claimReload()) {
    location.reload();
    return;
  }
  set("signed-out");
}

export const signIn = {
  /** A 401 or login redirect arrived. */
  signal() {
    if (state !== "signed-out") set("reconnecting");
    if (check.signal()) expired();
  },
  /** Any other answer shows the sign-in works. */
  answered() {
    check.reset();
    set("live");
  },
  /** A request failed without an answer (offline, timeout). */
  unreachable() {
    if (state === "live") set("reconnecting");
  },
  state: () => state,
  subscribe(listener: Listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  /** Runs before an automatic reload; unregister with the returned function. */
  beforeReload(hook: ReloadHook) {
    hooks.add(hook);
    return () => hooks.delete(hook);
  },
  /** Test seam. */
  reset() {
    check.reset();
    hooks.clear();
    state = "live";
  },
};
