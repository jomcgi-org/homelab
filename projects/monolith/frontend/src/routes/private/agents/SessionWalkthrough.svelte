<script>
  // Session-tier walkthrough (ADR 056): what did this turn change, and why.
  // The diff is fact, the agent's points are testimony, and the two are
  // juxtaposed, never merged. Collapsed by default; the compare is fetched
  // once on first open (the console polls, so nothing here polls), and
  // per-step patches are fetched lazily and only for authored steps.
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { joinMeta } from "./run-format.js";
  import { composeWalkthrough, parsePatch } from "./walkthrough.js";

  let {
    sessionId = null,
    turnSeq = null,
    model = "",
    // Fixture preview (?fixture=walk-*): compare/rationale/patches supplied
    // inline, so the preview never fetches.
    fixture = null,
  } = $props();

  let compare = $state(fixture?.compare ?? null);
  let rationale = $state(fixture?.rationale ?? null);
  let loading = $state(false);
  let failed = $state(false);
  let focus = $state(0);
  let diffOpen = $state({});
  let patches = $state({});

  const walk = $derived(
    compare ? composeWalkthrough(compare, rationale) : null,
  );
  const total = $derived(walk?.steps.length ?? 0);
  const attribution = $derived(
    joinMeta(`${P.labels.turn} ${turnSeq ?? ""}`.trim(), model),
  );

  async function load() {
    if (compare || loading || fixture) return;
    loading = true;
    failed = false;
    try {
      const response = await fetch(
        `/api/swarm/compare/${encodeURIComponent(sessionId)}/${encodeURIComponent(turnSeq)}`,
      );
      if (!response.ok) throw new Error("compare unavailable");
      compare = await response.json();
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
  }

  function onToggle(event) {
    if (event.currentTarget.open) load();
  }

  function focusStep(index) {
    if (total === 0) return;
    focus = Math.max(0, Math.min(index, total - 1));
  }

  // Left/right arrows walk the steps, the OpenChamber guided-tour motion.
  // Up/down stay untouched so the transcript keeps scrolling normally.
  function onKeydown(event) {
    if (total < 2) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      focusStep(focus - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      focusStep(focus + 1);
    }
  }

  async function toggleDiff(step) {
    const path = step.path;
    diffOpen[path] = !diffOpen[path];
    if (!diffOpen[path] || patches[path]) return;
    if (fixture?.patches?.[path] !== undefined) {
      patches[path] = { lines: parsePatch(fixture.patches[path]) };
      return;
    }
    if (!step.patchUrl) {
      patches[path] = { failed: true };
      return;
    }
    patches[path] = { loading: true };
    try {
      const response = await fetch(step.patchUrl);
      if (!response.ok) throw new Error("patch unavailable");
      const body = await response.json();
      patches[path] = { lines: parsePatch(body.patch) };
    } catch {
      patches[path] = { failed: true };
    }
  }

  function retry() {
    compare = null;
    load();
  }
</script>

<details class="walk" open={Boolean(fixture)} ontoggle={onToggle}>
  <summary class="walk-summary mono">
    <span>{P.labels.walkSummary}</span>
    {#if walk && walk.rung <= 2}
      <span class="walk-stats">
        {walk.stats.totalFiles}
        {walk.stats.totalFiles === 1
          ? P.labels.walkFileWord
          : P.labels.walkFilesWord}
        <span class="add">+{walk.stats.additions}</span>
        <span class="del">&minus;{walk.stats.deletions}</span>
      </span>
    {/if}
  </summary>

  {#if loading}
    <div class="walk-fact">{P.labels.walkLoading}</div>
  {:else if failed}
    <div class="walk-fact">
      {P.labels.walkUnavailable}
      <button class="walk-retry mono" type="button" onclick={retry}
        >{P.labels.walkRetry}</button
      >
    </div>
  {:else if walk}
    {#if walk.ephemeral}
      <div class="walk-fact">{P.labels.walkEphemeralNote}</div>
    {/if}
    {#each walk.truncation as reason}
      <div class="walk-fact">
        <span class="walk-flag">{P.labels.walkTruncatedWord}</span>
        {reason}
      </div>
    {/each}

    {#if walk.rung <= 2}
      {#if !walk.trailer}
        <div class="walk-fact">{P.labels.walkNoTrailerNote}</div>
      {/if}
      {#if total > 0}
        <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
        <div
          class="walk-tour"
          role="group"
          aria-label={P.labels.walkSummary}
          onkeydown={onKeydown}
        >
          {#if total > 1}
            <div class="walk-nav">
              <span class="walk-progress"
                >{P.labels.walkStepWord} {focus + 1} {P.labels.of} {total}</span
              >
              <button
                class="walk-nav-btn"
                type="button"
                aria-label={P.labels.walkPrevStep}
                disabled={focus === 0}
                onclick={() => focusStep(focus - 1)}>&larr;</button
              >
              <button
                class="walk-nav-btn"
                type="button"
                aria-label={P.labels.walkNextStep}
                disabled={focus === total - 1}
                onclick={() => focusStep(focus + 1)}>&rarr;</button
              >
              <span class="walk-phone-note">{P.labels.walkDiffsDesktop}</span>
            </div>
          {/if}
          <ol class="walk-steps">
            {#each walk.steps as step, index (step.path)}
              <li class="walk-step" class:focused={index === focus}>
                <button
                  class="walk-step-head"
                  type="button"
                  aria-expanded={index === focus}
                  onclick={() => focusStep(index)}
                >
                  <span class="walk-idx">{index + 1}</span>
                  <span class="walk-path">{step.path}</span>
                  {#if step.unexplained}
                    <span class="walk-flag" title={P.labels.walkUnexplainedNote}
                      >{P.labels.walkUnexplained}</span
                    >
                  {/if}
                  <span class="walk-file-stats">
                    <span class="add">+{step.additions}</span>
                    <span class="del">&minus;{step.deletions}</span>
                  </span>
                </button>
                {#if index === focus}
                  <div class="walk-step-body">
                    {#if step.why}
                      <div class="testimony" data-register="testimony">
                        <div class="testimony-attribution">{attribution}</div>
                        <div class="testimony-line">{step.why}</div>
                      </div>
                    {:else if step.unexplained}
                      <div class="walk-fact">
                        {P.labels.walkUnexplainedNote}
                      </div>
                    {/if}
                    {#if step.patchUrl || fixture?.patches?.[step.path] !== undefined}
                      <button
                        class="walk-diff-btn"
                        type="button"
                        onclick={() => toggleDiff(step)}
                        >{diffOpen[step.path]
                          ? P.labels.walkHideDiff
                          : P.labels.walkShowDiff}</button
                      >
                      {#if diffOpen[step.path]}
                        {#if patches[step.path]?.loading}
                          <div class="walk-fact">
                            {P.labels.walkDiffLoading}
                          </div>
                        {:else if patches[step.path]?.failed}
                          <div class="walk-fact">
                            {P.labels.walkDiffUnavailable}
                          </div>
                        {:else if patches[step.path]?.lines}
                          <!-- A div of block spans, not a pre: template
                               whitespace inside a pre would render as blank
                               lines between every patch line. -->
                          <div class="walk-patch">
                            {#each patches[step.path].lines as line, lineIndex (lineIndex)}
                              <span class={`pl pl-${line.kind}`}
                                >{line.text}</span
                              >
                            {/each}
                          </div>
                        {/if}
                      {/if}
                    {/if}
                  </div>
                {/if}
              </li>
            {/each}
          </ol>
        </div>
      {/if}
      {#if walk.mechanical}
        <details class="walk-mech">
          <summary class="walk-mech-summary"
            >{P.labels.walkMechanicalStep}{P.punct.colon}
            {walk.mechanical.count}
            {walk.mechanical.count === 1
              ? P.labels.walkFileWord
              : P.labels.walkFilesWord}</summary
          >
          <ul class="walk-list">
            {#each walk.mechanical.files as path (path)}
              <li>{path}</li>
            {/each}
          </ul>
        </details>
      {/if}
      {#each walk.contradicted as claim (claim.path)}
        <div class="walk-contradicted">
          <span class="walk-path">{claim.path}</span>
          {#if claim.why}
            <div class="testimony" data-register="testimony">
              <div class="testimony-attribution">{attribution}</div>
              <div class="testimony-line">{claim.why}</div>
            </div>
          {/if}
          <div class="conflict" data-register="fact">
            {P.labels.walkContradictedNote}
          </div>
        </div>
      {/each}
    {:else if walk.rung === 3}
      <div class="walk-fact">{P.labels.walkNoCompareNote}</div>
      <ol class="walk-steps">
        {#each walk.steps as step, index (step.path)}
          <li class="walk-step focused">
            <div class="walk-step-head static">
              <span class="walk-idx">{index + 1}</span>
              <span class="walk-path">{step.path}</span>
            </div>
            {#if step.why}
              <div class="walk-step-body">
                <div class="testimony" data-register="testimony">
                  <div class="testimony-attribution">{attribution}</div>
                  <div class="testimony-line">{step.why}</div>
                </div>
              </div>
            {/if}
          </li>
        {/each}
      </ol>
      {#if walk.touched.length > 0}
        <div class="walk-fact">{P.labels.walkTouchedLabel}</div>
        <ul class="walk-list">
          {#each walk.touched as path (path)}
            <li>{path}</li>
          {/each}
        </ul>
      {/if}
    {:else if walk.rung === 4}
      <!-- Rung 4 declines to offer a walk (ADR 056 decision 6): stats and
           the touched list, each labelled as what it is, and nothing walks. -->
      <div class="walk-fact">{P.labels.walkDeclinedNote}</div>
      <div class="walk-fact">
        {P.labels.walkTouchedLabel}{P.punct.colon}
        {walk.touched.length}
      </div>
      <ul class="walk-list">
        {#each walk.touched as path (path)}
          <li>{path}</li>
        {/each}
      </ul>
    {:else}
      <div class="walk-fact">{P.labels.walkNothingNote}</div>
    {/if}

    {#if walk.deviations.length > 0}
      <div class="testimony" data-register="testimony">
        <div class="testimony-attribution">{attribution}</div>
        {#each walk.deviations as deviation (deviation)}
          <div class="testimony-line">
            <span class="dev-chip">{P.labels.deviationWord}</span><span
              >{deviation}</span
            >
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</details>

<style>
  /* Colors, sizes, and radii come from agents-theme.css tokens only; spacing
     follows run-view.css's 4/8/16 rhythm. */
  .walk {
    margin-top: 8px;
    border-top: 1px solid var(--line);
    padding-top: 4px;
  }
  .walk-summary {
    display: flex;
    align-items: baseline;
    gap: 8px;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
    user-select: none;
    width: fit-content;
  }
  .walk-summary:hover {
    color: var(--text-soft);
  }
  .walk-stats {
    color: var(--text-soft);
  }
  .add {
    color: var(--ok);
  }
  .del {
    color: var(--err);
  }
  .walk-fact {
    margin-top: 8px;
    color: var(--muted);
    font-size: var(--size-detail);
  }
  .walk-flag {
    display: inline-block;
    margin-right: 4px;
    padding: 0 4px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
  }
  .walk-nav {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }
  .walk-progress {
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
  }
  .walk-nav-btn {
    padding: 0 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-soft);
    font: var(--size-body-mono) var(--font-mono);
    cursor: pointer;
  }
  .walk-nav-btn:disabled {
    color: var(--dot-idle);
    border-color: var(--line);
    cursor: default;
  }
  .walk-nav-btn:not(:disabled):hover {
    background: var(--hover);
  }
  /* The phone reader gets intent, counts, and quoted rationale; patch
     reading is deferred to a desktop by design (ADR 056), so the note only
     exists where the diff controls are hidden. */
  .walk-phone-note {
    display: none;
    margin-left: auto;
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .walk-steps {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  .walk-step + .walk-step {
    border-top: 1px solid var(--line);
  }
  .walk-step-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    width: 100%;
    padding: 4px 8px;
    border: 0;
    background: transparent;
    text-align: left;
    font: var(--size-body-mono) var(--font-mono);
    color: var(--text);
    cursor: pointer;
  }
  .walk-step-head.static {
    cursor: default;
  }
  button.walk-step-head:hover {
    background: var(--hover);
  }
  .walk-step.focused > .walk-step-head {
    background: var(--code-bg);
  }
  .walk-idx {
    color: var(--muted);
    min-width: 16px;
  }
  .walk-path {
    font-family: var(--font-mono);
    font-size: var(--size-body-mono);
    overflow-wrap: anywhere;
  }
  .walk-file-stats {
    margin-left: auto;
    white-space: nowrap;
    font-size: var(--size-meta);
  }
  .walk-step-body {
    padding: 4px 8px 8px 32px;
  }
  .walk-diff-btn {
    margin-top: 8px;
    padding: 0 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
  }
  .walk-diff-btn:hover {
    background: var(--hover);
  }
  .walk-patch {
    margin: 8px 0 0;
    padding: 8px;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--panel-bg);
    font: var(--size-body-mono) var(--font-mono);
    line-height: 1.5;
    overflow-x: auto;
  }
  .pl {
    display: block;
    white-space: pre;
    min-height: 1em;
  }
  .pl-add {
    background: var(--ok-soft);
  }
  .pl-del {
    background: var(--err-bg);
  }
  .pl-hunk {
    color: var(--info);
  }
  .walk-retry {
    margin-left: 8px;
    padding: 0 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
  }
  .walk-mech {
    margin-top: 8px;
  }
  .walk-mech-summary {
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
    user-select: none;
    width: fit-content;
  }
  .walk-mech-summary:hover {
    color: var(--text-soft);
  }
  .walk-list {
    list-style: none;
    margin: 4px 0 0;
    padding: 0 0 0 16px;
    color: var(--text-soft);
    font: var(--size-body-mono) var(--font-mono);
  }
  .walk-contradicted {
    margin-top: 8px;
  }
  @media (max-width: 760px) {
    .walk-diff-btn,
    .walk-patch {
      display: none;
    }
    .walk-phone-note {
      display: inline;
    }
  }
</style>
