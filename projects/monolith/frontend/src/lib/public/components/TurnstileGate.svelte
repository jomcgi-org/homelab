<script>
  /**
   * Minimal Cloudflare Turnstile admission gate (ADR 005, Phase 2).
   *
   * The deliberate Phase 2 scope: render the challenge with the public site key,
   * and on solve exchange the token for a server-side session via the SSR proxy.
   * It carries NO chat UI: the neo-brutalist chat box / graph overlay is Phase 4,
   * which restyles and places this gate. Keep this a thin shell around
   * `createChatSession` (the testable admission seam).
   *
   * The site key is public by design; the Turnstile *secret* never leaves the
   * FastAPI backend. The challenge script + iframe load from
   * https://challenges.cloudflare.com. The app sets no Content-Security-Policy
   * (a CSP hardening layer is deferred to a later pass).
   *
   * `admit` is the token-exchange seam: it takes the solved Turnstile token and
   * resolves to `{ ok }`. It defaults to `createChatSession` (open a fresh
   * session); the shared-snapshot "fork this chat" flow passes a variant that
   * forks the snapshot instead. Keeping it a prop lets one gate serve both
   * admission paths without duplicating the widget lifecycle.
   *
   * @type {{
   *   siteKey: string,
   *   onAdmitted?: () => void,
   *   admit?: (token: string) => Promise<{ ok: boolean }>,
   * }}
   */
  import { onMount } from "svelte";
  import { createChatSession } from "$lib/public/chat/admission.js";

  let { siteKey, onAdmitted, admit = createChatSession } = $props();

  const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";

  // null = waiting for a solve; "verifying" = exchanging the token; "ready" =
  // admitted (session cookie set); "error" = admission rejected.
  let status = $state(null);
  let widgetEl;
  // The id Turnstile returns from render(). Tracked so we render exactly once
  // per mount and can remove() the widget on unmount, keeping Turnstile's global
  // widget registry clean. Without this, an unmount (NEW CHAT re-gating, or SPA
  // navigation back to this page) orphans the widget and the next render fails
  // with "Cannot find Widget", so the solve callback never fires and no session
  // is ever created.
  let widgetId = null;

  async function onSolve(token) {
    status = "verifying";
    try {
      const { ok } = await admit(token);
      if (ok) {
        status = "ready";
        onAdmitted?.();
      } else {
        status = "error";
      }
    } catch {
      status = "error";
    }
  }

  function renderWidget() {
    if (!window.turnstile || !widgetEl || !siteKey) return;
    // Render at most once per mount. A second render() on the same element is
    // what triggers the "widget already rendered" / orphan errors.
    if (widgetId !== null) return;
    widgetId = window.turnstile.render(widgetEl, {
      sitekey: siteKey,
      callback: onSolve,
      "error-callback": () => {
        status = "error";
      },
    });
  }

  function removeWidget() {
    if (widgetId !== null && window.turnstile) {
      try {
        window.turnstile.remove(widgetId);
      } catch {
        // Widget already gone; nothing to clean up.
      }
    }
    widgetId = null;
  }

  onMount(() => {
    if (!siteKey) return undefined;
    if (window.turnstile) {
      renderWidget();
      // Still remove the widget on unmount so the registry stays clean.
      return removeWidget;
    }
    // Load the challenge script once, then render when it is ready.
    let script = document.querySelector(`script[src="${SCRIPT_SRC}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = SCRIPT_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", renderWidget);
    return () => {
      script.removeEventListener("load", renderWidget);
      removeWidget();
    };
  });
</script>

{#if !siteKey}
  <p class="turnstile-gate__note">Chat is unavailable right now.</p>
{:else}
  <div class="turnstile-gate">
    <div bind:this={widgetEl} class="turnstile-gate__widget"></div>
    {#if status === "verifying"}
      <p class="turnstile-gate__note">Verifying…</p>
    {:else if status === "error"}
      <p class="turnstile-gate__note">Verification failed. Please try again.</p>
    {/if}
  </div>
{/if}

<style>
  .turnstile-gate {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .turnstile-gate__note {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
  }
</style>
