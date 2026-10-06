<script>
  import { CONTENT_STATES, requireKind, requireText } from "./contracts.js";

  let {
    title,
    headingLevel = 2,
    state = "ready",
    message,
    children,
    footer,
  } = $props();
  const componentId = $props.id();
  const headingId = `${componentId}-heading`;
  const headingTag = $derived.by(() => {
    if (
      !Number.isInteger(headingLevel) ||
      headingLevel < 2 ||
      headingLevel > 6
    ) {
      throw new RangeError("headingLevel must be an integer from 2 to 6");
    }
    return `h${headingLevel}`;
  });
  const checkedTitle = $derived(requireText(title, "title"));
  const checkedState = $derived(
    requireKind(state, CONTENT_STATES, "content state"),
  );
</script>

<section
  aria-labelledby={headingId}
  aria-busy={checkedState === "loading" ? true : undefined}
  data-state={checkedState}
>
  <header>
    <svelte:element this={headingTag} id={headingId}
      >{checkedTitle}</svelte:element
    >
  </header>
  <div class="content">
    {#if checkedState === "ready"}
      {@render children?.()}
    {:else}
      <p class="state">
        {CONTENT_STATES[checkedState]}{message ? `: ${message}` : ""}
      </p>
    {/if}
  </div>
  {#if footer}<footer>{@render footer()}</footer>{/if}
</section>

<style>
  section {
    min-width: 0;
    color: var(--ds-ink);
    background: var(--ds-surface);
    border: var(--ds-border-weight) solid var(--ds-line-strong);
    border-radius: var(--ds-radius);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  header,
  footer {
    padding: var(--ds-space-sm) var(--ds-space-md);
  }
  header {
    border-bottom: var(--ds-border-weight) solid var(--ds-line);
  }
  header > :is(h2, h3, h4, h5, h6) {
    margin: 0;
    font-size: 1.125rem;
    line-height: 1.4;
    font-family: var(--ds-font-mono);
  }
  .content {
    min-width: 0;
    padding: var(--ds-space-md);
  }
  footer {
    border-top: var(--ds-border-weight) solid var(--ds-line);
  }
  .state {
    margin: 0;
    color: var(--ds-ink-muted);
  }
  [data-state="error"] .state {
    color: var(--ds-err);
  }
</style>
