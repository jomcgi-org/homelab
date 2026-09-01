<script>
  const REARM_AFTER_MS = 30_000;
  const KEEPALIVE_INTERVAL_MS = 15_000;

  let { sessionId = null } = $props();
  let keepaliveTimer;
  let stopTimer;

  $effect(() => {
    sessionId;
    stopKeepalive();
    return () => stopKeepalive();
  });

  function stopKeepalive() {
    clearInterval(keepaliveTimer);
    clearTimeout(stopTimer);
    keepaliveTimer = undefined;
  }

  function postPrewarm() {
    try {
      void fetch("/private/agents/prewarm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      }).catch(() => {});
    } catch {
      // This is an invisible optimization. Synchronous fetch failures are
      // ignored just like rejected requests.
    }
  }

  function handleInput(event) {
    if (sessionId == null || !event.target?.closest?.("form.composer")) return;

    // Every input event pushes the stop deadline out.
    clearTimeout(stopTimer);
    stopTimer = setTimeout(stopKeepalive, REARM_AFTER_MS);

    if (keepaliveTimer !== undefined) return;
    postPrewarm();
    keepaliveTimer = setInterval(postPrewarm, KEEPALIVE_INTERVAL_MS);
  }
</script>

<svelte:window oninput={handleInput} />
