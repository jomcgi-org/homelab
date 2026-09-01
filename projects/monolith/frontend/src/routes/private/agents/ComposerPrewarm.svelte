<script>
  // Keepalive cadence: the first keystroke wakes the guest immediately, then a
  // 15s interval keeps it warm while typing continues, and the interval stops
  // 30s after the last input event. Paired with the 300s session idle window
  // the guest stays hot through composition and for five minutes after the
  // last keystroke. The backend rate-limits to one wake per 10s per session,
  // so the 15s cadence always passes.
  const KEEPALIVE_MS = 15_000;
  const STOP_AFTER_MS = 30_000;

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
    stopTimer = setTimeout(stopKeepalive, STOP_AFTER_MS);

    if (keepaliveTimer !== undefined) return;
    postPrewarm();
    keepaliveTimer = setInterval(postPrewarm, KEEPALIVE_MS);
  }
</script>

<svelte:window oninput={handleInput} />
