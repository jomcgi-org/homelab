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
        throw new Error(body.error || body.detail || unavailableLabel);
      }
      if (
        typeof body.user_code !== "string" ||
        typeof body.verification_url !== "string"
      ) {
        throw new Error(invalidResponseLabel);
      }

      requestedUserCode = body.user_code;
      requestedVerificationUrl = body.verification_url;
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(userCode);
          copied = true;
          copiedTimer = setTimeout(() => (copied = false), 1200);
        } catch {
          copied = false;
        }
      }
      window.open(body.verification_url, "_blank", "noopener,noreferrer");
    } catch (error) {
      onError(error instanceof Error ? error.message : unavailableLabel);
    } finally {
      authorizing = false;
    }
  }
</script>

<div class="codex-login-control">
  <button type="button" disabled={authorizing} onclick={authorize}>
    {authorizing ? authorizingLabel : authorizeLabel}
  </button>
  {#if userCode}
    <code aria-label={codeLabel} tabindex="0">{userCode}</code>
  {/if}
  {#if verificationUrl}
    <a href={verificationUrl} target="_blank" rel="noopener noreferrer"
      >{openLinkLabel}</a
    >
  {/if}
  {#if copied}<span class="copied" role="status">{copiedLabel}</span>{/if}
</div>

<style>
  .codex-login-control {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  button {
    min-height: 28px;
    padding: 0 9px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--text);
    background: var(--panel-bg);
    font: 600 12px var(--font-mono);
  }
  button:hover:not(:disabled) {
    background: var(--hover);
  }
  button:disabled {
    color: var(--muted);
    cursor: wait;
  }
  code {
    color: var(--attn-text);
    font: 600 12px var(--font-mono);
    user-select: text;
  }
  a {
    color: var(--attn-text);
    font-size: 11px;
  }
  .copied {
    color: var(--muted);
    font-size: 11px;
  }
</style>
