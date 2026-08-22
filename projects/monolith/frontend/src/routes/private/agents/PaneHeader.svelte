<script>
  import { onDestroy as onComponentDestroy, tick } from "svelte";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import {
    SESSION_VIEW_CONVERSATION,
    SESSION_VIEW_WALKTHROUGH,
  } from "./session-view.js";

  let {
    kind,
    crumbs = [],
    onCrumb = () => {},
    chips,
    children,
    sessionRow = false,
    selectedRun = false,
    sessionId = "",
    sessionView = SESSION_VIEW_CONVERSATION,
    onBackToRun = () => {},
    onChangeView = () => {},
    onDestroy = () => {},
  } = $props();

  let menuOpen = $state(false);
  let destroyArmed = $state(false);
  let copied = $state(false);
  let activeMenuItem = $state("copy");
  let menuEl = $state(null);
  let menuButtonEl = $state(null);
  let copiedTimer;

  function closeMenu(returnFocus = true) {
    if (!menuOpen) return;
    menuOpen = false;
    destroyArmed = false;
    if (returnFocus) menuButtonEl?.focus({ preventScroll: true });
  }

  function menuItems() {
    return [...(menuEl?.querySelectorAll('[role="menuitem"]') ?? [])].filter(
      (item) =>
        !item.disabled &&
        !item.hidden &&
        (!item.classList.contains("mobile-view-item") ||
          (window.matchMedia?.("(max-width: 760px)").matches ??
            window.innerWidth <= 760)),
    );
  }

  function focusMenuItem(item) {
    if (!item) return;
    activeMenuItem = item.dataset.menuItem;
    for (const menuItem of menuEl.querySelectorAll('[role="menuitem"]')) {
      menuItem.tabIndex = menuItem === item ? 0 : -1;
    }
    item.focus({ preventScroll: true });
  }

  async function openMenu() {
    menuOpen = true;
    await tick();
    focusMenuItem(menuItems()[0]);
  }

  function toggleMenu() {
    if (menuOpen) closeMenu();
    else void openMenu();
  }

  function handleKeydown(event) {
    if (!menuOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (event.key === "Tab") {
      queueMicrotask(() => closeMenu(false));
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      return;
    }

    const items = menuItems();
    if (!items.length) return;
    event.preventDefault();
    const current = items.indexOf(document.activeElement);
    let next = 0;
    if (event.key === "End") next = items.length - 1;
    else if (event.key === "ArrowDown") next = (current + 1) % items.length;
    else if (event.key === "ArrowUp") {
      next = (current - 1 + items.length) % items.length;
    }
    focusMenuItem(items[next]);
  }

  function handleDocumentClick(event) {
    if (!menuOpen || menuEl?.contains(event.target)) return;
    // The click already moved focus (into the composer, a turn); do not
    // yank it back to the trigger. Escape still returns focus.
    closeMenu(false);
  }

  async function copyId() {
    destroyArmed = false;
    copied = false;
    clearTimeout(copiedTimer);
    if (!navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(sessionId);
      copied = true;
      copiedTimer = setTimeout(() => (copied = false), 1200);
    } catch {
      copied = false;
    }
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
    destroyArmed = false;
    onChangeView(
      sessionView === SESSION_VIEW_CONVERSATION
        ? SESSION_VIEW_WALKTHROUGH
        : SESSION_VIEW_CONVERSATION,
    );
    closeMenu();
  }

  function backToRun() {
    destroyArmed = false;
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
            <button
              type="button"
              role="menuitem"
              data-menu-item="back"
              tabindex={activeMenuItem === "back" ? 0 : -1}
              onfocus={() => (activeMenuItem = "back")}
              onclick={backToRun}>{P.labels.headerBackToRun}</button
            >
          {/if}
          <button
            type="button"
            role="menuitem"
            data-menu-item="copy"
            tabindex={activeMenuItem === "copy" ? 0 : -1}
            onfocus={() => (activeMenuItem = "copy")}
            onclick={copyId}
            >{copied ? P.labels.copied : P.labels.headerCopyId}</button
          >
          <button
            class="mobile-view-item"
            type="button"
            role="menuitem"
            data-menu-item="view"
            tabindex={activeMenuItem === "view" ? 0 : -1}
            onfocus={() => (activeMenuItem = "view")}
            onclick={chooseAlternateView}
            >{sessionView === SESSION_VIEW_CONVERSATION
              ? P.labels.walkthroughView
              : P.labels.conversationView}</button
          >
          <button
            class="destroy-item"
            type="button"
            role="menuitem"
            data-menu-item="destroy"
            tabindex={activeMenuItem === "destroy" ? 0 : -1}
            onfocus={() => (activeMenuItem = "destroy")}
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
