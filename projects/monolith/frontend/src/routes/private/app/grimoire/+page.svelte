<script>
  import { onMount } from "svelte";

  const API = "/api/grimoire";
  const LS_CAMPAIGN = "grimoire:campaign";
  const LS_VIEWPOINT = "grimoire:viewpoint";

  const ENTITY_TYPES = [
    "creature",
    "spell",
    "location",
    "npc",
    "faction",
    "deity",
    "item",
  ];
  const GRANT_SCOPES = ["full", "partial", "name_only"];

  // ── Campaigns ──────────────────────────────
  let campaigns = $state([]);
  let campaignId = $state("");
  let campaignsLoading = $state(true);
  let campaignsError = $state("");

  let newCampaignName = $state("");
  let newCampaignDm = $state("");
  let creatingCampaign = $state(false);
  let createCampaignError = $state("");

  // ── Characters / viewpoint ─────────────────
  let characters = $state([]);
  let charactersLoading = $state(false);
  let viewpoint = $state("dm");

  let showAddCharacter = $state(false);
  let newCharName = $state("");
  let newCharPlayer = $state("");
  let addingCharacter = $state(false);
  let addCharacterError = $state("");

  // ── Entity browser ─────────────────────────
  let entities = $state([]);
  let entitiesLoading = $state(false);
  let entitiesError = $state("");
  let typeFilter = $state("");
  let nameQuery = $state("");

  // ── Entity detail ──────────────────────────
  let selectedEntityId = $state(null);
  let entityDetail = $state(null);
  let entityRelationships = $state([]);
  let detailLoading = $state(false);
  let detailError = $state("");

  // ── Search ──────────────────────────────────
  let searchQuery = $state("");
  let searchResults = $state([]);
  let searching = $state(false);
  let searchError = $state("");
  let searchTimer;

  // ── DM grant editor ─────────────────────────
  let grants = $state([]);
  let grantsLoading = $state(false);
  let grantEntityId = $state("");
  let grantCharacterId = $state("");
  let grantScope = $state("full");
  let grantRevealedDetails = $state("");
  let creatingGrant = $state(false);
  let grantError = $state("");

  let isDm = $derived(viewpoint === "dm");
  let entityById = $derived(new Map(entities.map((e) => [e.id, e])));
  let characterById = $derived(new Map(characters.map((c) => [c.id, c])));

  async function apiFetch(path, options = {}) {
    const res = await fetch(`${API}${path}`, {
      ...options,
      headers: options.body
        ? { "Content-Type": "application/json" }
        : undefined,
    });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      throw new Error(body?.detail ?? `request failed (${res.status})`);
    }
    return body;
  }

  async function loadCampaigns() {
    campaignsLoading = true;
    campaignsError = "";
    try {
      campaigns = await apiFetch("/campaigns");
      const stored = localStorage.getItem(LS_CAMPAIGN);
      if (stored && campaigns.some((c) => c.id === stored)) {
        campaignId = stored;
      } else if (campaigns.length > 0) {
        campaignId = campaigns[0].id;
      }
    } catch (e) {
      campaignsError = e.message;
    } finally {
      campaignsLoading = false;
    }
  }

  onMount(loadCampaigns);

  function selectCampaign(id) {
    campaignId = id;
    localStorage.setItem(LS_CAMPAIGN, id);
  }

  async function createCampaign() {
    if (!newCampaignName.trim()) return;
    creatingCampaign = true;
    createCampaignError = "";
    try {
      const campaign = await apiFetch("/campaigns", {
        method: "POST",
        body: JSON.stringify({
          name: newCampaignName.trim(),
          dm_name: newCampaignDm.trim() || null,
        }),
      });
      campaigns = [...campaigns, campaign];
      newCampaignName = "";
      newCampaignDm = "";
      selectCampaign(campaign.id);
      showAddCharacter = true;
    } catch (e) {
      createCampaignError = e.message;
    } finally {
      creatingCampaign = false;
    }
  }

  async function loadCharacters(id) {
    if (!id) {
      characters = [];
      return;
    }
    charactersLoading = true;
    try {
      characters = await apiFetch(`/campaigns/${id}/characters`);
      const stored = localStorage.getItem(LS_VIEWPOINT);
      viewpoint =
        stored === "dm" || characters.some((c) => c.id === stored)
          ? stored
          : "dm";
      showAddCharacter = characters.length === 0;
    } catch {
      characters = [];
      viewpoint = "dm";
    } finally {
      charactersLoading = false;
    }
  }

  function selectViewpoint(v) {
    viewpoint = v;
    localStorage.setItem(LS_VIEWPOINT, v);
    selectedEntityId = null;
    entityDetail = null;
    entityRelationships = [];
  }

  async function addCharacter() {
    if (!campaignId || !newCharName.trim()) return;
    addingCharacter = true;
    addCharacterError = "";
    try {
      const character = await apiFetch(`/campaigns/${campaignId}/characters`, {
        method: "POST",
        body: JSON.stringify({
          character_name: newCharName.trim(),
          player_name: newCharPlayer.trim() || null,
        }),
      });
      characters = [...characters, character];
      newCharName = "";
      newCharPlayer = "";
      showAddCharacter = false;
    } catch (e) {
      addCharacterError = e.message;
    } finally {
      addingCharacter = false;
    }
  }

  async function loadEntities() {
    if (!campaignId) {
      entities = [];
      return;
    }
    entitiesLoading = true;
    entitiesError = "";
    try {
      const params = new URLSearchParams({ as: viewpoint });
      if (typeFilter) params.set("type", typeFilter);
      if (nameQuery.trim()) params.set("q", nameQuery.trim());
      entities = await apiFetch(`/campaigns/${campaignId}/entities?${params}`);
    } catch (e) {
      entitiesError = e.message;
      entities = [];
    } finally {
      entitiesLoading = false;
    }
  }

  async function openEntity(id) {
    if (!campaignId || !id) return;
    selectedEntityId = id;
    detailLoading = true;
    detailError = "";
    entityDetail = null;
    entityRelationships = [];
    const asParam = `as=${encodeURIComponent(viewpoint)}`;
    try {
      const [detail, relationships] = await Promise.all([
        apiFetch(`/campaigns/${campaignId}/entities/${id}?${asParam}`),
        apiFetch(
          `/campaigns/${campaignId}/entities/${id}/relationships?${asParam}`,
        ),
      ]);
      entityDetail = detail;
      entityRelationships = relationships;
    } catch (e) {
      detailError = e.message;
    } finally {
      detailLoading = false;
    }
  }

  async function runSearch() {
    if (!campaignId || !searchQuery.trim()) return;
    searching = true;
    searchError = "";
    try {
      const params = new URLSearchParams({
        as: viewpoint,
        q: searchQuery.trim(),
      });
      searchResults = await apiFetch(`/campaigns/${campaignId}/search?${params}`);
    } catch (e) {
      searchError = e.message;
      searchResults = [];
    } finally {
      searching = false;
    }
  }

  async function loadGrants() {
    if (!campaignId) {
      grants = [];
      return;
    }
    grantsLoading = true;
    try {
      grants = await apiFetch(`/campaigns/${campaignId}/grants`);
    } catch {
      grants = [];
    } finally {
      grantsLoading = false;
    }
  }

  async function createGrant() {
    if (!campaignId || !grantEntityId || !grantCharacterId) return;
    creatingGrant = true;
    grantError = "";
    let revealedDetails = null;
    if (grantScope === "partial" && grantRevealedDetails.trim()) {
      try {
        revealedDetails = JSON.parse(grantRevealedDetails);
      } catch {
        grantError = "revealed details must be valid JSON";
        creatingGrant = false;
        return;
      }
    }
    try {
      await apiFetch(`/campaigns/${campaignId}/grants`, {
        method: "POST",
        body: JSON.stringify({
          entity_id: grantEntityId,
          player_character_id: grantCharacterId,
          grant_scope: grantScope,
          revealed_details: revealedDetails,
        }),
      });
      grantRevealedDetails = "";
      await Promise.all([loadGrants(), loadEntities()]);
    } catch (e) {
      grantError = e.message;
    } finally {
      creatingGrant = false;
    }
  }

  async function updateGrantScope(grant, scope) {
    grantError = "";
    try {
      await apiFetch(`/campaigns/${campaignId}/grants/${grant.id}`, {
        method: "PATCH",
        body: JSON.stringify({ grant_scope: scope }),
      });
      await Promise.all([loadGrants(), loadEntities()]);
    } catch (e) {
      grantError = e.message;
    }
  }

  function entityLabel(id) {
    return entityById.get(id)?.name ?? id;
  }

  function characterLabel(id) {
    return characterById.get(id)?.character_name ?? id;
  }

  function entityBadge(entity) {
    if (isDm) {
      const count = entity.grants?.length ?? 0;
      return count > 0 ? `${count} grant${count === 1 ? "" : "s"}` : "";
    }
    return "revealed_details" in entity && !("source_type" in entity)
      ? "partial"
      : "";
  }

  const DETAIL_SKIP_FIELDS = new Set([
    "id",
    "entity_type",
    "name",
    "grant",
    "grants",
    "revealed_details",
  ]);

  function detailFields(entity) {
    if (!entity) return [];
    return Object.entries(entity).filter(
      ([key, value]) => !DETAIL_SKIP_FIELDS.has(key) && value !== null,
    );
  }

  function formatFieldName(key) {
    return key.replaceAll("_", " ");
  }

  $effect(() => {
    loadCharacters(campaignId);
  });

  $effect(() => {
    campaignId;
    viewpoint;
    typeFilter;
    nameQuery;
    loadEntities();
  });

  $effect(() => {
    if (isDm) loadGrants();
  });

  $effect(() => {
    campaignId;
    viewpoint;
    selectedEntityId = null;
    entityDetail = null;
    entityRelationships = [];
    searchQuery = "";
    searchResults = [];
    searchError = "";
  });

  $effect(() => {
    clearTimeout(searchTimer);
    const q = searchQuery;
    if (!q.trim()) {
      searchResults = [];
      searchError = "";
      return;
    }
    searchTimer = setTimeout(runSearch, 300);
    return () => clearTimeout(searchTimer);
  });
