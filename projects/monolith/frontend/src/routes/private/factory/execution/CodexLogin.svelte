<script>
  import { onDestroy } from "svelte";

  let {
    authorizeLabel,
    authorizingLabel,
    copiedLabel,
    unavailableLabel,
    invalidResponseLabel,
    openLinkLabel,
    requestNewCodeLabel,
    startingLabel,
    environment = "prod",
    initialUserCode = null,
    initialVerificationUrl = null,
    onError = () => {},
    onNotice = () => {},
  } = $props();

  let authorizing = $state(false);
  let copied = $state(false);
  let requestedUserCode = $state(null);
  let requestedVerificationUrl = $state(null);
  let persistenceState = $state(null);
  const savingLogin = $derived(persistenceState === "pending");
  const userCode = $derived(
    persistenceState ? null : requestedUserCode ?? initialUserCode,
  );
  const verificationUrl = $derived(
    requestedVerificationUrl ?? initialVerificationUrl,
  );
  let copiedTimer;
  let disposed = false;

  $effect(() => {
    if (!initialUserCode) return;
    persistenceState = null;
    requestedUserCode = null;
    requestedVerificationUrl = null;
  });

  onDestroy(() => {
    disposed = true;
    clearTimeout(copiedTimer);
  });

  async function checkPersistence() {
    if (authorizing) return;
    authorizing = true;
    onError(null);
    try {
      const query = new URLSearchParams({ grant: "codex-cluster", environment });
      const response = await fetch(
        `/private/factory/execution/codex-login/status?${query}`,
        { signal: AbortSignal.timeout(10000), cache: "no-store" },
      );
      const body = await response.json().catch(() => ({}));
      if (disposed) return;
      if (
        !response.ok ||
        !["none", "pending", "persistence_pending", "granted", "failed"].includes(
          body.state,
        ) ||
        (environment === "dev" &&
          (body.environment !== "dev" || body.grant !== "codex-cluster"))
      ) {
        throw new Error(invalidResponseLabel);
      }
      persistenceState = body.state;
      onNotice(
        body.state === "pending"
          ? "The approved login is still being saved. Check again shortly."
          : body.state === "granted"
            ? "Codex is connected."
            : "Saving the login needs another attempt. Retry saving login.",
      );
    } catch {
      if (!disposed) onError(unavailableLabel);
    } finally {
      authorizing = false;
    }
  }

  async function authorize() {
    if (authorizing) return;
    authorizing = true;
    copied = false;
    clearTimeout(copiedTimer);
    onError(null);
    onNotice(null);
    try {
      if (environment !== "prod" && environment !== "dev") {
        throw new Error("Codex login environment must be prod or dev");
      }
      const query =
        environment === "dev" ? "?grant=codex-cluster&environment=dev" : "";
      const response = await fetch(
        `/private/factory/execution/codex-login/start${query}`,
        {
          method: "POST",
        },
      );
      const body = await response.json().catch(() => ({}));
      if (disposed) return;
      if (!response.ok) {
        throw new Error(
          body?.error ||
            body?.detail ||
            `${unavailableLabel} (HTTP ${response.status})`,
        );
      }
      if (
        environment === "dev" &&
        (body.environment !== "dev" || body.grant !== "codex-cluster")
      ) {
        throw new Error(invalidResponseLabel);
      }
      if (typeof body.retry_after === "number" && body.retry_after > 0) {
        onNotice(startingLabel);
        return;
      }
      if (body.state === "pending" && body.persistence_retry === true) {
        persistenceState = "pending";
        requestedUserCode = null;
        requestedVerificationUrl = null;
        onNotice("Saving the approved login. Check the connection shortly.");
        return;
      }
      if (
        typeof body.user_code !== "string" ||
        typeof body.verification_url !== "string"
      ) {
        throw new Error(invalidResponseLabel);
      }

      const previousUserCode = userCode;
      persistenceState = null;
      requestedUserCode = body.user_code;
      requestedVerificationUrl = body.verification_url;
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(body.user_code);
          if (disposed) return;
          copied = true;
          copiedTimer = setTimeout(() => (copied = false), 1200);
        } catch {
          copied = false;
        }
      }
      if (disposed) return;
      if (body.pending !== true || body.user_code !== previousUserCode) {
        window.open(body.verification_url, "_blank", "noopener,noreferrer");
      }
    } catch (error) {
      if (disposed) return;
      onError(error instanceof Error ? error.message : unavailableLabel);
    } finally {
      authorizing = false;
    }
  }
</script>

{#if userCode}
  <div class="codex-login-control">
    <code id="codex-device-code">{userCode}</code>
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
{:else if persistenceState === "granted"}
  <span role="status">Codex connected</span>
{:else}
  <button
    class="primary-action"
    type="button"
    disabled={authorizing}
    onclick={savingLogin ? checkPersistence : authorize}
  >
    {authorizing
      ? authorizingLabel
      : savingLogin
        ? "Check login status"
        : persistenceState
          ? "Retry saving login"
          : authorizeLabel}
  </button>
{/if}

<style>
  .codex-login-control {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px;
    border: 1px solid var(--line-strong);
    border-radius: 0;
    background: var(--panel-bg);
  }
  .primary-action {
    display: inline-flex;
    align-items: center;
    min-height: 28px;
    padding: 0 9px;
    border: 1px solid var(--line-strong);
    border-radius: 0;
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
    border-radius: 0;
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
