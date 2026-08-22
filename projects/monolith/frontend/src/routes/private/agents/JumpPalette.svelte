<script>
  import { tick } from "svelte";
  import { nodeStateClass } from "./dag.js";
  import { jumpActions, jumpMatches } from "./jump.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { statusClass } from "./status.js";

  let {
    open,
    query = $bindable(""),
    sessions = [],
    runs = [],
    terminalRuns = [],
    inbox = { needsYou: [], running: [] },
    onClose,
    onOpenRun,
    onOpenSession,
    onNewSession,
    onSearchTurns,
  } = $props();

  let panelEl = $state(null);
  let inputEl = $state(null);
  let highlighted = $state(0);
  let wasOpen = false;
  let previousFocus = null;

  const matches = $derived(
    jumpMatches(query, { sessions, runs, terminalRuns, inbox }),
  );
  const actions = $derived(jumpActions(query));
  const rows = $derived.by(() => [
    ...matches.inbox.map((item) => ({ ...item, section: "inbox" })),
    ...matches.earlier.map((item) => ({ ...item, section: "earlier" })),
    ...actions.map((item) => ({ ...item, section: "actions" })),
  ]);
  const noMatches = $derived(
    Boolean(query.trim()) &&
      matches.inbox.length === 0 &&
      matches.earlier.length === 0,
  );
  const hasQuery = $derived(Boolean(query.trim()));
  const historyScope = $derived(
    P.labels.jumpHistoryScope.replace("{count}", String(sessions.length)),
  );
  const activeOptionId = $derived(
    rows[highlighted] ? optionId(highlighted) : undefined,
  );

  $effect(() => {
    query;
    if (open) highlighted = 0;
  });

  $effect(() => {
    if (open && !wasOpen) {
      previousFocus = document.activeElement;
      wasOpen = true;
      tick().then(() => inputEl?.focus({ preventScroll: true }));
    } else if (!open && wasOpen) {
      wasOpen = false;
      const target = previousFocus;
      previousFocus = null;
      tick().then(() => {
        if (document.activeElement === document.body) {
          target?.focus?.({ preventScroll: true });
        }
      });
    }
  });

  $effect(() => {
    highlighted;
    if (open) {
      tick().then(() =>
        document
          .getElementById(optionId(highlighted))
          ?.scrollIntoView({ block: "nearest" }),
      );
    }
  });

  $effect(() => {
    const next = rows.length && highlighted < rows.length ? highlighted : 0;
    if (highlighted !== next) highlighted = next;
  });

  function optionId(index) {
    return `jump-option-${index}`;
  }

  function valueFor(item) {
    if (item.kind === "session") {
      return sessions.find((session) => String(session.id) === String(item.id));
    }
    return [...runs, ...terminalRuns].find(
      (run) => String(run.workflow_id) === String(item.id),
    );
  }

  function shapeClass(run, node) {
    if (
      run?.needs?.kind === "human" &&
      node.state === "blocked" &&
      run.current?.state === "blocked"
    ) {
      return "g-blocked-h";
    }
    return nodeStateClass(node);
  }

  function openInNewTab(item) {
    const url = new URL(window.location.href);
    url.searchParams.delete("run");
    url.searchParams.delete("session");
    url.searchParams.set(item.kind, String(item.id));
    window.open(url.toString(), "_blank", "noopener,noreferrer");
  }

  function activate(item, newTab = false) {
    if (!item) return;
    if (item.kind === "run" || item.kind === "session") {
      if (newTab) {
        openInNewTab(item);
        return;
      }
      onClose();
      if (item.kind === "run") onOpenRun(item.id);
      else onOpenSession(item.id);
      return;
    }
    onClose();
    if (item.kind === "new") onNewSession(query.trim());
    else onSearchTurns(query.trim());
  }

  function moveHighlight(offset) {
    if (!rows.length) return;
    highlighted = (highlighted + offset + rows.length) % rows.length;
  }

  function trapFocus(event) {
    const focusable = [
      ...(panelEl?.querySelectorAll(
        'input, button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? []),
    ];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleKeydown(event) {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      moveHighlight(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveHighlight(-1);
    } else if (event.key === "Enter" && event.shiftKey) {
      event.preventDefault();
      onClose();
      onSearchTurns(query.trim());
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = rows[highlighted];
      const newTab =
        Boolean(event.metaKey || event.ctrlKey) &&
        (item?.kind === "run" || item?.kind === "session");
      activate(item, newTab);
    } else if (event.key === "Tab") {
      trapFocus(event);
    }
  }
</script>

{#if open}
  <button
    class="jump-scrim"
    tabindex="-1"
    type="button"
    aria-label={P.labels.jumpClose}
    onclick={onClose}
  ></button>
  <div
    class="jump-panel"
    bind:this={panelEl}
    role="dialog"
    aria-modal="true"
    aria-label={P.labels.jumpLabel}
    tabindex="-1"
    onkeydown={handleKeydown}
  >
    <div class="jump-input-row">
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="7" cy="7" r="4.25"></circle>
        <path d="m10.25 10.25 3 3"></path>
      </svg>
      <input
        bind:this={inputEl}
        bind:value={query}
        role="combobox"
        aria-label={P.labels.jumpSearchLabel}
        aria-expanded="true"
        aria-controls="jump-options"
        aria-activedescendant={activeOptionId}
        autocomplete="off"
      />
      <kbd>{P.labels.shortcutEscape}</kbd>
    </div>

    <div class="jump-options" id="jump-options" role="listbox">
      {#if matches.inbox.length}
        <div class="jump-section-label">{P.labels.jumpInbox}</div>
        {#each matches.inbox as item, index (`${item.kind}:${item.id}`)}
          {@render jumpRow(item, index)}
        {/each}
      {/if}

      {#if matches.earlier.length || hasQuery}
        <div class="jump-section-label">{P.labels.jumpEarlier}</div>
        {#each matches.earlier as item, index (`${item.kind}:${item.id}`)}
          {@render jumpRow(item, matches.inbox.length + index)}
        {/each}
        {#if hasQuery}
          <div class="jump-history-scope">{historyScope}</div>
        {/if}
      {/if}

      {#if noMatches}
        <div class="jump-empty">{P.labels.jumpNoMatches}</div>
      {/if}

      <div class="jump-section-label">{P.labels.jumpActions}</div>
      {#each actions as item, index (item.id)}
        {@render jumpRow(
          item,
          matches.inbox.length + matches.earlier.length + index,
        )}
      {/each}
    </div>

    <div class="jump-footer">
      <span><kbd>{P.labels.shortcutMove}</kbd> {P.labels.jumpMove}</span>
      <span><kbd>{P.labels.shortcutEnter}</kbd> {P.labels.jumpOpen}</span>
      <span><kbd>{P.labels.shortcutNewTab}</kbd> {P.labels.jumpNewTab}</span>
    </div>
  </div>
{/if}

{#snippet jumpRow(item, index)}
  {@const value = valueFor(item)}
  <button
    id={optionId(index)}
    class:highlighted={highlighted === index}
    class="jump-row"
    type="button"
    role="option"
    tabindex="-1"
    aria-selected={highlighted === index}
    onmouseenter={() => (highlighted = index)}
    onclick={() => activate(item)}
  >
    <span class="jump-mark" aria-hidden="true">
      {#if item.kind === "run"}
        <span class="run-shape-strip">
          {#each value?.shape?.length ? value.shape : [{ key: "run", kind: "work", state: value?.state }] as node, nodeIndex (`${node.key}:${nodeIndex}`)}
            <span
              class:gate={node.kind === "gate"}
              class={`shape-node ${shapeClass(value, node)}`}
            ></span>
          {/each}
        </span>
      {:else if item.kind === "session"}
        <span class={`dot ${statusClass(value)}`}></span>
      {:else if item.kind === "new"}
        <svg viewBox="0 0 16 16">
          <path d="M8 3v10M3 8h10"></path>
        </svg>
      {:else}
        <svg viewBox="0 0 16 16">
          <circle cx="7" cy="7" r="4.25"></circle>
          <path d="m10.25 10.25 3 3"></path>
        </svg>
      {/if}
    </span>
    <span class="jump-title">
      {#if item.segments}
        {#each item.segments as segment}
          {#if segment.hit}<strong>{segment.text}</strong
            >{:else}{segment.text}{/if}
        {/each}
      {:else}
        {item.title}
      {/if}
    </span>
    {#if item.meta}<span class="jump-meta">{item.meta}</span>{/if}
    {#if item.hint}<kbd class="jump-hint">{item.hint}</kbd>{/if}
  </button>
{/snippet}

<style>
  .jump-scrim {
    position: fixed;
    z-index: 20;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: var(--scrim-bg);
  }
  .jump-panel {
    position: fixed;
    z-index: 21;
    top: 120px;
    left: 50%;
    width: 620px;
    max-width: calc(100vw - 32px);
    max-height: calc(100dvh - 152px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    transform: translateX(-50%);
    border: 1px solid var(--line);
    border-radius: 8px;
    color: var(--text);
    background: var(--panel-bg);
    box-shadow: var(--panel-shadow);
  }
  .jump-input-row {
    flex: 0 0 52px;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 14px;
    border-bottom: 1px solid var(--line);
  }
  .jump-input-row svg,
  .jump-mark svg {
    width: 16px;
    height: 16px;
    flex: 0 0 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
  }
  .jump-input-row svg {
    color: var(--muted);
  }
  .jump-input-row input {
    min-width: 0;
    height: 100%;
    flex: 1;
    padding: 0;
    border: 0;
    outline: 0;
    color: var(--text);
    background: transparent;
    font-size: 15px;
  }
  .jump-input-row kbd,
  .jump-hint,
  .jump-footer kbd {
    min-width: 24px;
    padding: 2px 5px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--muted);
    background: var(--page-bg);
    font: 11.5px var(--font-mono);
    text-align: center;
    white-space: nowrap;
  }
  .jump-options {
    min-height: 0;
    overflow-y: auto;
    padding: 6px;
  }
  .jump-section-label {
    padding: 8px 10px 5px;
    color: var(--muted);
    font-size: 11.5px;
    font-weight: 600;
    line-height: 1.2;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .jump-row {
    width: 100%;
    height: 44px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 10px;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--text);
    background: transparent;
    text-align: left;
  }
  .jump-row.highlighted {
    background: var(--page-bg);
  }
  .jump-meta,
  .jump-footer {
    /* The page's .mono is scoped to +page.svelte and cannot reach a child
       component, so the family is set here. */
    font-family: var(--font-mono);
  }
  .jump-mark {
    min-width: 16px;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
  }
  .jump-mark .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
  }
  .jump-mark .dot.running,
  .jump-mark .dot.working {
    background: var(--ok);
  }
  .jump-mark .dot.needs_input {
    background: var(--attn);
  }
  .jump-mark .dot.warn {
    background: var(--err);
  }
  .run-shape-strip {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    overflow: hidden;
  }
  .shape-node {
    width: 6px;
    height: 6px;
    flex: 0 0 6px;
    border-radius: 2px;
    background: currentColor;
  }
  .shape-node.gate {
    transform: rotate(45deg) scale(0.85);
  }
  .jump-title {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    color: var(--text);
    font-size: 14px;
    font-weight: 400;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .jump-title strong {
    font-weight: 600;
  }
  .jump-meta {
    max-width: 190px;
    overflow: hidden;
    color: var(--muted);
    font-size: 12px;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .jump-empty {
    height: 44px;
    display: flex;
    align-items: center;
    padding: 0 36px;
    color: var(--muted);
    font-size: 14px;
  }
  .jump-history-scope {
    padding: 7px 10px 9px 36px;
    color: var(--muted);
    font: 11.5px var(--font-mono);
  }
  .jump-footer {
    min-height: 36px;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 6px 14px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 11.5px;
  }
  .jump-footer span {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .jump-footer kbd {
    min-width: 0;
    padding: 1px 4px;
  }
  @media (max-width: 760px) {
    .jump-panel {
      top: 16px;
      width: calc(100vw - 32px);
      max-width: none;
      max-height: calc(100dvh - 32px);
    }
    .jump-row,
    .jump-empty {
      height: 48px;
    }
    .jump-footer {
      gap: 10px;
      overflow-x: auto;
      white-space: nowrap;
    }
  }
</style>
