<script>
  import { makeStopRequest, stopInFlight } from "./stop-control.js";

  let {
    enabled = false,
    identity = null,
    status = null,
    onStop = () => {},
  } = $props();

  const busy = $derived(stopInFlight(status));
  const label = $derived(
    status?.outcome === "pending" || status?.outcome === "requested"
      ? "Stopping…"
      : "Stop",
  );
  const messages = {
    pending: "Stop request pending",
    requested: "Stop requested, waiting for the turn result",
    confirmed: "Turn stopped",
    completed: "Turn completed before Stop took effect",
    failed: "Stop failed",
    unknown: "Stop outcome unknown",
  };

  function stop() {
    const request = makeStopRequest(identity);
    if (!request || busy) return;
    onStop(request);
  }
</script>

{#if enabled && (identity || status)}
  <div class="stop-control" data-outcome={status?.outcome ?? "idle"}>
    {#if status?.outcome}
      <span class="stop-state" role="status">
        {messages[status.outcome] ?? messages.unknown}
      </span>
    {/if}
    {#if identity}
      <button type="button" class="stop-button" disabled={busy} onclick={stop}
        >{label}</button
      >
    {/if}
  </div>
{/if}

<style>
  .stop-control {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    padding: 8px 28px 0;
    color: var(--muted);
    font-size: 12px;
  }

  .stop-state {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stop-control[data-outcome="confirmed"] .stop-state {
    color: var(--ok);
  }

  .stop-control[data-outcome="failed"] .stop-state,
  .stop-control[data-outcome="unknown"] .stop-state {
    color: var(--err);
  }

  .stop-button {
    min-height: 30px;
    padding: 0 12px;
    border: 1px solid var(--err-line);
    border-radius: var(--radius-md);
    color: var(--err);
    background: transparent;
    font: 600 12px var(--font-ui);
  }

  .stop-button:hover:not(:disabled) {
    background: var(--err-bg);
  }

  .stop-button:disabled {
    cursor: wait;
    opacity: 0.65;
  }
</style>
