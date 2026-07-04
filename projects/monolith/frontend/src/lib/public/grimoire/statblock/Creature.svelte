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

<article class="card-hard frame">
  <h2 class="display name">{data.name}</h2>
  {#if strap}<p class="eyebrow strap">{strap}</p>{/if}

  <hr class="rule" />

  <dl class="topline">
    {#if data.ac != null}
      <div>
        <dt class="eyebrow">Armor Class</dt>
        <dd>{data.ac}</dd>
      </div>
    {/if}
    {#if data.hp_avg != null}
      <div>
        <dt class="eyebrow">Hit Points</dt>
        <dd>{data.hp_avg}</dd>
      </div>
    {/if}
    {#if speed}
      <div>
        <dt class="eyebrow">Speed</dt>
        <dd>{speed}</dd>
      </div>
    {/if}
    {#if data.cr != null}
      <div>
        <dt class="eyebrow">Challenge</dt>
        <dd>{data.cr}</dd>
      </div>
    {/if}
  </dl>

  {#if hasScores}
    <hr class="rule" />
    <table class="abilities mono">
      <thead>
        <tr>
          {#each ABILITIES as a (a)}
            <th class="eyebrow">{a}</th>
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
    <hr class="rule" />
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
    <h3 class="eyebrow section-head">Actions</h3>
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
  /* Classic stat-block content reframed in the design system's flat-ink
     language: .card-hard supplies the border/shadow, an accent top stripe
     marks it as a stat block specifically. */
  .frame {
    padding: clamp(20px, 4vw, 32px);
    border-top: 4px solid var(--accent);
    max-width: 640px;
  }

  .name {
    font-size: clamp(28px, 5vw, 40px);
  }

  .strap {
    margin-top: 4px;
  }

  .rule {
    border: none;
    border-top: 2px solid var(--rule);
    margin: 16px 0;
  }

  .topline {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 28px;
  }

  .topline div {
    display: flex;
    gap: 8px;
    align-items: baseline;
  }

  .topline dd {
    font-weight: 700;
    font-size: 15px;
  }

  .abilities {
    width: 100%;
    border-collapse: collapse;
    text-align: center;
  }

  .abilities th {
    font-size: 12px;
    color: var(--ink-3);
    padding-bottom: 6px;
  }

  .abilities td {
    padding: 4px;
    font-size: 15px;
  }

  .ab-score {
    font-weight: 700;
  }

  .ab-mod {
    color: var(--ink-3);
    font-size: 0.85em;
  }

  .section-head {
    margin-top: 20px;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--accent);
    color: var(--ink);
  }

  .blocks {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 12px;
  }

  .block {
    line-height: 1.6;
  }

  .block-name {
    font-style: italic;
  }
</style>
