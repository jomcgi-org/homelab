<script>
  import { onMount, untrack } from "svelte";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { joinMeta } from "./run-format.js";
  import { parsePatchHunks, walkthroughView } from "./walkthrough.js";

  let {
    sessionId = null,
    turnSeq = null,
    model = "",
    fixture = null,
  } = $props();

  let payload = $state(untrack(() => fixture?.payload ?? null));
  let loading = $state(false);
  let failed = $state(false);
  let patches = $state({});

  const walk = $derived(
    payload ? walkthroughView(payload, { sessionId, turnSeq }) : null,
  );
  const accounted = $derived(
    walk?.points.filter((item) => item.kind === "authored") ?? [],
  );
  const unexplained = $derived(
    walk?.points.filter((item) => item.kind === "unexplained") ?? [],
  );

  function attributionLine(item) {
    return joinMeta(
      P.labels.walkAccountLabel,
      item.attribution?.turn != null
        ? `${P.labels.turn} ${item.attribution.turn}`
        : "",
      item.attribution?.attempt != null
        ? `${P.labels.attempt} ${item.attribution.attempt}`
        : "",
      model,
    );
  }

  async function load() {
    if (payload || loading || fixture) return;
    loading = true;
    failed = false;
    try {
      const response = await fetch(
        `/api/swarm/walkthrough/${encodeURIComponent(sessionId)}/${encodeURIComponent(turnSeq)}`,
      );
      if (!response.ok) throw new Error("walkthrough unavailable");
      payload = await response.json();
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
  }

  async function loadPatch(item) {
    const path = item?.path;
    if (!path || patches[path]) return;
    if (fixture?.patches?.[path] !== undefined) {
      patches[path] = { hunks: parsePatchHunks(fixture.patches[path]) };
      return;
    }
    if (!item.patchUrl) return;
    patches[path] = { loading: true };
    try {
      const response = await fetch(item.patchUrl);
      if (!response.ok) throw new Error("patch unavailable");
      const body = await response.json();
      patches[path] = { hunks: parsePatchHunks(body.patch) };
    } catch {
      patches[path] = { failed: true };
    }
  }

  // The full-page view is open by definition. Keep the one-shot onMount load
  // because a declaratively visible panel has no disclosure toggle to trigger.
  onMount(load);

  $effect(() => {
    if (!walk) return;
    for (const item of walk.points) loadPatch(item);
  });

  function retry() {
    payload = null;
    load();
  }
</script>

{#snippet diff(item)}
  {#if item.patchUrl || fixture?.patches?.[item.path] !== undefined}
    <div class="hunks">
      {#if patches[item.path]?.loading}
        <div class="walk-fact pad">{P.labels.walkDiffLoading}</div>
      {:else if patches[item.path]?.failed}
        <div class="walk-fact pad">{P.labels.walkDiffUnavailable}</div>
      {:else if patches[item.path]?.hunks}
        {#each patches[item.path].hunks as hunk, hunkIndex (hunkIndex)}
          <div class="hunk">
            <div class="hunk-inner">
              {#if hunk.header}
                <div class="hunk-head">{hunk.header}</div>
              {/if}
              {#each hunk.lines as line, lineIndex (lineIndex)}
                <span class={`dl ${line.kind}`}
                  ><span class="g">{line.gutter}</span>{line.text}</span
                >
              {/each}
            </div>
          </div>
        {/each}
      {/if}
    </div>
  {/if}
{/snippet}

{#snippet fileHeading(item)}
  <header class="file-heading">
    <h4>{item.path}</h4>
    <span class="file-stat">
      <span class="plus">+{item.additions}</span>
      <span class="minus">&minus;{item.deletions}</span>
    </span>
  </header>
{/snippet}

<section class="walk-turn" aria-labelledby={`walk-turn-${turnSeq}`}>
  <h3 id={`walk-turn-${turnSeq}`}>
    {P.labels.turn}
    {turnSeq}
  </h3>

  {#if loading}
    <div class="walk-fact">{P.labels.walkLoading}</div>
  {:else if failed}
    <div class="walk-fact">
      {P.labels.walkUnavailable}
      <button class="walk-retry" type="button" onclick={retry}
        >{P.labels.walkRetry}</button
      >
    </div>
  {:else if walk}
    {#if walk.summary?.status === "available"}
      <p class="summary-sentence">
        {walk.summary.files}
        {walk.summary.files === 1
          ? P.labels.walkFileChanged
          : P.labels.walkFilesChanged}
        {P.labels.walkSummaryWith}
        {walk.summary.insertions}
        {walk.summary.insertions === 1
          ? P.labels.walkInsertion
          : P.labels.walkInsertions}
        {P.labels.walkSummaryAnd}
        {walk.summary.deletions}
        {walk.summary.deletions === 1
          ? P.labels.walkDeletion
          : P.labels.walkDeletions}{P.punct.semicolon}
        {P.labels.walkAgentAccountedSentence}
        {walk.summary.accounted}
        {walk.summary.accounted === 1
          ? P.labels.walkFileWord
          : P.labels.walkFilesWord}{P.punct.comma}
        {P.labels.walkSummaryLeaving}
        {walk.summary.unexplained}
        {walk.summary.unexplained === 1
          ? P.labels.walkFileWord
          : P.labels.walkFilesWord}
        {P.labels.walkSummaryUnexplainedEnd}{P.punct.period}
      </p>
    {:else if walk.summary?.status === "diff_unavailable"}
      <p class="summary-sentence">{P.labels.walkSummaryDiffUnavailable}</p>
    {/if}

    {#if walk.message}<div class="walk-fact">{walk.message}</div>{/if}
    {#each walk.truncations as reason (reason)}
      <div class="walk-fact">{reason}</div>
    {/each}

    {#if walk.hasTestimony}
      <div class="provenance">
        <b>{P.labels.walkProvenanceLead}</b>
        <span>{P.labels.walkNarrativeProvenanceBody}</span>
      </div>
    {/if}

    <div class="files">
      {#each accounted as item, index (`accounted:${item.path}:${index}`)}
        <article class="file-change">
          {@render fileHeading(item)}
          {#if item.why}
            <div class="account">
              <div class="account-label">{attributionLine(item)}</div>
              <p>{item.why}</p>
            </div>
          {:else}
            <div class="no-account">{P.labels.walkNoAccount}</div>
          {/if}
          {#each item.deviations as deviation (deviation)}
            <div class="deviation">
              <span>{P.labels.deviationWord}</span>
              {deviation}
            </div>
          {/each}
          {@render diff(item)}
        </article>
      {/each}

      {#if walk.mechanical.length > 0}
        <section class="supporting" aria-label={P.labels.walkMechanicalHeading}>
          <h4>{P.labels.walkMechanicalHeading}</h4>
          {#each walk.mechanical as mech, index (index)}
            <p>
              {P.labels.walkMechanicalStep}{P.punct.colon}
              {mech.count}
              {mech.count === 1
                ? P.labels.walkFileWord
                : P.labels.walkFilesWord}
              {#if mech.generator}<code>{mech.generator}</code>{/if}
            </p>
          {/each}
        </section>
      {/if}

      {#if unexplained.length > 0}
        <section
          class="unexplained-files"
          aria-labelledby={`walk-unexplained-${turnSeq}`}
        >
          <h4 id={`walk-unexplained-${turnSeq}`}>
            {P.labels.walkUnexplainedFilesHeading}
          </h4>
          <p class="unexplained-intro">
            {P.labels.walkUnexplainedBody}
          </p>
          {#each unexplained as item, index (`unexplained:${item.path}:${index}`)}
            <article class="file-change unexplained-change">
              {@render fileHeading(item)}
              {@render diff(item)}
            </article>
          {/each}
        </section>
      {/if}

      {#if walk.contradictions.length > 0}
        <section
          class="contradictions"
          aria-labelledby={`walk-contradictions-${turnSeq}`}
        >
          <h4 id={`walk-contradictions-${turnSeq}`}>
            {P.labels.walkContradictedFilesHeading}
          </h4>
          <p>{P.labels.walkContradictedBody}</p>
          {#each walk.contradictions as item, index (`contradiction:${item.path}:${index}`)}
            <article class="claim">
              <code>{item.path}</code>
              {#if item.why}<p>{item.why}</p>{/if}
            </article>
          {/each}
        </section>
      {/if}

      {#if walk.rung === 4 && walk.touched.length > 0}
        <section class="supporting" aria-label={P.labels.walkTouchedLabel}>
          <h4>{P.labels.walkTouchedLabel}</h4>
          <ul>
            {#each walk.touched as path (path)}<li>
                <code>{path}</code>
              </li>{/each}
          </ul>
        </section>
      {/if}
    </div>
  {/if}
</section>

<style>
  .walk-turn {
    min-width: 0;
    padding: 24px 0 36px;
    border-top: 4px solid var(--line);
  }
  .walk-turn:first-child {
    border-top: 0;
  }
  .walk-turn > h3 {
    margin: 0 0 8px;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }
  .summary-sentence {
    max-width: 760px;
    margin: 0;
    color: var(--text);
    font-size: var(--size-title);
    font-weight: 600;
    line-height: 1.35;
    text-wrap: pretty;
  }
  .walk-fact {
    margin-top: 8px;
    color: var(--muted);
    font-size: var(--size-detail);
  }
  .walk-fact.pad {
    padding: 12px 16px;
  }
  .walk-retry {
    margin-left: 8px;
    padding: 0 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
  }
  .provenance {
    display: flex;
    align-items: baseline;
    gap: 8px;
    max-width: 760px;
    margin-top: 16px;
    padding: 8px 12px;
    background: var(--attn-soft);
    color: var(--text);
    font-size: var(--size-meta);
  }
  .provenance b {
    color: var(--attn-text);
  }
  .files {
    min-width: 0;
    margin-top: 24px;
  }
  .file-change {
    min-width: 0;
    padding: 20px 0 28px;
    border-top: 1px solid var(--line);
  }
  .file-heading {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .file-heading h4 {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    font: 600 var(--size-body-mono) var(--font-mono);
  }
  .file-stat {
    display: flex;
    flex: none;
    gap: 8px;
    margin-left: auto;
    font: var(--size-body-mono) var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .plus {
    color: var(--diff-add-mark);
  }
  .minus {
    color: var(--diff-del-mark);
  }
  .account {
    max-width: 760px;
    margin: 16px 0;
    padding-left: 12px;
    border-left: 2px solid var(--attn);
  }
  .account-label {
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }
  .account p {
    margin: 6px 0 0;
    color: var(--text);
    font-size: var(--size-detail);
    line-height: 1.6;
    text-wrap: pretty;
  }
  .no-account,
  .deviation {
    max-width: 760px;
    margin: 12px 0;
    color: var(--text-soft);
    font-size: var(--size-detail);
  }
  .deviation span {
    margin-right: 8px;
    color: var(--attn-text);
    font: var(--size-meta) var(--font-mono);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .hunks {
    max-width: 100%;
    margin-top: 16px;
    overflow-x: auto;
    background: var(--code-bg);
  }
  .hunk + .hunk {
    border-top: 1px solid var(--line);
  }
  .hunk-inner {
    width: max-content;
    min-width: 100%;
  }
  .hunk-head {
    position: sticky;
    left: 0;
    padding: 4px 16px;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
  }
  .dl {
    display: block;
    padding: 1px 16px 1px 30px;
    color: var(--text-soft);
    font: var(--size-body-mono) var(--font-mono);
    line-height: 1.6;
    text-indent: -14px;
    white-space: pre;
  }
  .dl.add {
    background: var(--diff-add-bg);
    color: var(--text);
  }
  .dl.del {
    background: var(--diff-del-bg);
    color: var(--text);
  }
  .g {
    display: inline-block;
    width: 14px;
    color: var(--muted);
  }
  .dl.add .g {
    color: var(--diff-add-mark);
  }
  .dl.del .g {
    color: var(--diff-del-mark);
  }
  .supporting,
  .unexplained-files,
  .contradictions {
    margin-top: 24px;
    padding-top: 20px;
    border-top: 4px solid var(--line);
  }
  .supporting > h4,
  .unexplained-files > h4,
  .contradictions > h4 {
    margin: 0;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }
  .supporting p,
  .unexplained-intro,
  .contradictions > p {
    max-width: 760px;
    margin: 8px 0 0;
    color: var(--text-soft);
    font-size: var(--size-detail);
  }
  .supporting code {
    display: block;
    margin-top: 4px;
    overflow-wrap: anywhere;
  }
  .supporting ul {
    margin: 8px 0 0;
    padding-left: 20px;
  }
  .unexplained-files > h4,
  .contradictions > h4 {
    color: var(--err);
  }
  .unexplained-change {
    margin-top: 16px;
    padding-left: 12px;
    border-left: 4px solid var(--err-line);
  }
  .claim {
    max-width: 760px;
    margin-top: 12px;
    padding: 10px 12px;
    background: var(--err-bg);
  }
  .claim code {
    overflow-wrap: anywhere;
    color: var(--err);
  }
  .claim p {
    margin: 6px 0 0;
    color: var(--text);
    font-size: var(--size-detail);
  }
  @media (max-width: 760px) {
    .walk-turn {
      padding-top: 20px;
    }
    .summary-sentence {
      font-size: var(--size-body);
    }
    .provenance {
      display: grid;
      gap: 2px;
    }
  }
</style>
