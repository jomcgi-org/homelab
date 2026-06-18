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
   * https://challenges.cloudflare.com (allowed: this app sets no CSP, see the
   * Phase 2 notes).
   *
   * @type {{
   *   siteKey: string,
   *   onAdmitted?: () => void,
   * }}
   */
  let { siteKey, onAdmitted } = $props();

  import { onMount } from "svelte";
  import { createChatSession } from "$lib/public/chat/admission.js";

  const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";

  // null = waiting for a solve; "verifying" = exchanging the token; "ready" =
  // admitted (session cookie set); "error" = admission rejected.
  let status = $state(null);
  let widgetEl;

  async function onSolve(token) {
    status = "verifying";
    try {
      const { ok } = await createChatSession(token);
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
    window.turnstile.render(widgetEl, {
      sitekey: siteKey,
      callback: onSolve,
      "error-callback": () => {
        status = "error";
      },
    });
  }

  onMount(() => {
    if (!siteKey) return;
    if (window.turnstile) {
      renderWidget();
      return;
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
    return () => script.removeEventListener("load", renderWidget);
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
      <p class="turnstile-gate__note">
        Verification failed. Please try again.
      </p>
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
