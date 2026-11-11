<script>
  import CodexLogin from "../CodexLogin.svelte";
  import { RUN_LEXICON as P } from "../run-lexicon.js";
  import "../execution-sheet.css";

  let { data } = $props();
  const environment = $derived(data.environment);
  const environmentLabel = $derived(
    environment === "dev" ? "Dev conformance" : "Production",
  );
  const statusLabel = $derived(
    {
      none: "Device login required",
      pending: "Device approval pending",
      granted: "Login saved",
      failed: "Device login failed",
      unavailable: "Connection status unavailable",
    }[data.state],
  );
  let errorMessage = $state(null);
  let noticeMessage = $state(null);

  $effect(() => {
    void environment;
    errorMessage = null;
    noticeMessage = null;
  });
</script>

<svelte:head>
  <title>Codex login · {environmentLabel}</title>
</svelte:head>

<main class="td console login-page">
  <a class="back" href="/factory/execution">Back to execution</a>
  <h1>Codex login</h1>
  <nav aria-label="Codex login environment">
    <a
      href="?environment=prod"
      aria-current={environment === "prod" ? "page" : undefined}
      data-sveltekit-reload>Production</a
    >
    <a
      href="?environment=dev"
      aria-current={environment === "dev" ? "page" : undefined}
      data-sveltekit-reload>Dev conformance</a
    >
  </nav>

  <section aria-labelledby="login-environment">
    <h2 id="login-environment">{environmentLabel}</h2>
    <p class="status" role="status">{statusLabel}</p>
    {#if data.state === "granted"}
      <p>A Codex login is stored for {environmentLabel.toLowerCase()}.</p>
    {:else if data.state === "unavailable"}
      <p>Check the connection again to read the login status.</p>
    {:else}
      <p>
        Start device login and approve it on the OpenAI page. Return here to
        check the connection after approval.
      </p>
      {#key environment}
        <CodexLogin
          {environment}
          authorizeLabel={`Authorize Codex for ${environmentLabel.toLowerCase()}`}
          authorizingLabel={P.labels.authorizingCodex}
          copiedLabel={P.labels.copied}
          unavailableLabel={P.labels.codexLoginUnavailable}
          invalidResponseLabel={P.labels.codexLoginInvalidResponse}
          openLinkLabel={P.labels.codexLoginOpenLink}
          requestNewCodeLabel={P.labels.codexLoginRequestNewCode}
          startingLabel={P.labels.codexLoginStarting}
          onError={(message) => (errorMessage = message)}
          onNotice={(message) => (noticeMessage = message)}
        />
      {/key}
    {/if}
    {#if errorMessage}<p class="error" role="alert">{errorMessage}</p>{/if}
    {#if noticeMessage}<p role="status">{noticeMessage}</p>{/if}
    <form method="GET" data-sveltekit-reload>
      <input type="hidden" name="environment" value={environment} />
      <button class="check" type="submit">Check connection</button>
    </form>
  </section>
</main>

<style>
  .login-page {
    max-width: 640px;
    margin: 40px auto;
    padding: 0 20px;
    color: var(--text);
  }
  .back {
    color: var(--muted);
    font-size: 13px;
  }
  h1 {
    margin: 24px 0 16px;
    font-size: 28px;
  }
  nav {
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
  }
  nav a,
  .check {
    padding: 8px 12px;
    border: 1px solid var(--line-strong);
    background: var(--panel-bg);
    color: var(--text);
    font: 500 13px var(--font-mono);
    text-decoration: none;
  }
  nav a[aria-current="page"] {
    background: var(--ink);
    color: var(--ink-text);
  }
  section {
    padding: 24px;
    border: 1px solid var(--line-strong);
    background: var(--panel-bg);
  }
  h2 {
    margin: 0 0 16px;
    font-size: 18px;
  }
  p {
    line-height: 1.5;
  }
  .status {
    font-weight: 600;
  }
  .error {
    color: var(--err);
  }
  form {
    margin-top: 24px;
  }
  .check {
    cursor: pointer;
  }
  a:focus-visible,
  .check:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
</style>
