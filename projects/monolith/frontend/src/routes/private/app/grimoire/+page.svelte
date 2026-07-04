<script>
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import {
    apiFetch,
    lastCampaignId,
    rememberCampaign,
    lastViewpoint,
    libraryHref,
  } from "$lib/grimoire/api.js";
  import "$lib/grimoire/theme.css";

  let campaigns = $state([]);
  let loading = $state(true);
  let error = $state("");

  let newName = $state("");
  let newDm = $state("");
  let creating = $state(false);
  let createError = $state("");

  const viewpoint = () => lastViewpoint() || "dm";

  function enter(id) {
    rememberCampaign(id);
    goto(libraryHref(id, viewpoint()));
  }

  onMount(async () => {
    try {
      campaigns = await apiFetch("/campaigns");
      const stored = lastCampaignId();
      const target =
        campaigns.find((c) => c.id === stored)?.id ?? campaigns[0]?.id;
      // Redirect straight into the remembered (or first) campaign; the picker
      // only shows when there is a genuine choice to make or nothing yet.
      if (target && campaigns.length === 1) {
        enter(target);
        return;
      }
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  });

  async function createCampaign() {
    if (!newName.trim()) return;
    creating = true;
    createError = "";
    try {
      const campaign = await apiFetch("/campaigns", {
        method: "POST",
        body: JSON.stringify({
          name: newName.trim(),
          dm_name: newDm.trim() || null,
        }),
      });
      enter(campaign.id);
    } catch (e) {
      createError = e.message;
    } finally {
      creating = false;
    }
  }
</script>

<div class="grimoire picker">
  <header class="head">
    <h1 class="grim-title brand">Grimoire</h1>
    <p class="tagline">An arcane ledger for your table.</p>
  </header>

  {#if loading}
    <div class="skeleton-list">
      <div class="skeleton"></div>
      <div class="skeleton"></div>
    </div>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else}
    {#if campaigns.length > 0}
      <section class="block">
        <h2 class="label">choose a campaign</h2>
        <ul class="campaign-list">
          {#each campaigns as campaign (campaign.id)}
            <li>
              <button class="campaign-row" onclick={() => enter(campaign.id)}>
                <span class="grim-title campaign-name">{campaign.name}</span>
                {#if campaign.dm_name}
                  <span class="campaign-dm">DM: {campaign.dm_name}</span>
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="block">
      <h2 class="label">new campaign</h2>
      <div class="form">
        <input
          class="text-input"
          placeholder="campaign name"
          bind:value={newName}
        />
        <input
          class="text-input"
          placeholder="dm name (optional)"
          bind:value={newDm}
        />
        <button
          class="btn"
          disabled={creating || !newName.trim()}
          onclick={createCampaign}
        >
          create
        </button>
      </div>
      {#if createError}
        <p class="status status--error">{createError}</p>
      {/if}
    </section>
  {/if}
</div>

<style>
  .picker {
    min-height: 100dvh;
    background: var(--bg);
    color: var(--fg);
    font-family: var(--font-mono);
    padding: clamp(1.5rem, 6vw, 5rem) 1.5rem;
    max-width: 46rem;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 2rem;
  }

  .brand {
    font-size: clamp(2rem, 8vw, 3rem);
    color: var(--grim-accent);
  }

  .tagline {
    color: var(--muted);
    font-size: 0.85rem;
    margin-top: 0.35rem;
  }

  .label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    margin-bottom: 0.75rem;
  }

  .campaign-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .campaign-row {
    width: 100%;
    min-height: 3rem;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.75rem 1rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    cursor: pointer;
  }

  .campaign-row:hover {
    border-color: var(--grim-accent);
  }

  .campaign-name {
    font-size: 1.15rem;
  }

  .campaign-dm {
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  .form {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
  }

  .text-input {
    font-family: var(--font-mono);
    font-size: 0.9rem;
    min-height: 2.75rem;
    padding: 0.5rem 0.65rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    flex: 1 1 12rem;
  }

  .btn {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    min-height: 2.75rem;
    padding: 0.5rem 1rem;
    background: var(--grim-accent);
    color: #fff;
    border: none;
    cursor: pointer;
  }

  .btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
  }

  .skeleton-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .skeleton {
    height: 3rem;
    background: linear-gradient(
      90deg,
      var(--surface) 25%,
      transparent 37%,
      var(--surface) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
  }

  @keyframes shimmer {
    0% {
      background-position: 100% 0;
    }
    100% {
      background-position: 0 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton {
      animation: none;
    }
  }
</style>
