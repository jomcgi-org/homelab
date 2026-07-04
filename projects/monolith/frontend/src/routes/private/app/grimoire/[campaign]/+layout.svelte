<script>
  import { setContext } from "svelte";
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import {
    apiFetch,
    resolveViewpoint,
    rememberViewpoint,
    rememberCampaign,
    libraryHref,
    entitiesHref,
  } from "$lib/grimoire/api.js";
  import Omnibox from "$lib/grimoire/Omnibox.svelte";
  import Shell from "$lib/grimoire/Shell.svelte";
  import "$lib/grimoire/theme.css";

  let { children } = $props();

  const campaignId = $derived($page.params.campaign);
  const viewpoint = $derived(resolveViewpoint($page.url));

  let campaigns = $state([]);
  let characters = $state([]);
  // Drives the two-pane Shell. matchMedia keeps it in sync with the 880px
  // breakpoint without a resize-listener storm; ssr=false so window is safe.
  // Seeded from the initial match (not false) so the first paint already picks
  // the right frame, avoiding a mobile->desktop flash and a wasted list fetch.
  let isDesktop = $state(window.matchMedia("(min-width: 880px)").matches);

  $effect(() => {
    const mq = window.matchMedia("(min-width: 880px)");
    const update = () => (isDesktop = mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  });

  // Shared state for child routes (character list for grant UIs, the campaign
  // list, and the fetch helper) via context with reactive getters.
  setContext("grimoire", {
    apiFetch,
    get campaigns() {
      return campaigns;
    },
    get characters() {
      return characters;
    },
    get campaignId() {
      return campaignId;
    },
    get viewpoint() {
      return viewpoint;
    },
    get isDesktop() {
      return isDesktop;
    },
    reloadCharacters: () => loadCharacters(campaignId),
  });

  async function loadCampaigns() {
    try {
      campaigns = await apiFetch("/campaigns");
    } catch {
      campaigns = [];
    }
  }

  async function loadCharacters(id) {
    if (!id) {
      characters = [];
      return;
    }
    try {
      characters = await apiFetch(`/campaigns/${id}/characters`);
    } catch {
      characters = [];
    }
  }

  $effect(() => {
    loadCampaigns();
  });

  $effect(() => {
    rememberCampaign(campaignId);
    loadCharacters(campaignId);
  });

  function switchCampaign(id) {
    goto(libraryHref(id, viewpoint));
  }

  function switchViewpoint(v) {
    rememberViewpoint(v);
    const url = new URL($page.url);
    url.searchParams.set("as", v);
    goto(url, { keepFocus: true, noScroll: true });
  }
</script>

<div class="grimoire root">
  <header class="topbar">
    <div class="brand-row">
      <a class="grim-title brand" href={libraryHref(campaignId, viewpoint)}>
        Grimoire
      </a>
      {#if campaigns.length > 0}
        <select
          class="campaign-select"
          value={campaignId}
          onchange={(e) => switchCampaign(e.currentTarget.value)}
          aria-label="Campaign"
        >
          {#each campaigns as campaign (campaign.id)}
            <option value={campaign.id}>{campaign.name}</option>
          {/each}
        </select>
      {/if}
    </div>

    <div class="omnibox-slot">
      <Omnibox {campaignId} {viewpoint} />
    </div>

    <nav class="viewpoints" aria-label="Viewpoint">
      <span class="viewpoints-label">as</span>
      <button
        class="vp"
        class:vp--active={viewpoint === "dm"}
        onclick={() => switchViewpoint("dm")}
      >
        DM
      </button>
      {#each characters as character (character.id)}
        <button
          class="vp"
          class:vp--active={viewpoint === character.id}
          onclick={() => switchViewpoint(character.id)}
        >
          {character.character_name}
        </button>
      {/each}
    </nav>

    <nav class="crumbs" aria-label="Sections">
      <a
        class="crumb"
        class:crumb--active={$page.url.pathname === `/app/grimoire/${campaignId}`}
        href={libraryHref(campaignId, viewpoint)}>Library</a
      >
      <a
        class="crumb"
        class:crumb--active={$page.url.pathname.includes("/entities") ||
          $page.url.pathname.includes("/entity/")}
        href={entitiesHref(campaignId, viewpoint)}>Entities</a
      >
    </nav>
  </header>

  <main class="frame">
    <Shell {children} />
  </main>
</div>

<style>
  .root {
    height: 100dvh;
    display: flex;
    flex-direction: column;
    font-family: var(--font-mono);
    color: var(--fg);
    background: var(--bg);
    overflow: hidden;
  }

  .topbar {
    display: grid;
    grid-template-columns: auto 1fr auto;
    grid-template-areas:
      "brand omnibox viewpoints"
      "crumbs crumbs crumbs";
    align-items: center;
    gap: 0.75rem 1rem;
    padding: 0.75rem 1rem;
    border-bottom: var(--border-heavy);
  }

  .brand-row {
    grid-area: brand;
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .brand {
    font-size: 1.25rem;
    color: var(--grim-accent);
    text-decoration: none;
  }

  .campaign-select {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    min-height: 2.25rem;
    padding: 0.3rem 0.4rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    max-width: 9rem;
  }

  .omnibox-slot {
    grid-area: omnibox;
    justify-self: stretch;
    min-width: 0;
  }

  .viewpoints {
    grid-area: viewpoints;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    justify-content: flex-end;
    /* Breathing room: separate the switcher cluster from the omnibox and keep
     * it off the right edge. */
    padding-left: 0.85rem;
    margin-left: 0.25rem;
    border-left: var(--border-thin);
  }

  .viewpoints-label {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
    margin-right: 0.15rem;
  }

  .vp {
    font-family: var(--font-mono);
    font-size: 0.72rem;
    min-height: 2.25rem;
    padding: 0.3rem 0.6rem;
    background: var(--bg);
    color: var(--fg-secondary);
    border: var(--border-thin);
    cursor: pointer;
  }

  .vp--active {
    background: var(--grim-accent);
    border-color: var(--grim-accent);
    color: var(--grim-on-accent);
  }

  .crumbs {
    grid-area: crumbs;
    display: flex;
    gap: 1rem;
  }

  .crumb {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg-tertiary);
    padding-bottom: 0.1rem;
    border-bottom: 2px solid transparent;
  }

  .crumb--active {
    color: var(--fg);
    border-bottom-color: var(--grim-accent);
  }

  .frame {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  /* Desktop: give the omnibox a comfortable center column and let the top bar
   * breathe on one row. */
  @media (min-width: 880px) {
    .topbar {
      grid-template-columns: auto minmax(16rem, 34rem) 1fr;
      grid-template-areas: "brand omnibox viewpoints" "crumbs crumbs crumbs";
      column-gap: 1.5rem;
    }
  }

  /* Narrow phones: stack the omnibox under the brand/viewpoint row so nothing
   * gets crushed. */
  @media (max-width: 600px) {
    .topbar {
      grid-template-columns: 1fr auto;
      grid-template-areas:
        "brand viewpoints"
        "omnibox omnibox"
        "crumbs crumbs";
    }
    .viewpoints {
      justify-content: flex-end;
    }
  }
</style>
