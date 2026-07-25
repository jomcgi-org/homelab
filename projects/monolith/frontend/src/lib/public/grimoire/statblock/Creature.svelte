<script>
  import {
    ABILITIES,
    abilityModifier,
    formatSpeed,
    normalizeBlocks,
  } from "$lib/public/grimoire/format.js";

  let { data } = $props();

  const strap = $derived(
    [data.size, data.creature_type].filter(Boolean).join(" "),
  );
  const speed = $derived(formatSpeed(data.speed));
  const scores = $derived(data.ability_scores ?? {});
  const hasScores = $derived(
    ABILITIES.some((a) => scores[a] != null || scores[a.toUpperCase()] != null),
  );
  const score = (a) => scores[a] ?? scores[a.toUpperCase()] ?? null;
  const traits = $derived(normalizeBlocks(data.traits));
  const actions = $derived(normalizeBlocks(data.actions));
</script>

<article class="statblock grim-paper">
  <h2 class="grim-title name">{data.name}</h2>
  {#if strap}<p class="grim-smallcaps strap">{strap}</p>{/if}

  <hr class="grim-rule" />

  <dl class="topline">
    {#if data.ac != null}
      <div>
        <dt>Armor Class</dt>
        <dd>{data.ac}</dd>
      </div>
    {/if}
    {#if data.hp_avg != null}
      <div>
        <dt>Hit Points</dt>
        <dd>{data.hp_avg}</dd>
      </div>
    {/if}
    {#if speed}
      <div>
        <dt>Speed</dt>
        <dd>{speed}</dd>
      </div>
    {/if}
    {#if data.cr != null}
      <div>
        <dt>Challenge</dt>
        <dd>{data.cr}</dd>
      </div>
    {/if}
  </dl>

  {#if hasScores}
    <hr class="grim-rule" />
    <table class="abilities">
      <thead>
        <tr>
          {#each ABILITIES as a (a)}
            <th class="grim-smallcaps">{a}</th>
          {/each}
        </tr>
      </thead>
      <tbody>
        <tr>
          {#each ABILITIES as a (a)}
            <td>
              {#if score(a) != null}
                <span class="ab-score">{score(a)}</span>
                <span class="ab-mod">({abilityModifier(score(a))})</span>
              {:else}
                <span class="ab-score">-</span>
              {/if}
            </td>
          {/each}
        </tr>
      </tbody>
    </table>
  {/if}

  {#if traits.length}
    <hr class="grim-rule" />
    <div class="blocks">
      {#each traits as t, i (i)}
        <p class="block">
          {#if t.name}<strong class="block-name">{t.name}.</strong>{/if}
          {t.text}
        </p>
      {/each}
    </div>
  {/if}

  {#if actions.length}
    <h3 class="grim-smallcaps section-head">Actions</h3>
    <div class="blocks">
      {#each actions as a, i (i)}
        <p class="block">
          {#if a.name}<strong class="block-name">{a.name}.</strong>{/if}
          {a.text}
        </p>
      {/each}
    </div>
  {/if}
</article>

<style>
  .statblock {
    padding: clamp(1rem, 3vw, 1.5rem);
    border: 1px solid var(--grim-paper-line);
    border-top: 3px solid var(--grim-accent);
    max-width: 40rem;
  }

  .name {
    font-size: clamp(1.4rem, 4vw, 1.9rem);
    color: var(--grim-accent-strong);
  }

  .strap {
    font-size: 0.85rem;
    color: var(--grim-ink-soft);
    font-style: italic;
  }

  .topline {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 1.5rem;
  }

  .topline div {
    display: flex;
    gap: 0.4rem;
    align-items: baseline;
  }

  .topline dt {
    font-family: var(--font-mono);
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-ink-soft);
  }

  .topline dd {
    font-family: var(--grim-serif);
    font-weight: 700;
  }

  .abilities {
    width: 100%;
    border-collapse: collapse;
    text-align: center;
  }

  .abilities th {
    font-size: 0.72rem;
    color: var(--grim-accent);
    padding-bottom: 0.2rem;
  }

  .abilities td {
    padding: 0.15rem 0.2rem;
    font-family: var(--grim-serif);
  }

  .ab-score {
    font-weight: 700;
  }

  .ab-mod {
    color: var(--grim-ink-soft);
    font-size: 0.8em;
  }

  .section-head {
    font-size: 0.95rem;
    color: var(--grim-accent);
    border-bottom: 1px solid var(--grim-accent);
    margin-top: 0.75rem;
    padding-bottom: 0.15rem;
  }

  .blocks {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .block {
    font-family: var(--grim-serif);
    font-size: 0.95rem;
    line-height: 1.5;
    color: var(--grim-ink);
  }

  .block-name {
    font-style: italic;
  }
</style>
