<script>
  const REARM_AFTER_MS = 30_000;

  let { sessionId = null } = $props();
  let armed = true;
  let rearmTimer;

  $effect(() => {
    sessionId;
    armed = true;
    clearTimeout(rearmTimer);
    return () => clearTimeout(rearmTimer);
  });

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

    clearTimeout(rearmTimer);
    rearmTimer = setTimeout(() => {
      armed = true;
    }, REARM_AFTER_MS);

    if (!armed) return;
    armed = false;
    postPrewarm();
  }
</script>

<svelte:window oninput={handleInput} />
