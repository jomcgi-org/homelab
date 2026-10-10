<script>
  import { onMount, untrack, tick } from "svelte";
  import { sheetRolls } from "$lib/grimoire/sheet-rolls.js";
  import { composeMessage } from "$lib/grimoire/session-compose.js";
  import RevealEditor from "$lib/grimoire/RevealEditor.svelte";
  import SessionNotesPanel from "$lib/grimoire/SessionNotesPanel.svelte";
  import JournalPanel from "$lib/grimoire/SessionJournalPanel.svelte";
  import KnowledgeDrawer from "$lib/grimoire/KnowledgeDrawer.svelte";
  import "$lib/grimoire/theme.css";

  let { data } = $props();
  let state = $state(untrack(() => data));
  let draft = $state("");
  let audience = $state("table");
  let formula = $state("d20");
  let rollLabel = $state("");
  let rollVisibility = $state("table");
  let rollMode = $state("normal");
  let rollKind = $state("checks");
  let replyTo = $state(null);
  let resolved = $state(true);
  let activeTab = $state("story");
  let selectedEntity = $state(null);
  let busy = $state(false);
  let failure = $state("");
  let pendingMessage = null;
  let connection = $state("Live");
  $effect(() => {
    state = data;
    draft = "";
    audience = "table";
    rollVisibility = data.campaign.role === "dm" ? "dm" : "table";
    replyTo = null;
  });
  let dm = $derived(state.campaign.role === "dm");
  let playing = $derived(state.session && state.session.status !== "ended");
  const endpoint = () =>
    `/grimoire/campaigns/${state.campaign.id}/session/state`;

  async function showEvent(id) {
    activeTab = "story";
    await tick();
    document.getElementById(`event-${id}`)?.scrollIntoView({ block: "center" });
  }

  async function pin(event) {
    const saved = await act({
      operation: "note",
      fromEventId: event.id,
      kind: "character",
    });
    if (saved) activeTab = "notes";
  }

  async function refresh() {
    try {
      const response = await fetch(endpoint());
      const next = await response.json();
      if (!response.ok) throw new Error(next.error);
      state = next;
      connection = "Live";
    } catch {
      connection = "Reconnecting";
    }
  }

  async function act(input) {
    if (busy) return;
    busy = true;
    failure = "";
    try {
      const response = await fetch(endpoint(), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ sessionId: state.session?.id, ...input }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (
        input.operation === "post" &&
        draft === input.text &&
        replyTo?.id === input.replyTo
      ) {
        draft = "";
        replyTo = null;
        pendingMessage = null;
      }
      await refresh();
      return result;
    } catch (error) {
      failure =
        error instanceof TypeError
          ? "Connection interrupted. Your draft is saved. Retry Send to confirm this message."
          : error.message || "Could not send. Your draft is saved; try again.";
    } finally {
      busy = false;
    }
  }

  function post(event) {
    event.preventDefault();
    const message = composeMessage({
      text: draft,
      dm,
      audience,
      replyTo,
      resolved,
    });
    const signature = JSON.stringify({
      sessionId: state.session?.id,
      ...message,
    });
    if (pendingMessage?.signature !== signature)
      pendingMessage = { signature, id: crypto.randomUUID() };
    act({ ...message, requestId: pendingMessage.id });
  }

  function actionCharacter(event) {
    const member = state.members?.find(
      (row) => row.id === event.author_member_id,
    );
    return state.characters.find(
      (row) => row.id === member?.player_character_id,
    );
  }

  function isResolved(event) {
    return state.events.some(
      (row) =>
        row.kind === "narration" &&
        !row.retracted_at &&
        row.body?.reply_to === event.id &&
        row.body?.resolved === true,
    );
  }

  function reply(event) {
    const character = actionCharacter(event);
    if (!character) return;
    replyTo = { id: event.id, name: character.character_name };
    audience = `pc:${character.id}`;
    draft = "";
    resolved = true;
    document.getElementById("message")?.focus();
  }

  function roll(event) {
    event.preventDefault();
    act({
      operation: "roll",
      formula,
      label: rollLabel,
      visibility: rollVisibility,
    });
  }

  onMount(() => {
    let stopped = false;
    let timer;
    async function poll() {
      if (!document.hidden) await refresh();
      if (!stopped) timer = setTimeout(poll, document.hidden ? 10000 : 2000);
    }
    timer = setTimeout(poll, 2000);
    const visible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  });
