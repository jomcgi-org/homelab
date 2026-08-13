<script>
  // Minimal Phase 2 mount point for the public-chat admission gate (ADR 005).
  // This proves the Turnstile -> session-cookie path end to end; the
  // neo-brutalist chat box and graph overlay are Phase 4, which will restyle and
  // place the gate (and add the message UI). Intentionally bare for now.
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";

  let { data } = $props();
  let admitted = $state(false);
</script>

<svelte:head>
  <title>Chat · jomcgi.dev</title>
  <meta name="robots" content="noindex" />
</svelte:head>

<main class="chat-shell">
  <h1>Chat</h1>
  {#if admitted}
    <p>You're in. The chat itself isn't open yet.</p>
  {:else}
    <p>Solve the challenge to start chatting.</p>
    <TurnstileGate
      siteKey={data.turnstileSiteKey}
      onAdmitted={() => (admitted = true)}
    />
  {/if}
</main>

<style>
  .chat-shell {
    max-width: 640px;
    margin: 0 auto;
    padding: 48px 24px;
    font-family: var(--mono);
    color: var(--ink);
  }
</style>
