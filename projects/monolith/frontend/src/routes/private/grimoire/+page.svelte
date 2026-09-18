<script>
  import "$lib/grimoire/theme.css";

  let { data, form } = $props();

  const abilities = [
    ["strength", "STR"],
    ["dexterity", "DEX"],
    ["constitution", "CON"],
    ["intelligence", "INT"],
    ["wisdom", "WIS"],
    ["charisma", "CHA"],
  ];

  function signed(value) {
    return value >= 0 ? `+${value}` : String(value);
  }

  function date(value) {
    return value ? new Date(value).toLocaleString() : "";
  }
</script>

<svelte:head>
  <title>Character sheets · Grimoire</title>
</svelte:head>

<main class="grimoire sheets-page">
  <header class="page-head">
    <div>
      <p class="eyebrow">Grimoire · private table</p>
      <h1>Character sheets</h1>
      <p class="lede">
        Players author base facts. Grimoire calculates the mechanics and keeps every
        submitted decision as history.
      </p>
    </div>
    <span class="contract">contract v1</span>
  </header>

  {#if form?.error}
    <p class="notice notice--error" role="alert">{form.error}</p>
  {:else if form?.ok}
    <p class="notice" role="status">Sheet updated.</p>
  {/if}

  {#if data.unavailable}
    <section class="empty">
      <h2>Sheets are unavailable</h2>
      <p>{data.message}</p>
    </section>
  {:else if data.groups.length === 0}
    <section class="empty">
      <h2>No campaigns yet</h2>
      <p>A campaign DM must add your verified account before sheets appear here.</p>
    </section>
  {:else}
    {#each data.groups as group}
      <section class="campaign">
        <div class="campaign-head">
          <p class="eyebrow">Campaign</p>
          <h2>{group.campaign.name}</h2>
        </div>

        {#if group.workspaces.length === 0}
          <p class="empty-inline">No assigned characters.</p>
        {/if}

        <div class="character-grid">
          {#each group.workspaces as workspace}
            {@const latest = workspace.versions[0] ?? null}
            {@const draft = latest?.status === "draft" ? latest : null}
            {@const base = draft?.sheet ?? latest?.sheet ?? null}
            <article class="sheet-card">
              <header class="character-head">
                <div>
                  <p class="eyebrow">{workspace.viewer_role}</p>
                  <h3>{workspace.character.character_name}</h3>
                  <p class="summary">
                    {workspace.character.class_name ?? "Unapproved class"}
                    {workspace.character.level ? ` · level ${workspace.character.level}` : ""}
                  </p>
                </div>
                <span class:approved={latest?.status === "approved"} class="status">
                  {latest?.status ?? "no sheet"}
                </span>
              </header>

              {#if workspace.viewer_role === "player" && latest?.status !== "submitted"}
                <form method="POST" action="?/save" class="builder">
                  <input type="hidden" name="campaign_id" value={group.campaign.id} />
                  <input type="hidden" name="character_id" value={workspace.character.id} />
                  {#if draft}
                    <input type="hidden" name="version_id" value={draft.id} />
                  {/if}
                  <div class="identity-fields">
                    <label>
                      <span>Ancestry</span>
                      <input
                        name="ancestry"
                        value={base?.ancestry ?? ""}
                        required
                        maxlength="120"
                      />
                    </label>
                    <label>
                      <span>Class</span>
                      <input
                        name="class_name"
                        value={base?.class_name ?? ""}
                        required
                        maxlength="120"
                      />
                    </label>
                    <label class="level-field">
                      <span>Level</span>
                      <input
                        name="level"
                        type="number"
                        min="1"
                        max="20"
                        value={base?.level ?? 1}
                        required
                      />
                    </label>
                  </div>
                  <fieldset>
                    <legend>Base ability scores</legend>
                    <div class="abilities">
                      {#each abilities as [name, label]}
                        <label>
                          <span>{label}</span>
                          <input
                            name={name}
                            type="number"
                            min="3"
                            max="20"
                            value={base?.ability_scores?.[name] ?? 10}
                            required
                          />
                        </label>
                      {/each}
                    </div>
                  </fieldset>
                  <button type="submit">{draft ? "Save draft" : "Start new draft"}</button>
                </form>

                {#if draft}
                  <form method="POST" action="?/submit" class="submit-row">
                    <input type="hidden" name="campaign_id" value={group.campaign.id} />
                    <input type="hidden" name="character_id" value={workspace.character.id} />
                    <input type="hidden" name="version_id" value={draft.id} />
                    <p>Submitting locks version {draft.version} for DM review.</p>
                    <button type="submit" class="primary">Submit to DM</button>
                  </form>
                {/if}
              {:else if latest?.status === "submitted"}
                <p class="notice">Version {latest.version} is waiting for the DM.</p>
              {/if}

              {#if latest?.derived}
                <section class="derived" aria-label="Calculated mechanics">
                  <div>
                    <span>Proficiency</span>
                    <strong>{signed(latest.derived.proficiency_bonus)}</strong>
                  </div>
                  <div>
                    <span>Unarmored AC</span>
                    <strong>{latest.derived.unarmored_armor_class}</strong>
                  </div>
                  <div><span>Max HP</span><strong>{latest.derived.max_hit_points}</strong></div>
                  <div><span>Hit die</span><strong>{latest.derived.hit_die}</strong></div>
                  {#each abilities as [name, label]}
                    <div>
                      <span>{label} mod / save</span>
                      <strong>
                        {signed(latest.derived.ability_modifiers[name])} /
                        {signed(latest.derived.saving_throw_bonuses[name])}
                      </strong>
                    </div>
                  {/each}
                </section>
              {/if}

              {#if workspace.viewer_role === "dm" && latest?.status === "submitted"}
                <form method="POST" action="?/decide" class="decision">
                  <input type="hidden" name="campaign_id" value={group.campaign.id} />
                  <input type="hidden" name="character_id" value={workspace.character.id} />
                  <input type="hidden" name="version_id" value={latest.id} />
                  <label>
                    <span>Decision comment</span>
                    <textarea
                      name="comment"
                      maxlength="1000"
                      rows="3"
                      placeholder="Required when returning"
                    ></textarea>
                  </label>
                  <div class="decision-actions">
                    <button type="submit" name="decision" value="return">Return to player</button>
                    <button
                      type="submit"
                      name="decision"
                      value="approve"
                      class="primary">Approve version</button
                    >
                  </div>
                </form>
              {/if}

              {#if latest?.decision_comment}
                <blockquote>
                  <p>{latest.decision_comment}</p>
                  <footer>{latest.decided_by_email} · {date(latest.decided_at)}</footer>
                </blockquote>
              {/if}

              <details class="history">
                <summary>Version history ({workspace.versions.length})</summary>
                {#if workspace.versions.length === 0}
                  <p>No versions submitted.</p>
                {:else}
                  <ol>
                    {#each workspace.versions as version}
                      <li>
                        <span>v{version.version} · {version.status}</span>
                        <time datetime={version.created_at}>{date(version.created_at)}</time>
                      </li>
                    {/each}
                  </ol>
                {/if}
              </details>
            </article>
          {/each}
        </div>
      </section>
    {/each}
  {/if}
</main>

<style>
  .sheets-page {
    min-height: 100vh;
    padding: 5rem clamp(1rem, 4vw, 4rem);
    background: var(--grim-bg, #f7f3e8);
    color: var(--grim-ink, #222018);
  }
  .page-head,
  .campaign-head,
  .character-head,
  .submit-row,
  .decision-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .page-head {
    max-width: 78rem;
    margin: 0 auto 3rem;
    align-items: flex-start;
  }
  h1,
  h2,
  h3,
  p {
    margin-top: 0;
  }
  h1 {
    margin-bottom: 0.5rem;
    font-size: clamp(2.5rem, 7vw, 5rem);
    line-height: 0.95;
  }
  h2 {
    font-size: 1.8rem;
  }
  h3 {
    margin-bottom: 0.25rem;
    font-size: 1.5rem;
  }
  .eyebrow {
    margin-bottom: 0.35rem;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 0.72rem;
    font-weight: 700;
  }
  .lede,
  .summary,
  .empty-inline {
    color: var(--grim-muted, #6b6659);
  }
  .contract,
  .status {
    border: 1px solid currentColor;
    border-radius: 999px;
    padding: 0.35rem 0.65rem;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    white-space: nowrap;
  }
  .status.approved {
    color: #26633a;
  }
  .campaign {
    max-width: 78rem;
    margin: 0 auto 3rem;
  }
  .campaign-head {
    justify-content: flex-start;
    align-items: baseline;
    border-bottom: 1px solid #bdb6a5;
    margin-bottom: 1rem;
  }
  .character-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 32rem), 1fr));
    gap: 1.25rem;
  }
  .sheet-card,
  .empty {
    border: 1px solid #bdb6a5;
    background: color-mix(
      in srgb,
      var(--grim-bg, #f7f3e8) 88%,
      white
    );
    padding: clamp(1rem, 3vw, 1.75rem);
    box-shadow: 4px 4px 0 #d7cfbd;
  }
  .builder {
    margin-top: 1.5rem;
  }
  .identity-fields {
    display: grid;
    grid-template-columns: 1fr 1fr 5rem;
    gap: 0.75rem;
  }
  label span,
  legend,
  .derived span {
    display: block;
    margin-bottom: 0.3rem;
    color: var(--grim-muted, #6b6659);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  input,
  textarea,
  button {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid #8e8778;
    border-radius: 0;
    background: #fffdf7;
    color: inherit;
    font: inherit;
    padding: 0.65rem 0.7rem;
  }
  button {
    width: auto;
    cursor: pointer;
    background: transparent;
    font-weight: 700;
  }
  button:hover,
  button:focus-visible,
  button.primary {
    background: var(--grim-ink, #222018);
    color: var(--grim-bg, #f7f3e8);
  }
  fieldset {
    margin: 1rem 0;
    border: 0;
    padding: 0;
  }
  .abilities {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 0.5rem;
  }
  .abilities input {
    text-align: center;
  }
  .submit-row,
  .decision {
    margin-top: 1rem;
    border-top: 1px solid #d7cfbd;
    padding-top: 1rem;
  }
  .submit-row p {
    margin: 0;
    font-size: 0.85rem;
  }
  .derived {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.7rem;
    margin-top: 1.5rem;
  }
  .derived div {
    border-left: 2px solid #b58a3a;
    padding-left: 0.65rem;
  }
  .derived strong {
    font-variant-numeric: tabular-nums;
  }
  .decision-actions {
    margin-top: 0.75rem;
    justify-content: flex-end;
  }
  .notice {
    max-width: 78rem;
    margin: 0 auto 1rem;
    border: 1px solid #9b8b59;
    padding: 0.75rem 1rem;
    background: #fff7d8;
  }
  .notice--error {
    border-color: #a3483e;
    background: #fde9e4;
  }
  blockquote {
    margin: 1rem 0 0;
    border-left: 3px solid #a3483e;
    padding-left: 1rem;
  }
  blockquote footer,
  .history time {
    color: var(--grim-muted, #6b6659);
    font-size: 0.75rem;
  }
  .history {
    margin-top: 1.25rem;
  }
  .history summary {
    cursor: pointer;
    font-weight: 700;
  }
  .history ol {
    margin-bottom: 0;
    padding-left: 1.4rem;
  }
  .history li {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.35rem 0;
  }
  .empty {
    max-width: 48rem;
    margin: 3rem auto;
  }
  @media (max-width: 640px) {
    .page-head, .character-head, .submit-row { align-items: flex-start; flex-direction: column; }
    .identity-fields { grid-template-columns: 1fr 1fr; }
    .level-field { grid-column: span 2; }
    .abilities { grid-template-columns: repeat(3, 1fr); }
    .derived { grid-template-columns: 1fr; }
  }
</style>