</script>

<svelte:head><title>{state.campaign.name} · Grimoire</title></svelte:head>

<main class="grimoire table">
  <header>
    <a href="/grimoire">← Your campaigns</a>
    <p class="eyebrow">
      {dm ? "Dungeon master" : state.characters[0]?.character_name || "Player"}
    </p>
    <h1>{state.campaign.name}</h1>
    <div class="status">
      <span>{state.session?.status || "Not started"}</span><span role="status"
        >{connection}</span
      >
    </div>
  </header>

  <nav class="table-tabs" aria-label="Session sections">
    <button
      class="secondary"
      aria-pressed={activeTab === "story"}
      onclick={() => (activeTab = "story")}>Story</button
    ><button
      class="secondary"
      aria-pressed={activeTab === "notes"}
      onclick={() => (activeTab = "notes")}>Notes</button
    >
    <button
      class="secondary"
      aria-pressed={activeTab === "journal"}
      onclick={() => (activeTab = "journal")}>Journal</button
    >
  </nav>
  <div hidden={activeTab !== "notes"}>
    <SessionNotesPanel
      endpoint={endpoint()}
      {dm}
      {showEvent}
      openKnowledge={(id) => (selectedEntity = id)}
    />
  </div>
  {#if activeTab === "journal"}<div>
      <JournalPanel
        views={state.journal}
        {showEvent}
        openKnowledge={(id) => (selectedEntity = id)}
      />
    </div>{/if}
  <div class="layout" hidden={activeTab !== "story"}>
    <section aria-label="Session feed" class="feed">
      {#if !state.events.length}
        <div class="empty">
          <h2>
            {state.session?.status === "ended"
              ? "No messages were recorded."
              : "The story begins here."}
          </h2>
          <p>
            {state.session?.status === "ended"
              ? "This session has no story entries."
              : dm
                ? state.session?.status === "active"
                  ? "Set the scene for your players."
                  : "Start the session, then set the scene for your players."
                : "Take your seat. Your DM will set the scene shortly."}
          </p>
        </div>
      {/if}
      {#each state.events as event (event.id)}
        <article
          id={`event-${event.id}`}
          class:private-event={event.audience !== "table"}
        >
          <div class="event-meta">
            <strong
              >{event.kind === "narration"
                ? "The DM"
                : event.kind === "roll"
                  ? "Dice roll"
                  : event.kind === "reveal"
                    ? "New knowledge"
                    : "Player action"}</strong
            >
            <span
              >{event.audience === "table"
                ? "Everyone"
                : event.audience === "dm"
                  ? "DM and sender"
                  : "Selected player and DM"}</span
            >
          </div>
          {#if event.kind === "roll" && !event.retracted_at}
            <div class="roll-result">
              <strong>{event.body.total}</strong><span
                >{event.body.label || "Roll"} · {event.body.formula}<small
                  >Dice: {event.body.rolls.join(", ")} · Kept: {event.body.kept.join(
                    ", ",
                  )}</small
                ></span
              >
            </div>
          {:else if event.kind === "reveal" && !event.retracted_at}
            {#each (event.body?.reveals || [event.body]).filter((item) => !item?.silent && !event.body?.retracted_entity_ids?.includes(item.entity_id)) as knowledge}
              <div class="knowledge-entry">
                {#if knowledge.retracted}<p>
                    Knowledge retracted: {knowledge.name}.
                  </p>{:else}
                  <p>
                    <strong>{knowledge.name}</strong> · {knowledge.entity_type}
                  </p>
                  {#if knowledge.grant_scope === "name_only"}
                    <p>You recognize this name.</p>
                  {:else}
                    {#each Object.entries((knowledge.projection || knowledge.entity)?.revealed_details || knowledge.projection || knowledge.entity || {}).filter(([key, value]) => !["id", "name", "entity_type", "source_type", "source_book", "site", "is_global", "created_at", "created_in_session"].includes(key) && value !== null) as [key, value]}
                      <p>
                        <strong>{key.replaceAll("_", " ")}:</strong>
                        {typeof value === "object"
                          ? JSON.stringify(value)
                          : value}
                      </p>
                    {/each}
                  {/if}
                {/if}
                {#if !knowledge.retracted && knowledge.grant_scope !== "name_only"}
                  <button
                    class="secondary"
                    onclick={() => (selectedEntity = knowledge.entity_id)}
                    >Explore {knowledge.name}</button
                  >
                  <a
                    href={`/grimoire/campaigns/${state.campaign.id}/entities/${knowledge.entity_id}`}
                    >Open knowledge page</a
                  >
                {/if}
              </div>
            {/each}
          {:else}<p>
              {event.retracted_at
                ? "This message was retracted."
                : event.body?.text || event.body?.summary || "Session update"}
            </p>
          {/if}
          {#if event.kind === "action" && event.audience === "dm" && !event.retracted_at}
            <div class="private-action">
              <small>{isResolved(event) ? "Resolved" : "Waiting for DM"}</small>
              {#if dm && actionCharacter(event) && !isResolved(event) && playing}
                <button class="secondary" onclick={() => reply(event)}
                  >Reply privately to {actionCharacter(event)
                    .character_name}</button
                >
              {/if}
            </div>
          {/if}
          {#if !event.retracted_at && !event.body?.retracted}{#if !dm}<button
                class="secondary"
                disabled={busy}
                onclick={() => pin(event)}
                aria-label={`Pin ${event.body?.name || event.body?.reveals?.map((item) => item.name).join(", ") || event.body?.text || event.kind} to notes`}
                >Pin to notes</button
              >{/if}{/if}
        </article>
      {/each}
      {#if failure}<p role="alert" class="failure">{failure}</p>{/if}
      {#if playing}
        <form onsubmit={post} class="composer">
          {#if replyTo}<div class="reply-context">
              <span>Replying privately to {replyTo.name}</span><button
                type="button"
                class="secondary"
                onclick={() => {
                  replyTo = null;
                  audience = "table";
                }}>Cancel reply</button
              >
            </div>{/if}
          <label for="message">{dm ? "Set the scene" : "What do you do?"}</label
          >
          <textarea
            id="message"
            bind:value={draft}
            placeholder={dm
              ? "The lantern flickers as someone knocks…"
              : "I approach the door…"}
            maxlength="8000"
            rows="3"
            required></textarea>
          <div class="compose-actions">
            <label
              >Send to <select
                bind:value={audience}
                aria-label="Send to"
                disabled={Boolean(replyTo)}
              >
                <option value="table">Everyone</option>
                <option value="dm">{dm ? "DM notes" : "DM privately"}</option>
                {#if dm}{#each state.characters as character}<option
                      value={`pc:${character.id}`}
                      >{character.character_name} and DM</option
                    >{/each}{/if}
              </select></label
            >
            <button disabled={busy || !draft.trim()}
              >{busy ? "Sending…" : "Send"}</button
            >
          </div>
          {#if replyTo}<label class="resolve-choice"
              ><input type="checkbox" bind:checked={resolved} />Mark action
              resolved</label
            >{/if}
        </form>
        <details class="dice-tray">
          <summary>Roll dice</summary>
          {#if !dm && state.characters[0]?.approved}
            <div class="quick-dice">
              <select bind:value={rollKind} aria-label="Sheet roll type"
                ><option value="checks">Ability checks</option><option
                  value="saves">Saving throws</option
                ></select
              >
              <select bind:value={rollMode} aria-label="Sheet roll mode"
                ><option value="normal">Normal</option><option value="adv"
                  >Advantage</option
                ><option value="dis">Disadvantage</option></select
              >
            </div>
            <div class="quick-dice" aria-label="Approved sheet rolls">
              {#each sheetRolls(state.characters[0].approved, rollKind, rollMode) as preset}
                <button
                  class="secondary"
                  disabled={busy}
                  onclick={() =>
                    act({
                      operation: "roll",
                      formula: preset.formula,
                      label: preset.label,
                      visibility: rollVisibility,
                    })}>{preset.label}</button
                >
              {/each}
            </div>
          {/if}
          <div class="quick-dice" aria-label="Quick rolls">
            {#each [4, 6, 8, 10, 12, 20, 100] as sides}<button
                class="secondary"
                disabled={busy}
                onclick={() =>
                  act({
                    operation: "roll",
                    formula: `d${sides}`,
                    visibility: rollVisibility,
                  })}>d{sides}</button
              >{/each}
          </div>
          <form onsubmit={roll}>
            <label
              >Dice formula<input
                bind:value={formula}
                maxlength="64"
                placeholder="d20, 2d6+3, 1d20adv"
                required
              /></label
            >
            <label
              >Roll label<input
                bind:value={rollLabel}
                maxlength="200"
                placeholder="Search for traps"
              /></label
            >
            <div class="compose-actions">
              <label
                >Roll visibility<select
                  bind:value={rollVisibility}
                  aria-label="Roll visibility"
                >
                  <option value="table">Everyone</option><option value="dm"
                    >{dm ? "DM only" : "DM and me"}</option
                  ><option value="self"
                    >{dm ? "DM only" : "My character and DM"}</option
                  >
                </select></label
              ><button disabled={busy}>Roll</button>
            </div>
          </form>
        </details>
      {:else if state.session}
        <p class="closed">This session has ended. Your story is saved here.</p>
      {/if}
    </section>

    <aside>
      {#if dm && playing}<RevealEditor
          endpoint={endpoint()}
          characters={state.characters}
          changed={refresh}
        />{/if}
      <h2>{dm ? "At your table" : "Your character"}</h2>
      {#each state.characters as character}<div class="character">
          <strong>{character.character_name}</strong>
          <p>
            Level {character.level || 1}
            {character.class_name || "adventurer"}
          </p>
          {#if character.approved}<p>
              HP {character.approved.max_hit_points} · AC {character.approved
                .unarmored_armor_class}
            </p>{/if}
        </div>{/each}
      <a href="/grimoire/sheets">Open character sheets</a>
      {#if dm}<a href={`/grimoire/campaigns/${state.campaign.id}/grants`}
          >Manage knowledge grants</a
        >{/if}
      {#if dm}
        <div class="controls">
          {#if !playing}<button
              disabled={busy}
              onclick={() => act({ operation: "start" })}>Start session</button
            >
          {:else}
            <button
              disabled={busy}
              onclick={() =>
                act({
                  operation: "status",
                  status:
                    state.session.status === "paused" ? "active" : "paused",
                })}
              >{state.session.status === "paused"
                ? "Resume session"
                : "Pause session"}</button
            >
            <button
              class="secondary"
              disabled={busy}
              onclick={() => {
                if (
                  confirm("End this session? The story will remain available.")
                )
                  act({ operation: "status", status: "ended" });
              }}>End session</button
            >
          {/if}
        </div>
      {/if}
    </aside>
  </div>
</main>
{#if selectedEntity}{#key selectedEntity}<KnowledgeDrawer
      endpoint={endpoint()}
      entityId={selectedEntity}
      close={() => (selectedEntity = null)}
    />{/key}{/if}

<style>
  .knowledge-entry + .knowledge-entry {
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--grim-line);
  }
  .knowledge-entry > a {
    display: inline-block;
    margin: 8px 0 8px 12px;
  }
  [hidden] {
    display: none !important;
  }
  .table-tabs {
    display: flex;
    gap: 12px;
    margin: 20px 0;
  }
  .table {
    min-height: 100vh;
    background: var(--grim-paper);
    color: var(--grim-ink);
    padding: clamp(20px, 4vw, 48px);
  }
  header,
  .layout {
    max-width: 1100px;
    margin: auto;
  }
  header {
    border-bottom: 1px solid var(--grim-line);
    padding-bottom: 24px;
    margin-bottom: 28px;
  }
  h1 {
    font-family: Georgia, serif;
    font-size: clamp(28px, 5vw, 44px);
    line-height: 1.15;
    margin: 10px 0 16px;
  }
  h2 {
    font-family: Georgia, serif;
    font-size: 23px;
  }
  .eyebrow,
  .event-meta,
  .status {
    font-size: 13px;
  }
  .eyebrow {
    margin-top: 28px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  .status,
  .event-meta,
  .compose-actions {
    display: flex;
    gap: 16px;
    justify-content: space-between;
    align-items: center;
  }
  .status {
    justify-content: flex-start;
    text-transform: capitalize;
  }
  .status span {
    border: 1px solid var(--grim-line);
    padding: 5px 10px;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 240px;
    gap: 36px;
  }
  .feed {
    min-width: 0;
  }
  article {
    border-bottom: 1px solid var(--grim-line);
    padding: 20px 0;
  }
  article p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.65;
    margin-bottom: 0;
  }
  .private-event {
    padding: 16px;
    border-left: 3px solid var(--grim-accent);
    background: var(--grim-accent-soft);
    margin: 12px 0;
  }
  .event-meta {
    flex-wrap: wrap;
    color: var(--grim-ink-soft);
  }
  aside > a {
    display: block;
    margin: 12px 0;
  }

  .empty {
    padding: 32px 0 50px;
  }
  .empty p,
  .character p {
    color: var(--grim-ink-soft);
    line-height: 1.6;
  }
  .composer {
    margin-top: 28px;
  }
  .dice-tray {
    margin-top: 24px;
    border-top: 1px solid var(--grim-line);
    padding-top: 16px;
  }
  .dice-tray summary {
    cursor: pointer;
    min-height: 36px;
  }
  .dice-tray form {
    display: grid;
    gap: 14px;
  }
  .dice-tray form > label {
    display: grid;
    gap: 6px;
  }
  .quick-dice {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 4px 0 16px;
  }
  input {
    font: inherit;
    color: inherit;
    padding: 12px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    width: 100%;
    box-sizing: border-box;
  }
  .roll-result {
    display: flex;
    gap: 16px;
    align-items: center;
    margin-top: 12px;
  }
  .roll-result > strong {
    font-family: Georgia, serif;
    font-size: 36px;
  }
  .roll-result small {
    display: block;
    margin-top: 6px;
    color: var(--grim-ink-soft);
  }
  .private-action,
  .reply-context {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    margin-top: 14px;
  }
  .resolve-choice {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 12px;
  }
  .resolve-choice input {
    width: auto;
  }
  textarea {
    display: block;
    width: 100%;
    box-sizing: border-box;
    margin: 10px 0 12px;
    border: 1px solid var(--grim-line);
    padding: 14px;
    font: inherit;
    color: inherit;
    background: var(--grim-surface);
    resize: vertical;
  }
  select {
    max-width: 100%;
    font: inherit;
    padding: 10px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  button {
    min-height: 44px;
    padding: 10px 20px;
    font: inherit;
    border: 1px solid var(--grim-accent);
    background: var(--grim-accent);
    color: white;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .secondary {
    background: transparent;
    color: var(--grim-accent);
  }
  .character {
    padding: 12px 0;
    border-bottom: 1px solid var(--grim-line);
    margin-bottom: 16px;
  }
  .character p {
    margin: 4px 0;
  }
  .controls {
    display: grid;
    gap: 10px;
    margin-top: 28px;
  }
  .failure {
    padding: 14px;
    border: 1px solid #9c3028;
    color: #9c3028;
  }
  a {
    color: var(--grim-accent);
  }
  @media (max-width: 700px) {
    .layout {
      display: flex;
      flex-direction: column-reverse;
      gap: 22px;
    }
    aside {
      border-bottom: 1px solid var(--grim-line);
      padding-bottom: 20px;
    }
    aside h2 {
      margin-top: 0;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      margin-top: 16px;
    }
    aside h2 {
      font-size: 18px;
      margin-bottom: 10px;
    }
    .character {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 12px;
      align-items: baseline;
      padding: 0;
      border: 0;
      margin-bottom: 10px;
    }
    .character p {
      margin: 0;
      font-size: 14px;
    }
    .compose-actions {
      align-items: flex-end;
    }
    .compose-actions label {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
  }
</style>
