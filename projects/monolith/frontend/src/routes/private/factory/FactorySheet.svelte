<script>
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import "$lib/public/styles/technical-drawing.css";

  let { view = "factory", waiting = 0, className = "", children } = $props();
  const views = $derived([
    { href: "/factory", label: "factory", key: "factory" },
    {
      href: "/factory/escalations",
      label: `escalations${waiting ? ` (${waiting})` : ""}`,
      key: "escalations",
    },
    { href: "/factory/execution", label: "work", key: "execution" },
    {
      href: "/factory/execution/drain",
      label: "knowledge extraction queue",
      key: "drain",
    },
  ]);
  // One order on every view: a tab that moved when it was selected read as
  // a different page's strip, so a thumb learned the row twice.
  const ordered = $derived(views);
</script>

{#snippet trail()}
  <Trail
    root={{ label: "dashboard", href: "/" }}
    crumbs={[{ label: "factory", href: "/factory" }]}
    current={view === "factory"
      ? ""
      : view === "escalations"
        ? "escalations"
        : "Work Item"}
  />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet factory-sheet {className}">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <!-- SiteIndex lists public routes that do not exist on the private host.
             Keep this index private, with the existing factory destinations. -->
        <nav class="index strip view-tabs" aria-label="Factory views">
          <div class="index-list">
            {#each ordered as link (link.key)}
              {@const active =
                link.key === (view === "work-item" ? "factory" : view)}
              <a
                href={link.href}
                class:active
                class:here={active}
                aria-current={active ? "page" : undefined}>{link.label}</a
              >
            {/each}
          </div>
        </nav>
      </aside>
      <div class="content">{@render children()}</div>
    </div>
  </div>
</main>

<style>
  /* These three document pages scroll with the body. Keep the shared sheet
     overflow rule unchanged for application/execution consumers. Long IDs
     wrap here rather than concealing overflow at the viewport boundary. */
  .factory-sheet {
    overflow-wrap: anywhere;
  }
  .index-list a {
    min-height: 2.75rem;
    align-items: center;
  }
  .factory-sheet :global(a),
  .factory-sheet :global(summary) {
    min-height: 44px;
    min-width: 44px;
  }
  .factory-sheet :global(a) {
    display: inline-flex;
    align-items: center;
    max-width: 100%;
  }
  .factory-sheet :global(button) {
    max-width: 100%;
    border-radius: 0;
  }
  .factory-sheet :global(input),
  .factory-sheet :global(select),
  .factory-sheet :global(textarea) {
    min-width: 0;
    max-width: 100%;
    border-radius: 0;
  }
  .factory-sheet :global(input),
  .factory-sheet :global(select),
  .factory-sheet :global(textarea) {
    min-height: 2.75rem;
    color: var(--ds-ink);
    background: var(--ds-surface);
    font: inherit;
  }
  /* Every disclosure on these pages shows whether it opens: the same plus
     and minus the sheet's index groups wear, parked at the row's end. */
  .factory-sheet :global(details > summary) {
    list-style: none;
  }
  .factory-sheet :global(details > summary::-webkit-details-marker) {
    display: none;
  }
  .factory-sheet :global(details > summary::after) {
    flex: none;
    margin-left: auto;
    padding-left: 0.6em;
    color: var(--ink-2);
    font-family: var(--font-code);
    content: "+";
  }
  .factory-sheet :global(details[open] > summary::after) {
    content: "\2212";
  }
  @media (max-width: 760px) {
    /* The spine only contains this short private index. Sticky belongs on
       the spine, not a child constrained to the spine's own height. The
       strip keeps the sheet's one-row behaviour: it scrolls sideways
       rather than wrapping, so the pinned row is one line tall and the
       page keeps its screen. */
    .factory-sheet .spine {
      position: sticky;
      top: 0;
      z-index: 4;
      margin: 0 -1rem 1.5rem;
      background: var(--sheet);
    }
    .factory-sheet .index.strip {
      position: static;
      margin: 0;
    }
    .factory-sheet .index-list {
      min-width: 0;
      width: 100%;
      gap: 0;
    }
    .factory-sheet .index-list a {
      padding-inline: 0.6em;
    }
  }
</style>