</script>

<div class="root">
  <header class="topbar">
    <h1 class="title">Grimoire</h1>

    {#if campaigns.length > 0}
      <select
        class="campaign-select"
        value={campaignId}
        onchange={(e) => selectCampaign(e.target.value)}
      >
        {#each campaigns as campaign (campaign.id)}
          <option value={campaign.id}>{campaign.name}</option>
        {/each}
      </select>
    {/if}

    <nav class="viewpoints">
      <button
        class="viewpoint-btn"
        class:viewpoint-btn--active={viewpoint === "dm"}
        onclick={() => selectViewpoint("dm")}
      >
        DM
      </button>
      {#each characters as character (character.id)}
        <button
          class="viewpoint-btn"
          class:viewpoint-btn--active={viewpoint === character.id}
          onclick={() => selectViewpoint(character.id)}
        >
          {character.character_name}
        </button>
      {/each}
      <button
        class="viewpoint-btn viewpoint-btn--add"
        onclick={() => (showAddCharacter = !showAddCharacter)}
        disabled={!campaignId}
      >
        + character
      </button>
    </nav>
  </header>

  {#if campaignsLoading}
    <p class="status">loading campaigns...</p>
  {:else if campaignsError}
    <p class="status status--error">{campaignsError}</p>
  {:else if campaigns.length === 0}
    <section class="onboarding">
      <h2 class="section-label">new campaign</h2>
      <div class="form-row">
        <input
          class="text-input"
          placeholder="campaign name"
          bind:value={newCampaignName}
        />
        <input
          class="text-input"
          placeholder="dm name (optional)"
          bind:value={newCampaignDm}
        />
        <button
          class="btn"
          disabled={creatingCampaign || !newCampaignName.trim()}
          onclick={createCampaign}
        >
          create
        </button>
      </div>
      {#if createCampaignError}
        <p class="status status--error">{createCampaignError}</p>
      {/if}
    </section>
  {:else}
    {#if showAddCharacter}
      <section class="onboarding">
        <h2 class="section-label">add character</h2>
        <div class="form-row">
          <input
            class="text-input"
            placeholder="character name"
            bind:value={newCharName}
          />
          <input
            class="text-input"
            placeholder="player name (optional)"
            bind:value={newCharPlayer}
          />
          <button
            class="btn"
            disabled={addingCharacter || !newCharName.trim()}
            onclick={addCharacter}
          >
            add
          </button>
          <button class="btn btn--ghost" onclick={() => (showAddCharacter = false)}>
            close
          </button>
        </div>
        {#if addCharacterError}
          <p class="status status--error">{addCharacterError}</p>
        {/if}
      </section>
    {/if}

    <div class="layout">
      <aside class="sidebar">
        <section class="panel">
          <h2 class="section-label">entities</h2>
          <div class="filter-row">
            <select
              class="type-select"
              value={typeFilter}
              onchange={(e) => (typeFilter = e.target.value)}
            >
              <option value="">all types</option>
              {#each ENTITY_TYPES as t}
                <option value={t}>{t}</option>
              {/each}
            </select>
          </div>
          <input
            class="text-input"
            placeholder="search by name..."
            bind:value={nameQuery}
          />

          {#if entitiesLoading}
            <p class="status">loading...</p>
          {:else if entitiesError}
            <p class="status status--error">{entitiesError}</p>
          {:else if entities.length === 0}
            <p class="status">no entities visible</p>
          {:else}
            <ul class="entity-list">
              {#each entities as entity (entity.id)}
                <li>
                  <button
                    class="entity-card"
                    class:entity-card--active={selectedEntityId === entity.id}
                    onclick={() => openEntity(entity.id)}
                  >
                    <span class="entity-name">{entity.name}</span>
                    <span class="entity-meta">
                      <span class="entity-type">{entity.entity_type}</span>
                      {#if entityBadge(entity)}
                        <span class="badge">{entityBadge(entity)}</span>
                      {/if}
                    </span>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </aside>

      <main class="main">
        <section class="panel">
          <h2 class="section-label">search</h2>
          <input
            class="text-input"
            placeholder="search entities and lore..."
            bind:value={searchQuery}
          />
          {#if searching}
            <p class="status">searching...</p>
          {:else if searchError}
            <p class="status status--error">{searchError}</p>
          {:else if searchResults.length > 0}
            <ul class="search-list">
              {#each searchResults as hit, i (hit.id ?? i)}
                <li class="search-hit">
                  {#if hit.kind === "entity"}
                    <button class="search-hit-btn" onclick={() => openEntity(hit.id)}>
                      <span class="search-hit-title">{hit.name}</span>
                      <span class="entity-type">{hit.entity_type}</span>
                      <span class="score">{hit.score.toFixed(2)}</span>
                    </button>
                  {:else}
                    <div class="search-hit-chunk">
                      <span class="search-hit-title">
                        {hit.book_id} &middot; {hit.section_path}
                      </span>
                      <span class="score">{hit.score.toFixed(2)}</span>
                      <p class="chunk-preview">{hit.preview}</p>
                    </div>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </section>

        <section class="panel">
          <h2 class="section-label">detail</h2>
          {#if detailLoading}
            <p class="status">loading...</p>
          {:else if detailError}
            <p class="status status--error">{detailError}</p>
          {:else if entityDetail}
            <div class="detail">
              <h3 class="detail-name">{entityDetail.name}</h3>
              <span class="entity-type">{entityDetail.entity_type}</span>

              {#if entityDetail.revealed_details}
                <pre class="detail-json">{JSON.stringify(
                    entityDetail.revealed_details,
                    null,
                    2,
                  )}</pre>
              {/if}

              {#each detailFields(entityDetail) as [key, value]}
                <div class="detail-row">
                  <span class="detail-key">{formatFieldName(key)}</span>
                  {#if typeof value === "object"}
                    <pre class="detail-json">{JSON.stringify(value, null, 2)}</pre>
                  {:else}
                    <span class="detail-value">{String(value)}</span>
                  {/if}
                </div>
              {/each}

              {#if isDm && entityDetail.grants}
                <div class="detail-row">
                  <span class="detail-key">grants</span>
                  {#if entityDetail.grants.length === 0}
                    <span class="detail-value">none</span>
                  {:else}
                    <ul class="grant-mini-list">
                      {#each entityDetail.grants as g}
                        <li>
                          {characterLabel(g.player_character_id)}: {g.grant_scope}
                        </li>
                      {/each}
                    </ul>
                  {/if}
                </div>
              {/if}

              <h4 class="section-label">relationships</h4>
              {#if entityRelationships.length === 0}
                <p class="status">none visible</p>
              {:else}
                <ul class="rel-list">
                  {#each entityRelationships as rel, i (i)}
                    <li
                      class="rel-row"
                      class:rel-row--dim={rel.entity.recognition_only}
                    >
                      <span class="rel-arrow">{rel.direction === "out" ? "→" : "←"}</span>
                      <span class="rel-type">{rel.rel_type}</span>
                      {#if rel.entity.recognition_only}
                        <span class="rel-name">{rel.entity.name}</span>
                        <span class="badge">name only</span>
                      {:else}
                        <button
                          class="rel-name rel-name--link"
                          onclick={() => openEntity(rel.entity.id)}
                        >
                          {rel.entity.name}
                        </button>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {:else}
            <p class="status">select an entity</p>
          {/if}
        </section>

        {#if isDm}
          <section class="panel">
            <h2 class="section-label">grant editor</h2>
            <div class="grant-form">
              <select
                class="type-select"
                bind:value={grantEntityId}
              >
                <option value="">entity...</option>
                {#each entities as entity (entity.id)}
                  <option value={entity.id}>{entity.name} ({entity.entity_type})</option>
                {/each}
              </select>
              <select
                class="type-select"
                bind:value={grantCharacterId}
              >
                <option value="">character...</option>
                {#each characters as character (character.id)}
                  <option value={character.id}>{character.character_name}</option>
                {/each}
              </select>
              <select class="type-select" bind:value={grantScope}>
                {#each GRANT_SCOPES as scope}
                  <option value={scope}>{scope}</option>
                {/each}
              </select>
              {#if grantScope === "partial"}
                <textarea
                  class="text-input json-input"
                  placeholder={`{"note": "revealed details as json"}`}
                  bind:value={grantRevealedDetails}
                ></textarea>
              {/if}
              <button
                class="btn"
                disabled={creatingGrant || !grantEntityId || !grantCharacterId}
                onclick={createGrant}
              >
                grant
              </button>
            </div>
            {#if grantError}
              <p class="status status--error">{grantError}</p>
            {/if}

            {#if grantsLoading}
              <p class="status">loading grants...</p>
            {:else if grants.length === 0}
              <p class="status">no grants yet</p>
            {:else}
              <ul class="grant-list">
                {#each grants as grant (grant.id)}
                  <li class="grant-row">
                    <span class="grant-entity">{entityLabel(grant.entity_id)}</span>
                    <span class="grant-arrow">&rarr;</span>
                    <span class="grant-character">
                      {characterLabel(grant.player_character_id)}
                    </span>
                    <select
                      class="type-select type-select--inline"
                      value={grant.grant_scope}
                      onchange={(e) => updateGrantScope(grant, e.target.value)}
                    >
                      {#each GRANT_SCOPES as scope}
                        <option value={scope}>{scope}</option>
                      {/each}
                    </select>
                  </li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}
      </main>
    </div>
  {/if}
</div>

<style>
  .root {
    height: 100vh;
    display: flex;
    flex-direction: column;
    font-family: var(--font);
    color: var(--fg);
    background: var(--bg);
  }

  .topbar {
    display: flex;
    align-items: center;
    gap: 1.25rem;
    padding: 1rem 1.5rem;
    border-bottom: var(--border-heavy);
    flex-wrap: wrap;
  }

  .title {
    font-size: 1.1rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-right: auto;
  }

  .campaign-select {
    font-family: var(--font);
    font-size: 0.85rem;
    padding: 0.35rem 0.5rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
  }

  .viewpoints {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .viewpoint-btn {
    font-family: var(--font);
    font-size: 0.75rem;
    padding: 0.35rem 0.6rem;
    background: var(--bg);
    color: var(--fg-secondary);
    border: var(--border-thin);
    cursor: pointer;
  }

  .viewpoint-btn--active {
    background: var(--fg);
    color: var(--bg);
  }

  .viewpoint-btn--add {
    color: var(--fg-tertiary);
    border-style: dashed;
  }

  .viewpoint-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .onboarding {
    padding: 1.25rem 1.5rem;
    border-bottom: var(--border-thin);
  }

  .form-row {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
  }

  .layout {
    flex: 1;
    display: flex;
    overflow: hidden;
  }

  .sidebar {
    width: 22rem;
    flex-shrink: 0;
    border-right: var(--border-thin);
    overflow-y: auto;
  }

  .main {
    flex: 1;
    overflow-y: auto;
  }

  .panel {
    padding: 1.25rem 1.5rem;
    border-bottom: var(--border-thin);
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .section-label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
  }

  .text-input,
  .type-select {
    font-family: var(--font);
    font-size: 0.85rem;
    padding: 0.4rem 0.5rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
  }

  .json-input {
    min-height: 4rem;
    resize: vertical;
  }

  .type-select--inline {
    padding: 0.15rem 0.3rem;
    font-size: 0.75rem;
  }

  .filter-row {
    display: flex;
    gap: 0.5rem;
  }

  .btn {
    font-family: var(--font);
    font-size: 0.8rem;
    padding: 0.4rem 0.75rem;
    background: var(--fg);
    color: var(--bg);
    border: var(--border-thin);
    cursor: pointer;
  }

  .btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn--ghost {
    background: var(--bg);
    color: var(--fg-secondary);
  }

  .status {
    font-size: 0.8rem;
    color: var(--fg-tertiary);
  }

  .status--error {
    color: var(--danger);
  }

  .entity-list {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .entity-card {
    width: 100%;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.5rem 0.6rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    cursor: pointer;
    font-family: var(--font);
  }

  .entity-card--active {
    background: var(--surface);
    border-color: var(--accent);
  }

  .entity-name {
    font-weight: 700;
    font-size: 0.85rem;
  }

  .entity-meta {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .entity-type {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-tertiary);
  }

  .badge {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.1rem 0.35rem;
    border: var(--border-thin);
    color: var(--fg-secondary);
  }

  .search-list,
  .grant-list,
  .grant-mini-list,
  .rel-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .search-hit-btn {
    width: 100%;
    text-align: left;
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    background: none;
    border: none;
    padding: 0.3rem 0;
    cursor: pointer;
    font-family: var(--font);
    color: var(--fg);
  }

  .search-hit-title {
    font-weight: 700;
    font-size: 0.85rem;
  }

  .score {
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
  }

  .search-hit-chunk {
    padding: 0.3rem 0;
  }

  .chunk-preview {
    font-size: 0.8rem;
    color: var(--fg-secondary);
    margin-top: 0.2rem;
  }

  .detail {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .detail-name {
    font-size: 1rem;
    font-weight: 700;
  }

  .detail-row {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    font-size: 0.8rem;
  }

  .detail-key {
    color: var(--fg-tertiary);
    text-transform: uppercase;
    font-size: 0.65rem;
    letter-spacing: 0.06em;
    min-width: 8rem;
  }

  .detail-value {
    color: var(--fg);
  }

  .detail-json {
    font-size: 0.75rem;
    background: var(--surface);
    padding: 0.5rem;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .rel-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
  }

  .rel-row--dim {
    opacity: 0.5;
  }

  .rel-name--link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-family: var(--font);
    padding: 0;
    text-decoration: underline;
  }

  .grant-form {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
  }

  .grant-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
  }

  .grant-arrow {
    color: var(--fg-tertiary);
  }
</style>
