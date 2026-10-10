<script>
  import { STATUS_KINDS, requireKind, requireText } from "./contracts.js";

  // Live announcements are opt-in. Ordinary values must not announce updates.
  let { kind = "unknown", label, live = false } = $props();
  const checkedKind = $derived(requireKind(kind, STATUS_KINDS, "status kind"));
  const checkedLabel = $derived(requireText(label, "status label"));
</script>

<span
  class="status"
  data-kind={checkedKind}
  role={live ? "status" : undefined}
  aria-live={live ? "polite" : undefined}
>
  <span class="cue" aria-hidden="true">{STATUS_KINDS[checkedKind].cue}</span>
  <span>{checkedLabel}</span>
</span>

<style>
  .status {
    display: inline-flex;
    align-items: baseline;
    gap: 0.4em;
    max-width: 100%;
    color: var(--ds-ink-muted);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  .cue {
    flex: none;
    font-family: var(--ds-font-mono);
  }
  [data-kind="ok"] {
    color: var(--ds-ok);
  }
  [data-kind="warn"] {
    color: var(--ds-warn);
  }
  [data-kind="err"] {
    color: var(--ds-err);
  }
</style>
