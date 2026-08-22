<script>
  import { onDestroy as onComponentDestroy } from "svelte";
  import { RUN_LEXICON as P } from "./run-lexicon.js";

  let {
    kind,
    crumbs = [],
    onCrumb = () => {},
    chips,
    children,
    sessionRow = false,
    selectedRun = false,
    sessionId = "",
    sessionView = "conversation",
    onBackToRun = () => {},
    onChangeView = () => {},
    onDestroy = () => {},
  } = $props();

  let menuOpen = $state(false);
  let destroyArmed = $state(false);
  let copied = $state(false);
  let menuEl = $state(null);
  let menuButtonEl = $state(null);
  let copiedTimer;

  function closeMenu(returnFocus = true) {
    if (!menuOpen) return;
    menuOpen = false;
    destroyArmed = false;
    if (returnFocus) menuButtonEl?.focus({ preventScroll: true });
  }

  function toggleMenu() {
    if (menuOpen) closeMenu();
    else menuOpen = true;
  }

  function handleKeydown(event) {
    if (event.key !== "Escape" || !menuOpen) return;
    event.preventDefault();
    closeMenu();
  }

  function handleDocumentClick(event) {
    if (!menuOpen || menuEl?.contains(event.target)) return;
    closeMenu();
  }

  function copyId() {
    navigator.clipboard?.writeText(sessionId);
    copied = true;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => (copied = false), 1200);
  }

  function confirmDestroy() {
    if (!destroyArmed) {
      destroyArmed = true;
      return;
    }
    onDestroy();
    closeMenu(false);
  }

  function chooseAlternateView() {
    onChangeView(
      sessionView === "conversation" ? "walkthrough" : "conversation",
    );
    closeMenu();
  }

  function backToRun() {
    onBackToRun();
    closeMenu();
  }

  onComponentDestroy(() => clearTimeout(copiedTimer));
</script>

<svelte:window onkeydown={handleKeydown} />
<svelte:document onclick={handleDocumentClick} />

{#if sessionRow}
  <div class="session-pane-header">
    {#if crumbs.length > 1}
      <nav class="session-crumbs" aria-label={P.labels.location}>
        {#each crumbs as crumb, i}
          {#if crumb.to}
            <button
              class="crumb-link"
              type="button"
              onclick={() => onCrumb(crumb.to)}>{crumb.label}</button
            >
          {:else}
            <span class="crumb-current" aria-current="page">{crumb.label}</span>
          {/if}
          {#if i < crumbs.length - 1}
            <span class="crumb-sep" aria-hidden="true">{P.punct.chevron}</span>
          {/if}
        {/each}
      </nav>
    {/if}
    {@render children?.()}
    <div class="session-menu" bind:this={menuEl}>
      <button
        class="session-menu-button"
        type="button"
        aria-label={P.labels.sessionMenu}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onclick={toggleMenu}
        bind:this={menuButtonEl}
      >
        <svg viewBox="0 0 18 18" aria-hidden="true">
          <circle cx="4" cy="9" r="1.25"></circle>
          <circle cx="9" cy="9" r="1.25"></circle>
          <circle cx="14" cy="9" r="1.25"></circle>
        </svg>
      </button>
      {#if menuOpen}
        <div class="session-menu-popover" role="menu">
          {#if selectedRun}
            <button type="button" role="menuitem" onclick={backToRun}
              >{P.labels.headerBackToRun}</button
            >
          {/if}
          <button type="button" role="menuitem" onclick={copyId}
            >{copied ? P.labels.copied : P.labels.headerCopyId}</button
          >
          <button
            class="mobile-view-item"
            type="button"
            role="menuitem"
            onclick={chooseAlternateView}
            >{sessionView === "conversation"
              ? P.labels.walkthroughView
              : P.labels.conversationView}</button
          >
          <button
            class="destroy-item"
            type="button"
            role="menuitem"
            onclick={confirmDestroy}
            >{destroyArmed
              ? P.labels.destroyConfirmMenu
              : P.labels.headerDestroySession}</button
          >
        </div>
      {/if}
    </div>
  </div>
{:else}
  {#if crumbs.length}
    <nav class="crumbs" aria-label={P.labels.location}>
      {#each crumbs as crumb, i}
        {#if crumb.to}
          <button
            class="crumb-link"
            type="button"
            onclick={() => onCrumb(crumb.to)}>{crumb.label}</button
          >
        {:else}
          <span class="crumb-current" aria-current="page">{crumb.label}</span>
        {/if}
        {#if i < crumbs.length - 1}
          <span class="crumb-sep" aria-hidden="true">/</span>
        {/if}
      {/each}
    </nav>
  {/if}
  <div class="kind-row">
    <span class="kind">{kind}</span>
    {@render chips?.()}
  </div>
{/if}

<style>
  .session-pane-header {
    display: contents;
  }
  .session-crumbs {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--muted);
    font: 13px var(--font-ui);
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .session-crumbs .crumb-link {
    padding: 0;
    border: 0;
    color: inherit;
    background: transparent;
    font: inherit;
  }
  .session-crumbs .crumb-link:first-child {
    text-transform: capitalize;
  }
  .session-crumbs .crumb-link:hover {
    color: var(--text);
  }
  .session-crumbs .crumb-sep {
    margin: 0 6px;
  }
  .session-menu {
    position: relative;
    flex: 0 0 auto;
  }
  .session-menu-button {
    width: 36px;
    height: 36px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    color: var(--text-soft);
    background: transparent;
  }
  .session-menu-button:hover,
  .session-menu-button[aria-expanded="true"] {
    background: var(--hover);
  }
  .session-menu-button:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  .session-menu-button svg {
    width: 18px;
    height: 18px;
    fill: currentColor;
  }
  .session-menu-popover {
    position: absolute;
    z-index: 5;
    top: calc(100% + 4px);
    right: 0;
    width: 176px;
    overflow: hidden;
    padding: 4px;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-bg);
    box-shadow: var(--panel-shadow);
  }
  .session-menu-popover button {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    padding: 0 10px;
    border: 0;
    color: var(--text);
    background: transparent;
    text-align: left;
    font-size: 13px;
  }
  .session-menu-popover button:hover,
  .session-menu-popover button:focus-visible {
    background: var(--hover);
  }
  .session-menu-popover button:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: -2px;
  }
  .session-menu-popover .destroy-item {
    color: var(--err);
  }
  .session-menu-popover .mobile-view-item {
    display: none;
  }
  @media (max-width: 760px) {
    .session-crumbs {
      display: none;
    }
    .session-menu-button {
      width: 44px;
      height: 44px;
    }
    .session-menu-popover .mobile-view-item {
      display: flex;
    }
  }
</style>
