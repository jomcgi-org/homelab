<script>
  // Session-tier walkthrough (ADR 056): what did this turn change, and why.
  // Layout follows the agents-console walkthrough mock: a provenance banner,
  // a points list beside a detail pane, unexplained files as points in the
  // same list, a WHY block over a filebar and diff hunks. Two channels,
  // deliberately not merged: the points are the agent's own account, the
  // diff is the artifact, and the registers are expressed as style only.
  //
  // Collapsed by default; the composed payload is fetched once on first
  // open (the console polls, this never does), and per-point patches are
  // fetched lazily, on selection, desktop only.
  import { onMount, untrack } from "svelte";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { joinMeta } from "./run-format.js";
  import { walkthroughView, parsePatchHunks } from "./walkthrough.js";

  let {
    sessionId = null,
    turnSeq = null,
    model = "",
    // Fixture preview (?fixture=walk-*): payload/patches supplied inline,
    // so the preview never fetches.
    fixture = null,
  } = $props();

  let payload = $state(untrack(() => fixture?.payload ?? null));
  let loading = $state(false);
  let failed = $state(false);
  let opened = $state(untrack(() => Boolean(fixture)));
  let selected = $state(0);
  // Mobile pane swap: the points list and the detail pane are one column
  // under the console's 760px breakpoint, and selecting a point drills in.
  let drilled = $state(false);
  let patches = $state({});

  const walk = $derived(
    payload ? walkthroughView(payload, { sessionId, turnSeq }) : null,
  );
  const items = $derived(
    walk
      ? [
          ...walk.points,
          ...walk.contradictions.map((claim) => ({
            kind: "contradiction",
            path: claim.path,
            why: claim.why,
            attribution: claim.attribution,
            deviations: [],
            additions: 0,
            deletions: 0,
            touched: false,
            patchUrl: null,
          })),
        ]
      : [],
  );
  const point = $derived(items[selected] ?? null);

  function countLine(currentWalk) {
    const parts = [
      `${currentWalk.counts.accounted} ${P.labels.walkAccountedCount}`,
      `${currentWalk.counts.unexplained} ${P.labels.walkUnexplainedCount}`,
      `${currentWalk.counts.contradicted} ${P.labels.walkContradictedCount}`,
    ];
    if (currentWalk.counts.touched > 0) {
      parts.push(`${currentWalk.counts.touched} ${P.labels.walkTouchedCount}`);
    }
    return joinMeta(...parts);
  }

  function headerCountLine(currentWalk) {
    const parts = [countLine(currentWalk)];
    if (currentWalk.rung <= 2) {
      parts.push(
        `${currentWalk.counts.files} ${currentWalk.counts.files === 1 ? P.labels.walkFileChanged : P.labels.walkFilesChanged}`,
      );
    }
    return joinMeta(...parts);
  }

  function basename(path) {
    return String(path || "")
      .split("/")
      .pop();
  }

  function attributionLine(index, item) {
    return joinMeta(
      `${P.labels.walkPointWord} ${index + 1}`,
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

  function isDesktop() {
    return (
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 761px)").matches
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

  function onToggle(event) {
    opened = event.currentTarget.open;
    if (opened) load();
  }

  // A declaratively open details element does not reliably emit a toggle
  // event after Svelte attaches the listener. Start its one-shot load here;
  // onToggle shares the same guards if the browser emits both paths.
  onMount(() => {
    if (opened) load();
  });

  function selectItem(index) {
    selected = Math.max(0, Math.min(index, items.length - 1));
    drilled = true;
  }

  // Left/right arrows walk the points; up/down stay untouched so the
  // transcript keeps scrolling normally.
  function onKeydown(event) {
    if (items.length < 2) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      selectItem(selected - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      selectItem(selected + 1);
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

  // The phone reader gets intent, counts, and quoted rationale; patch
  // reading is deferred to a desktop by design (ADR 056), so a phone never
  // fetches what it will not render.
  $effect(() => {
    if (opened && point && isDesktop()) loadPatch(point);
  });

  function retry() {
    payload = null;
    load();
  }

  const showDiff = $derived(
    Boolean(point?.patchUrl || fixture?.patches?.[point?.path] !== undefined),
  );
</script>

<details class="walk" open={Boolean(fixture)} ontoggle={onToggle}>
  <summary class="walk-summary">
    <span>{P.labels.walkSummary}</span>
    {#if walk}
      <span class="walk-count">{headerCountLine(walk)}</span>
    {/if}
  </summary>

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
      <div class="walk-fact">
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
      </div>
    {:else if walk.summary?.status === "diff_unavailable"}
      <div class="walk-fact">{P.labels.walkSummaryDiffUnavailable}</div>
    {/if}
    {#if walk.message}
      <div class="walk-fact">{walk.message}</div>
    {/if}
    {#each walk.truncations as reason (reason)}
      <div class="walk-fact">{reason}</div>
    {/each}

    {#if items.length > 0 || walk.mechanical.length > 0}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div
        class="walk-frame"
        role="group"
        aria-label={P.labels.walkSummary}
        onkeydown={onKeydown}
      >
        {#if walk.hasTestimony}
          <div class="provenance">
            <b>{P.labels.walkProvenanceLead}</b>
            <span>{P.labels.walkProvenanceBody}</span>
          </div>
        {/if}
        <div class="wbody" class:drilled>
          <div class="points">
            {#each items as item, index (`${item.kind}:${item.path}`)}
              <button
                class="point"
                class:on={index === selected}
                class:unexplained={item.kind === "unexplained"}
                class:conflicted={item.kind === "contradiction"}
                type="button"
                aria-current={index === selected}
                onclick={() => selectItem(index)}
              >
                <span class="point-top">
                  <span class="point-n"
                    >{item.kind === "unexplained"
                      ? P.labels.walkUnexplainedMark
                      : item.kind === "contradiction"
                        ? P.labels.walkContradictedMark
                        : index + 1}</span
                  >
                  <span class="point-t">{basename(item.path)}</span>
                </span>
                <span class="point-f" dir="rtl">{item.path}</span>
              </button>
            {/each}
            {#each walk.mechanical as mech, index (index)}
              <div class="mech-row">
                {P.labels.walkMechanicalStep}{P.punct.colon}
                {mech.count}
                {mech.count === 1
                  ? P.labels.walkFileWord
                  : P.labels.walkFilesWord}
                {#if mech.generator}<span class="mech-gen"
                    >{mech.generator}</span
                  >{/if}
              </div>
            {/each}
          </div>

          <div class="detail">
            <button
              class="walk-back"
              type="button"
              onclick={() => (drilled = false)}>{P.labels.mobileBack}</button
            >
            {#if point}
              {#if point.kind === "unexplained"}
                <div class="nowhy">
                  <b>{P.labels.walkUnexplainedTitle}</b>
                  {P.labels.walkUnexplainedBody}
                </div>
              {:else if point.kind === "contradiction"}
                {#if point.why}
                  <div class="why">
                    <div class="why-lbl">
                      {attributionLine(selected, point)}
                    </div>
                    <div class="why-txt">{point.why}</div>
                  </div>
                {/if}
                <div class="nowhy">
                  <b>{P.labels.walkContradictedTitle}</b>
                  {P.labels.walkContradictedBody}
                </div>
              {:else if point.why}
                <div class="why">
                  <div class="why-lbl">{attributionLine(selected, point)}</div>
                  <div class="why-txt">{point.why}</div>
                </div>
              {/if}
              {#each point.deviations as deviation (deviation)}
                <div class="why-dev">
                  <span class="dev-chip">{P.labels.deviationWord}</span><span
                    >{deviation}</span
                  >
                </div>
              {/each}
              {#if showDiff}
                <div class="filebar">
                  <span class="filebar-path">{point.path}</span>
                  <span class="filebar-stat">
                    <span class="plus">+{point.additions}</span>
                    <span class="minus">&minus;{point.deletions}</span>
                  </span>
                </div>
                <div class="walk-phone-note">{P.labels.walkDiffsDesktop}</div>
                <div class="hunks">
                  {#if patches[point.path]?.loading}
                    <div class="walk-fact pad16">
                      {P.labels.walkDiffLoading}
                    </div>
                  {:else if patches[point.path]?.failed}
                    <div class="walk-fact pad16">
                      {P.labels.walkDiffUnavailable}
                    </div>
                  {:else if patches[point.path]?.hunks}
                    {#each patches[point.path].hunks as hunk, hunkIndex (hunkIndex)}
                      <div class="hunk">
                        <div class="hunk-inner">
                          {#if hunk.header}
                            <div class="hunk-head">{hunk.header}</div>
                          {/if}
                          {#each hunk.lines as line, lineIndex (lineIndex)}
                            <span class={`dl ${line.kind}`}
                              ><span class="g">{line.gutter}</span
                              >{line.text}</span
                            >
                          {/each}
                        </div>
                      </div>
                    {/each}
                  {/if}
                </div>
              {/if}
            {/if}
          </div>
        </div>
      </div>
    {/if}

    {#if walk.rung === 4 && walk.touched.length > 0}
      <div class="walk-fact">{P.labels.walkTouchedLabel}</div>
      <ul class="walk-list">
        {#each walk.touched as path (path)}
          <li>{path}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</details>

<style>
  /* Colors, sizes, and radii come from agents-theme.css tokens (including
     the diff tokens added for this surface); spacing follows the console's
     existing 4/8/16 rhythm. */
  .walk {
    margin-top: 8px;
    border-top: 1px solid var(--line);
    padding-top: 4px;
  }
  .walk-summary {
    display: flex;
    align-items: baseline;
    gap: 8px;
    width: fit-content;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
    user-select: none;
  }
  .walk-summary:hover {
    color: var(--text-soft);
  }
  .walk-count {
    color: var(--text-soft);
  }
  .walk-fact {
    margin-top: 8px;
    color: var(--muted);
    font-size: var(--size-detail);
  }
  .walk-fact.pad16 {
    margin: 12px 16px;
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
  .walk-frame {
    margin-top: 8px;
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  /* The provenance banner is the register statement for the whole surface:
     amber ground marks the agent's-account channel, once, instead of a
     per-element chip naming the register. */
  .provenance {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 8px 12px;
    background: var(--attn-soft);
    border-bottom: 1px solid var(--line);
    font-size: var(--size-meta);
    color: var(--text);
  }
  .provenance b {
    color: var(--attn-text);
  }
  .wbody {
    display: grid;
    grid-template-columns: 290px minmax(0, 1fr);
  }
  .points {
    border-right: 1px solid var(--line);
    background: var(--page-bg);
    max-height: 420px;
    overflow: auto;
  }
  .point {
    display: grid;
    gap: 3px;
    width: 100%;
    padding: 8px 12px;
    border: 0;
    border-bottom: 1px solid var(--line);
    background: transparent;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }
  .point:hover {
    background: var(--hover);
  }
  /* Selection is an inset ink edge, not a background swap, so the
     unexplained red ground survives selection. */
  .point.on {
    background: var(--panel-bg);
    box-shadow: inset 2px 0 0 var(--ink);
  }
  .point-top {
    display: flex;
    gap: 8px;
    align-items: baseline;
  }
  .point-n {
    flex: none;
    font: var(--size-meta) var(--font-mono);
    color: var(--muted);
  }
  .point-t {
    font-size: var(--size-detail);
    font-weight: 600;
    letter-spacing: -0.004em;
  }
  /* rtl so a long path keeps its end, the part that identifies the file. */
  .point-f {
    font: var(--size-meta) var(--font-mono);
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
  }
  .point.unexplained {
    background: var(--err-bg);
  }
  .point.unexplained .point-t {
    color: var(--err);
  }
  .point.unexplained.on {
    background: var(--err-bg);
  }
  .point.conflicted .point-t {
    color: var(--attn-text);
  }
  .mech-row {
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
    font: var(--size-meta) var(--font-mono);
    color: var(--muted);
  }
  .mech-gen {
    display: block;
    margin-top: 2px;
    color: var(--text-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .detail {
    min-width: 0;
    max-height: 420px;
    overflow: auto;
    background: var(--panel-bg);
  }
  .walk-back {
    display: none;
    margin: 8px 12px 0;
    padding: 0 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
    cursor: pointer;
  }
  .why {
    margin: 12px 16px;
    display: grid;
    gap: 6px;
  }
  .why-lbl {
    font: var(--size-meta) var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--muted);
  }
  /* The agent's words, quoted: amber edge for the account channel, never
     restated in system voice. */
  .why-txt {
    font-size: var(--size-detail);
    border-left: 2px solid var(--attn);
    padding-left: 12px;
    text-wrap: pretty;
  }
  .why-dev {
    margin: 0 16px 12px;
    font-size: var(--size-detail);
  }
  .nowhy {
    margin: 12px 16px;
    padding: 10px 12px;
    background: var(--err-bg);
    border-radius: var(--radius-md);
    font-size: var(--size-detail);
  }
  .nowhy b {
    color: var(--err);
  }
  .filebar {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 8px 16px;
    border-block: 1px solid var(--line);
    background: var(--page-bg);
    font: var(--size-body-mono) var(--font-mono);
  }
  .filebar-path {
    overflow-wrap: anywhere;
  }
  .filebar-stat {
    margin-left: auto;
    flex: none;
    font-variant-numeric: tabular-nums;
  }
  .plus {
    color: var(--diff-add-mark);
  }
  .minus {
    color: var(--diff-del-mark);
  }
  .hunk {
    border-bottom: 1px solid var(--line);
    overflow-x: auto;
  }
  .hunk-inner {
    width: max-content;
    min-width: 100%;
  }
  .hunk-head {
    position: sticky;
    left: 0;
    padding: 4px 16px;
    background: var(--code-bg);
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
  }
  .dl {
    display: block;
    padding: 1px 16px 1px 30px;
    text-indent: -14px;
    font: var(--size-body-mono) var(--font-mono);
    line-height: 1.6;
    white-space: pre;
    color: var(--text-soft);
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
  .walk-list {
    list-style: none;
    margin: 4px 0 0;
    padding: 0 0 0 16px;
    color: var(--text-soft);
    font: var(--size-body-mono) var(--font-mono);
  }
  .walk-phone-note {
    display: none;
    margin: 8px 16px 0;
    color: var(--muted);
    font-size: var(--size-meta);
  }
  @media (max-width: 760px) {
    .wbody {
      grid-template-columns: 1fr;
    }
    .points {
      border-right: 0;
    }
    .wbody:not(.drilled) .detail {
      display: none;
    }
    .wbody.drilled .points {
      display: none;
    }
    .walk-back {
      display: inline-flex;
    }
    .hunks {
      display: none;
    }
    .walk-phone-note {
      display: block;
    }
  }
</style>
