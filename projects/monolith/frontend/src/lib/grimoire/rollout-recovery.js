import { getContext, onMount, tick } from "svelte";

export const RECOVERY = Symbol("grimoire-rollout-recovery");
export const RECOVERY_KEY = "grimoire-rollout-drafts:v1";
const MAX_BYTES = 96_000;
const bytes = (value) => new TextEncoder().encode(value).length;
const MAX_AGE = 15 * 60_000;

// Include only authority from the new server projection. Never store a token,
// event feed, read-aloud queue, search result or another viewer's projection.
export function recoveryScope(view) {
  if (!view.user?.id || !view.session?.id || view.session.status === "ended")
    return null;
  return JSON.stringify([
    view.user.id,
    view.campaign.id,
    view.session.id,
    view.campaign.role,
    view.campaign.player_character_id ?? null,
    view.campaign.permissions ?? null,
    view.characters.map((row) => row.id).sort(),
    (view.voices || [])
      .map((row) => [row.speaker_key, Boolean(row.speaker_name)])
      .sort(),
  ]);
}

// Components supply explicit form-only serializers and typed restorers. The
// registry also keeps an unmounted panel's draft while switching tabs.
export function createRecovery({
  enabled,
  scope,
  keepComposer,
  dropComposer,
  reconnect,
}) {
  const panels = new Map();
  let drafts = {};
  let boundScope = null;
  let restoring = false;
  let failed = false;
  const inProgress = new Set();
  function valid() {
    return enabled() && scope() && boundScope === scope();
  }
  async function restorePanel(key, panel) {
    if (!Object.hasOwn(drafts, key) || inProgress.has(key)) return;
    const value = drafts[key];
    inProgress.add(key);
    try {
      await tick();
      if ((await panel.ready()) === false) return;
      const current = () => valid() && panels.get(key) === panel;
      if (!current()) return;
      if ((await panel.restore(value, current)) !== false) delete drafts[key];
    } catch {
      failed = true;
    } finally {
      inProgress.delete(key);
    }
  }
  return {
    isEnabled: enabled,
    transient(error) {
      if (
        !enabled() ||
        !(
          error instanceof TypeError ||
          error.name === "AbortError" ||
          [502, 503, 504].includes(error.status)
        )
      )
        return false;
      reconnect();
      return true;
    },
    register(key, panel) {
      panels.set(key, panel);
      if (restoring) void restorePanel(key, panel);
      return () => {
        try {
          if (valid() && !Object.hasOwn(drafts, key))
            drafts[key] = panel.capture();
        } catch {
          failed = true;
        }
        if (panels.get(key) === panel) panels.delete(key);
      };
    },
    retry(key) {
      const panel = panels.get(key);
      if (panel) void restorePanel(key, panel);
    },
    async restore() {
      if (!enabled()) return;
      boundScope = scope();
      let saved;
      try {
        const raw = sessionStorage.getItem(RECOVERY_KEY);
        sessionStorage.removeItem(RECOVERY_KEY);
        if (!raw) {
          restoring = true;
          return;
        }
        if (bytes(raw) > MAX_BYTES) throw new Error("oversize");
        saved = JSON.parse(raw);
        if (
          saved.version !== 1 ||
          saved.scope !== boundScope ||
          !boundScope ||
          !Number.isFinite(saved.at) ||
          Date.now() < saved.at ||
          Date.now() - saved.at > MAX_AGE ||
          !saved.drafts ||
          typeof saved.drafts !== "object" ||
          Array.isArray(saved.drafts)
        )
          throw new Error("stale");
      } catch {
        dropComposer();
        restoring = true;
        return;
      }
      drafts = saved.drafts;
      restoring = true;
      // Page controls (including the tab) restore first; conditional children
      // register after tick and after their own initial data reads settle.
      if (panels.has("page")) await restorePanel("page", panels.get("page"));
      await tick();
      await Promise.all(
        [...panels]
          .filter(([key]) => key !== "page")
          .map(([key, panel]) => restorePanel(key, panel)),
      );
    },
    save() {
      if (!valid() || failed || inProgress.size) return false;
      try {
        const snapshot = { ...drafts };
        for (const [key, panel] of panels) {
          if (!Object.hasOwn(drafts, key)) snapshot[key] = panel.capture();
        }
        const raw = JSON.stringify(
          { version: 1, at: Date.now(), scope: boundScope, drafts: snapshot },
          (_key, value) => (value === undefined ? null : value),
        );
        if (bytes(raw) > MAX_BYTES || !keepComposer()) return false;
        sessionStorage.setItem(RECOVERY_KEY, raw);
        return sessionStorage.getItem(RECOVERY_KEY) === raw;
      } catch {
        return false;
      }
    },
  };
}

export function registerDraft(key, capture, restore, ready = async () => {}) {
  const recovery = getContext(RECOVERY);
  onMount(() => recovery?.register(key, { capture, restore, ready }));
  return () => recovery?.retry(key);
}

// Reject corrupt fields rather than coerce objects into strings or booleans.
export function fields(value, defaults) {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("invalid draft");
  return Object.fromEntries(
    Object.entries(defaults).map(([key, fallback]) => {
      const item = value[key];
      if (typeof fallback === "number" && item === null) return [key, null];
      if (
        typeof item !== typeof fallback ||
        (typeof item === "number" && !Number.isFinite(item)) ||
        (typeof item === "string" && item.length > 20_000)
      )
        throw new Error("invalid field");
      return [key, item];
    }),
  );
}

// Use at component initialization. Proxy status is checked before JSON parsing;
// callers keep their existing application/auth error handling and never retry.
export function panelRecovery() {
  const recovery = getContext(RECOVERY);
  return {
    fetch(...args) {
      const request = globalThis.fetch(...args);
      if (!recovery?.isEnabled()) return request;
      return request.then((response) => {
        if ([502, 503, 504].includes(response.status)) {
          const error = Object.assign(new Error("Upstream unavailable"), {
            status: response.status,
          });
          if (recovery.transient(error)) throw error;
        }
        return response;
      });
    },
    transient: (error) => recovery?.transient(error) === true,
  };
}

export function pendingRequest(value) {
  if (value === null) return null;
  if (
    typeof value?.signature !== "string" ||
    value.signature.length > MAX_BYTES ||
    typeof value?.id !== "string" ||
    value.id.length > 200
  )
    throw new Error("invalid pending request");
  return { signature: value.signature, id: value.id };
}
