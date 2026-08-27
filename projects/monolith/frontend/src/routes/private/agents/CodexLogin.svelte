<script>
  import { onDestroy } from "svelte";

  let {
    authorizeLabel,
    authorizingLabel,
    copiedLabel,
    unavailableLabel,
    invalidResponseLabel,
    codeLabel,
    openLinkLabel,
    requestNewCodeLabel,
    initialUserCode = null,
    initialVerificationUrl = null,
    onError = () => {},
  } = $props();

  let authorizing = $state(false);
  let copied = $state(false);
  let requestedUserCode = $state(null);
  let requestedVerificationUrl = $state(null);
  const userCode = $derived(requestedUserCode ?? initialUserCode);
  const verificationUrl = $derived(
    requestedVerificationUrl ?? initialVerificationUrl,
  );
  let copiedTimer;

  $effect(() => {
    initialUserCode;
    requestedUserCode = null;
    requestedVerificationUrl = null;
  });

  onDestroy(() => clearTimeout(copiedTimer));

  async function authorize() {
    if (authorizing) return;
    authorizing = true;
    copied = false;
    clearTimeout(copiedTimer);
    onError(null);
    try {
      const response = await fetch("/private/agents/codex-login/start", {
        method: "POST",
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          body?.error ||
            body?.detail ||
            `${unavailableLabel} (HTTP ${response.status})`,
        );
      }
      if (
        typeof body.user_code !== "string" ||
        typeof body.verification_url !== "string"
      ) {
        throw new Error(invalidResponseLabel);
      }

      const previousUserCode = userCode;
      requestedUserCode = body.user_code;
      requestedVerificationUrl = body.verification_url;
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(body.user_code);
          copied = true;
          copiedTimer = setTimeout(() => (copied = false), 1200);
        } catch {
          copied = false;
        }
      }
      if (body.pending !== true || body.user_code !== previousUserCode) {
        window.open(body.verification_url, "_blank", "noopener,noreferrer");
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : unavailableLabel);
    } finally {
      authorizing = false;
    }
  }
</script>

{#if userCode}
  <div class="codex-login-control">
    <code id="codex-device-code" tabindex="0">{userCode}</code>
    {#if verificationUrl}
      <a
        class="primary-action"
        href={verificationUrl}
        target="_blank"
        aria-describedby="codex-device-code"
        rel="noopener noreferrer">{openLinkLabel}</a
      >
    {/if}
    <button
      class="secondary-action"
      type="button"
      disabled={authorizing}
      onclick={authorize}
    >
      {authorizing ? authorizingLabel : requestNewCodeLabel}
    </button>
    {#if copied}<span class="copied" role="status">{copiedLabel}</span>{/if}
  </div>
{:else}
  <button
    class="primary-action"
    type="button"
    disabled={authorizing}
    onclick={authorize}
  >
    {authorizing ? authorizingLabel : authorizeLabel}
  </button>
{/if}

<style>
  .codex-login-control {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: var(--panel-bg);
  }
  .primary-action {
    display: inline-flex;
    align-items: center;
    min-height: 28px;
    padding: 0 9px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--ink-text);
    background: var(--ink);
    font: 600 12px var(--font-mono);
    text-decoration: none;
  }
  .primary-action:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  /* Keep the filled treatment on hover. Swapping to --hover here flips a
     solid dark button to a pale chip mid-gesture, which reads as the control
     losing its state rather than responding to the pointer. */
  .primary-action:hover:not(:disabled) {
    opacity: 0.85;
  }
  .secondary-action {
    min-height: 28px;
    padding: 0 7px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--text);
    background: var(--panel-bg);
    font: 500 11px var(--font-mono);
  }
  .secondary-action:hover:not(:disabled) {
    color: var(--text);
    background: var(--hover);
  }
  button:not(.primary-action):disabled {
    color: var(--muted);
    cursor: wait;
  }
  button.primary-action:disabled {
    color: var(--ink-text);
    cursor: wait;
    opacity: 0.62;
  }
  code {
    color: var(--attn-text);
    font: 600 12px var(--font-mono);
    user-select: text;
  }
  .copied {
    color: var(--muted);
    font-size: 11px;
  }
</style>
