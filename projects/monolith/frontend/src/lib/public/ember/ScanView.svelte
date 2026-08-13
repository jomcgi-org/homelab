<script>
  import { onDestroy } from "svelte";

  // ScanView: editor, gutter/ignition, sweep, receipt line, and the
  // cold-vs-warm race. Ported from the interactive spec at
  // the ember semgrep scanview mockup (the target design, referenced in a
  // browser, everything inside the white panel is this component + the
  // page shell around it). Session/Turnstile gating, proxies, and the
  // busy/queued/error states live one level up in +page.svelte; this
  // component only knows about the editing buffer, the scan response, and
  // the choreography that plays once a response lands.
  //
  // Editor technique: the real <textarea> stays the editable surface (users
  // must be able to type/paste), with a mirrored per-line <pre> row render
  // behind it carrying the gutter, the sweep highlight, and finding flags.
  // The current page already synced a plain line-number gutter to the
  // textarea's scrollTop; this extends that to a full per-row mirror so a
  // finding's row can light up and pin a flag chip without touching the
  // textarea's own text rendering.
  let {
    examples,
    language = $bindable(""),
    code = $bindable(""),
    activeExampleIndex = $bindable(-1),
    scanning = false,
    queuedNarration = false,
    sessionReady = false,
    scanUnavailable = false,
    overCap = false,
    scanError = "",
    busyWaiting = null,
    result = null,
    onscan = () => {},
    onpickexample = () => {},
  } = $props();

  const MAX_LINES = 200;
  const MAX_CHARS = 8_000;

  let lineCount = $derived(code.length === 0 ? 1 : code.split("\n").length);
  let charCount = $derived(code.length);
  let linesWarn = $derived(lineCount > MAX_LINES * 0.9);
  let charsWarn = $derived(charCount > MAX_CHARS * 0.9);

  let codeLines = $derived(code.length === 0 ? [""] : code.split("\n"));

  let textareaEl;
  let mirrorEl;

  function syncMirrorScroll() {
    if (mirrorEl && textareaEl) {
      mirrorEl.scrollTop = textareaEl.scrollTop;
      mirrorEl.scrollLeft = textareaEl.scrollLeft;
    }
  }

  // ---------------------------------------------------------------------
  // Findings-by-line lookup. A line can carry at most one flag chip (the
  // mock's rows show one), keyed by 1-based line number.
  // ---------------------------------------------------------------------
  let findings = $derived(result?.findings ?? []);

  let findingByLine = $derived.by(() => {
    const m = new Map();
    for (const f of findings) {
      if (!m.has(f.line)) m.set(f.line, f);
    }
    return m;
  });

  let highlightedLine = $state(null);

  // ---------------------------------------------------------------------
  // Sweep + ignition choreography, ported from the mock's runJourney().
  // Plays AFTER the scan response arrives, since findings (and their line
  // fractions) aren't known until then. Reduced motion: skip straight to
  // the end state, no rAF loop.
  // ---------------------------------------------------------------------
  const reduced =
    typeof matchMedia === "function"
      ? matchMedia("(prefers-reduced-motion: reduce)").matches
      : false;

  let sweepTop = $state(0);
  let sweepOpacity = $state(0);
  let litLines = $state(new Set());
  let journeyPhase = $state("idle"); // idle | restored | sweeping | done
  let receiptText = $state("");
  let sweepRaf = null;

  // Race state, ported from startRace().
  const BOOT_END = 0.25;
  const RULES_END = 0.93;
  let raceVisible = $state(false);
  let raceRan = false;
  let oursPct = $state(0);
  let ghostPct = $state(0);
  let oursStat = $state("");
  let oursDone = $state(false);
  let ghostStat = $state("");
  let ghostDone = $state(false);
  let segShow = $state([false, false, false]);
  let raceRaf = null;
  let raceTimeout = null;

  onDestroy(() => {
    if (sweepRaf) cancelAnimationFrame(sweepRaf);
    if (raceRaf) cancelAnimationFrame(raceRaf);
    if (raceTimeout) clearTimeout(raceTimeout);
  });

  function resetJourney() {
    if (sweepRaf) cancelAnimationFrame(sweepRaf);
    if (raceRaf) cancelAnimationFrame(raceRaf);
    if (raceTimeout) clearTimeout(raceTimeout);
    sweepRaf = null;
    raceRaf = null;
    raceTimeout = null;
    sweepTop = 0;
    sweepOpacity = 0;
    litLines = new Set();
    journeyPhase = scanning ? "restored" : "idle";
    receiptText = "";
    raceVisible = false;
    raceRan = false;
    oursPct = 0;
    ghostPct = 0;
    oursStat = "";
    oursDone = false;
    ghostStat = "";
    ghostDone = false;
    segShow = [false, false, false];
  }

  // Drives the whole post-response journey. scan_ms and cold_start_ms come
  // straight off the response the page already fetched.
  function playJourney(response) {
    if (sweepRaf) cancelAnimationFrame(sweepRaf);
    if (raceRaf) cancelAnimationFrame(raceRaf);
    if (raceTimeout) clearTimeout(raceTimeout);
    raceRan = false;
    raceVisible = false;
    litLines = new Set();
    highlightedLine = null;
    journeyPhase = "sweeping";

    const scanMs = response.scan_ms ?? 0;
    const sweepMs = Math.max(scanMs, 700);
    const lineOrder = codeLines.map((_, i) => i);
    const total = Math.max(lineOrder.length, 1);
    // The sweep must travel the rows' actual pixel height, and lines must
    // ignite by pixel comparison against it. scrollHeight clamps to the
    // 320px viewport when the snippet is shorter, so using it (or line
    // fractions of the duration) makes findings light up after the sweep
    // has already passed their row.
    const rowH = mirrorEl?.querySelector(".row")?.offsetHeight ?? 0;
    const boxH = rowH > 0 ? rowH * total : (mirrorEl?.scrollHeight ?? 0);

    if (reduced) {
      sweepOpacity = 0;
      litLines = new Set(
        [...findingByLine.keys()].map((line) => line - 1).filter((i) => i >= 0),
      );
      finishSweep(response);
      return;
    }

    const t0 = performance.now();
    function frame(now) {
      const p = Math.min(1, (now - t0) / sweepMs);
      sweepOpacity = p < 1 ? 1 : 0;
      sweepTop = p * boxH;
      const next = new Set(litLines);
      let changed = false;
      lineOrder.forEach((i) => {
        const line = i + 1;
        if (
          findingByLine.has(line) &&
          !next.has(i) &&
          sweepTop >= (i + 0.5) * (boxH / total)
        ) {
          next.add(i);
          changed = true;
        }
      });
      if (changed) litLines = next;
      if (p < 1) {
        sweepRaf = requestAnimationFrame(frame);
      } else {
        finishSweep(response);
      }
    }
    sweepRaf = requestAnimationFrame(frame);
  }

  function finishSweep(response) {
    const n = findings.length;
    const secs = ((response.scan_ms ?? 0) / 1000).toFixed(2);
    receiptText = `${n} finding${n === 1 ? "" : "s"} · scanned in ${secs} s`;
    journeyPhase = "done";
    raceTimeout = setTimeout(() => startRace(response), 700);
  }

  function startRace(response) {
    if (raceRan) return;
    raceRan = true;
    raceVisible = true;

    const scanMs = response.scan_ms ?? 0;
    const coldMs = (response.cold_start_ms ?? 0) + scanMs;
    const oursSecs = (scanMs / 1000).toFixed(2);
    const coldSecs = (coldMs / 1000).toFixed(1);

    if (reduced) {
      oursPct = 100;
      ghostPct = 100;
      oursStat = `done · ${oursSecs} s`;
      oursDone = true;
      ghostStat = `done · ${coldSecs} s`;
      ghostDone = true;
      segShow = [true, true, true];
      return;
    }

    const t0 = performance.now();
    function frame(now) {
      const el = now - t0;
      const gp = Math.min(1, coldMs > 0 ? el / coldMs : 1);
      oursPct = Math.min(100, scanMs > 0 ? (el / scanMs) * 100 : 100);
      ghostPct = gp * 100;
      if (el >= scanMs && !oursDone) {
        oursStat = `done · ${oursSecs} s`;
        oursDone = true;
      }
      const next = [...segShow];
      if (gp >= 0) next[0] = true;
      if (gp >= BOOT_END) next[1] = true;
      if (gp >= RULES_END) next[2] = true;
      segShow = next;
      if (gp < 1) {
        ghostStat = `${((coldMs - el) / 1000).toFixed(1)} s to go`;
        raceRaf = requestAnimationFrame(frame);
      } else {
        ghostStat = `done · ${coldSecs} s`;
        ghostDone = true;
      }
    }
    raceRaf = requestAnimationFrame(frame);
  }

  // Called by the page once a scan response has landed (findings + timings
  // known). Exported so +page.svelte can drive the choreography after its
  // own fetch/session/error handling settles.
  export function runJourney(response) {
    playJourney(response);
  }

  export function resetView() {
    resetJourney();
  }

  $effect(() => {
    if (scanning) {
      journeyPhase = "restored";
    }
  });

  function selectFinding(f) {
    highlightedLine = f.line;
  }

  // Rule ids are dot-separated paths whose specificity increases left to
  // right (etc.semgrep.rules.pro-javascript....express-child-process), so
  // the inline flag chip keeps whole trailing segments within its width
  // budget and drops the registry-boilerplate prefix behind an ellipsis.
  // The findings list below still shows the full id.
  const FLAG_ID_BUDGET = 28;

  function shortRuleId(id) {
    if (!id || id.length <= FLAG_ID_BUDGET) return id;
    const segs = id.split(".");
    let out = segs.pop() ?? "";
    while (
      segs.length > 0 &&
      out.length + segs[segs.length - 1].length + 1 <= FLAG_ID_BUDGET
    ) {
      out = `${segs.pop()}.${out}`;
    }
    return `…${out}`;
  }

  let sortedFindings = $derived.by(() => {
    const order = { ERROR: 0, WARNING: 1, INFO: 2 };
    return [...findings].sort(
      (a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3),
    );
  });

  let scanErrors = $derived(result?.errors ?? []);

  // Example chips grouped by consecutive language runs so each language
  // gets a small caption and a divider; indices stay global because
  // onpickexample addresses the flat examples array.
  let exampleGroups = $derived.by(() => {
    const groups = [];
    examples.forEach((ex, i) => {
      const last = groups[groups.length - 1];
      if (last && last.language === ex.language) {
        last.items.push({ ex, i });
      } else {
        groups.push({ language: ex.language, items: [{ ex, i }] });
      }
    });
    return groups;
  });
</script>

<div class="sv">
  <div class="editor">
    <div class="editor-head">
      <div class="examples">
        {#each exampleGroups as group (group.language)}
          <div class="ex-group">
            <span class="ex-lang">{group.language}</span>
            <div class="ex-chips">
              {#each group.items as { ex, i } (i)}
                <button
                  type="button"
                  class="ex-chip"
                  class:on={activeExampleIndex === i}
                  onclick={() => onpickexample(i)}
                >
                  {ex.label}
                </button>
              {/each}
            </div>
          </div>
        {/each}
      </div>
      <button
        type="button"
        class="scan-btn"
        disabled={!sessionReady || scanUnavailable || scanning || overCap}
        onclick={onscan}
      >
        {#if scanning}
          {queuedNarration ? "queued…" : "scanning…"}
        {:else if scanUnavailable}
          scan unavailable
        {:else if overCap}
          snippet too large
        {:else}
          scan
        {/if}
      </button>
    </div>

    <div class="editor-body">
      <div class="codebox" bind:this={mirrorEl} aria-hidden="true">
        <div
          class="sweep"
          style:opacity={sweepOpacity}
          style:top="{sweepTop}px"
        ></div>
        {#each codeLines as line, i (i)}
          {@const lineNo = i + 1}
          {@const finding = findingByLine.get(lineNo)}
          <div
            class="row"
            class:lit={litLines.has(i)}
            class:pinned={lineNo === highlightedLine}
          >
            <span class="no">{lineNo}</span>
            <pre>{line}</pre>
            {#if finding}
              <span
                class="flag"
                class:err={finding.severity === "ERROR"}
                class:warn={finding.severity !== "ERROR"}
              >
                {shortRuleId(finding.rule_id)}
              </span>
            {/if}
          </div>
        {/each}
      </div>
      <textarea
        class="code-input"
        class:code-input-over={overCap}
        bind:this={textareaEl}
        bind:value={code}
        onscroll={syncMirrorScroll}
        spellcheck="false"
        autocapitalize="off"
        autocorrect="off"
        aria-label="code snippet to scan"></textarea>
    </div>

    <div class="editor-footer">
      <span class="counter" class:counter-warn={linesWarn}
        >{lineCount} / {MAX_LINES} lines</span
      >
      <span class="counter" class:counter-warn={charsWarn}
        >{charCount} / {MAX_CHARS} chars</span
      >
    </div>
  </div>

  {#if scanError}
    <p class="run-error">{scanError}</p>
  {/if}
  {#if busyWaiting !== null}
    <p class="busy-notice">
      demo is busy, {busyWaiting} waiting, try again in a moment
    </p>
  {/if}

  <p class="journey-line">
    {#if journeyPhase === "restored"}
      <span class="dim">microVM restored · 21 ms</span>
    {:else if journeyPhase === "done"}
      {receiptText}
    {:else}
      &nbsp;
    {/if}
  </p>

  <div class="race" class:race-visible={raceVisible}>
    <p class="race-title">the same scan, cold</p>
    <div class="lane ours">
      <span class="label"
        >warm restore<small>rules already in memory</small></span
      >
      <div class="track"><div class="fill" style:width="{oursPct}%"></div></div>
      <span class="stat" class:done={oursDone}>{oursStat || " "}</span>
    </div>
    <div class="lane ghost">
      <span class="label"
        >cold start<small>boot VM · load 1,600 rules · scan</small></span
      >
      <div class="track">
        <div class="fill" style:width="{ghostPct}%"></div>
        <span class="seg" class:show={segShow[0]} style:left="2%">booting…</span
        >
        <span class="seg" class:show={segShow[1]} style:left="{BOOT_END * 100}%"
          >loading rules…</span
        >
        <span
          class="seg"
          class:show={segShow[2]}
          style:left="{RULES_END * 100}%">scanning…</span
        >
      </div>
      <span class="stat">{ghostStat || " "}</span>
    </div>
  </div>

  <div class="results-card">
    {#if sortedFindings.length === 0 && scanErrors.length === 0}
      <p class="empty-state">
        {#if result}
          no findings in this snippet. try another.
        {:else}
          findings land on their lines as the scan passes them.
        {/if}
      </p>
    {:else}
      <ul class="findings-list">
        {#each sortedFindings as f, i (i)}
          <li>
            <button
              type="button"
              class="finding-row"
              class:finding-row-active={f.line === highlightedLine}
              onclick={() => selectFinding(f)}
            >
              <span class="severity-badge severity-{f.severity?.toLowerCase()}"
                >{f.severity}</span
              >
              <span class="finding-main">
                <span class="finding-rule">{f.rule_id}</span>
                <span class="finding-message">{f.message}</span>
              </span>
              <span class="finding-loc">{f.line}:{f.col}</span>
            </button>
          </li>
        {/each}
      </ul>
      {#if scanErrors.length > 0}
        <p class="scan-errors-note">
          {scanErrors.length} rule error{scanErrors.length === 1 ? "" : "s"} during
          this scan
        </p>
      {/if}
    {/if}
  </div>
</div>

<style>
  .sv {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* ---------- editor ---------- */
  .editor {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .editor-head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--em-line);
    background: var(--em-ground);
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
  }

  .examples {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
  }

  .ex-group {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .ex-group + .ex-group {
    border-left: 1px solid var(--em-line);
    padding-left: 14px;
  }

  .ex-lang {
    font-size: 10px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--em-faint);
    user-select: none;
  }

  .ex-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .ex-chip {
    font-family: var(--em-mono);
    font-size: 11.5px;
    padding: 3px 10px;
    border: 1px solid var(--em-line);
    border-radius: 999px;
    background: var(--em-panel);
    color: var(--em-muted);
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      color 0.15s ease;
  }

  .ex-chip:hover {
    border-color: var(--em-faint);
  }

  .ex-chip.on {
    border-color: var(--em-ember);
    color: var(--em-ember-deep);
  }

  .scan-btn {
    font-family: inherit;
    font-weight: 600;
    font-size: 13.5px;
    padding: 7px 20px;
    background: var(--em-ember);
    border: 1px solid var(--em-ember-deep);
    color: var(--em-on-color);
    border-radius: 8px;
    cursor: pointer;
    min-width: 110px;
    transition: background-color 0.15s ease;
  }

  .scan-btn:hover:not(:disabled) {
    background: var(--em-ember-deep);
  }

  .scan-btn:disabled {
    background: var(--em-ember-dim);
    border-color: var(--em-ember-dim);
    cursor: default;
  }

  /* ---------- editor body: mirror + real textarea stacked ---------- */
  .editor-body {
    position: relative;
    height: 320px;
  }

  .codebox {
    position: absolute;
    inset: 0;
    overflow: auto;
    font-family: var(--em-mono);
    font-size: 13px;
    line-height: 1.5;
    pointer-events: none;
  }

  .row {
    display: flex;
    min-width: max-content;
    position: relative;
    padding: 0 190px 0 8px;
    transition: background-color 0.35s ease;
  }

  @media (prefers-reduced-motion: reduce) {
    .row {
      transition: none;
    }
  }

  .row .no {
    width: 3.5ch;
    flex: 0 0 3.5ch;
    text-align: right;
    padding-right: 10px;
    color: var(--em-faint);
    user-select: none;
  }

  .row pre {
    margin: 0;
    white-space: pre;
    color: transparent;
  }

  .row.lit {
    background: color-mix(in srgb, var(--em-ember-dim) 45%, transparent);
  }

  .row.lit .no {
    color: var(--em-ember-deep);
    font-weight: 700;
  }

  .row.pinned {
    background: color-mix(in srgb, var(--em-frost-dim) 45%, transparent);
  }

  .flag {
    position: absolute;
    right: 10px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 11px;
    font-family: var(--em-mono);
    padding: 1px 8px;
    border-radius: 4px;
    color: var(--em-on-color);
    white-space: nowrap;
    max-width: 172px;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0;
    translate: 8px 0;
    transition:
      opacity 0.3s ease,
      translate 0.3s ease;
  }

  @media (prefers-reduced-motion: reduce) {
    .flag {
      transition: none;
    }
  }

  .row.lit .flag {
    opacity: 1;
    translate: 0 0;
  }

  .flag.err {
    background: var(--em-ember);
  }

  .flag.warn {
    background: var(--em-amber);
    color: var(--em-ink);
  }

  .sweep {
    position: absolute;
    left: 0;
    right: 0;
    height: 2px;
    background: var(--em-ember);
    box-shadow: 0 0 10px 1px
      color-mix(in srgb, var(--em-ember) 50%, transparent);
    pointer-events: none;
  }

  .code-input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0 190px 0 calc(3.5ch + 8px + 10px);
    border: none;
    resize: none;
    background: transparent;
    color: var(--em-ink);
    font-family: var(--em-mono);
    font-size: 13px;
    line-height: 1.5;
    outline: none;
    caret-color: var(--em-ink);
  }

  .code-input-over {
    color: var(--em-ember-deep);
  }

  .editor-footer {
    display: flex;
    justify-content: flex-end;
    gap: 14px;
    padding: 8px 12px;
    border-top: 1px solid var(--em-line);
  }

  .counter {
    font-family: var(--em-mono);
    font-size: 11px;
    color: var(--em-faint);
    font-variant-numeric: tabular-nums;
  }

  .counter-warn {
    color: var(--em-ember-deep);
    font-weight: 600;
  }

  .run-error {
    margin: 0;
    color: var(--em-ember-deep);
    font-size: 13px;
  }

  .busy-notice {
    margin: 0;
    color: var(--em-frost);
    font-size: 13px;
  }

  /* ---------- journey line ---------- */
  .journey-line {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 13px;
    min-height: 1.6em;
    color: var(--em-ink);
  }

  .journey-line .dim {
    color: var(--em-faint);
  }

  /* ---------- race ---------- */
  .race {
    display: flex;
    flex-direction: column;
    gap: 10px;
    border-top: 1px dashed var(--em-line);
    padding-top: 16px;
    visibility: hidden;
  }

  .race.race-visible {
    visibility: visible;
  }

  .race-title {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
  }

  .lane {
    display: grid;
    grid-template-columns: 190px 1fr 130px;
    gap: 12px;
    align-items: center;
    font-family: var(--em-mono);
    font-size: 12px;
  }

  .lane .label {
    color: var(--em-muted);
    text-align: right;
    line-height: 1.3;
  }

  .lane .label small {
    display: block;
    color: var(--em-faint);
    font-size: 10.5px;
  }

  .track {
    height: 8px;
    background: var(--em-track);
    border-radius: 999px;
    overflow: hidden;
    position: relative;
  }

  .fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.1s linear;
  }

  @media (prefers-reduced-motion: reduce) {
    .fill {
      transition: none;
    }
  }

  .lane.ours .fill {
    background: var(--em-ember);
  }

  .lane.ghost .fill {
    background: var(--em-frost);
    opacity: 0.55;
  }

  .seg {
    position: absolute;
    top: -16px;
    font-size: 10px;
    color: var(--em-faint);
    opacity: 0;
    transition: opacity 0.4s ease;
  }

  @media (prefers-reduced-motion: reduce) {
    .seg {
      transition: none;
    }
  }

  .seg.show {
    opacity: 1;
  }

  .lane .stat {
    color: var(--em-faint);
    font-variant-numeric: tabular-nums;
  }

  .lane.ours .stat.done {
    color: var(--em-ember-deep);
  }

  /* ---------- results ---------- */
  .results-card {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
    padding: 8px;
    min-height: 160px;
    display: flex;
    flex-direction: column;
  }

  .empty-state {
    margin: auto;
    padding: 24px;
    text-align: center;
    font-size: 13.5px;
    color: var(--em-faint);
  }

  .findings-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .finding-row {
    width: 100%;
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 10px 8px;
    border: none;
    border-bottom: 1px solid var(--em-line-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
    font-family: inherit;
    transition: background-color 0.15s ease;
  }

  .finding-row:hover {
    background: var(--em-ground);
  }

  .finding-row-active {
    background: color-mix(in srgb, var(--em-ember-dim) 35%, transparent);
  }

  .severity-badge {
    flex: none;
    padding: 2px 7px;
    border-radius: 999px;
    font-family: var(--em-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--em-on-color);
    background: var(--em-faint);
  }

  .severity-error {
    background: var(--em-ember-deep);
  }

  .severity-warning {
    background: var(--em-amber);
    color: var(--em-ink);
  }

  .severity-info {
    background: var(--em-frost);
  }

  .finding-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .finding-rule {
    font-family: var(--em-mono);
    font-size: 11.5px;
    color: var(--em-ember-deep);
  }

  .finding-message {
    font-size: 13px;
    color: var(--em-ink);
    overflow-wrap: anywhere;
  }

  .finding-loc {
    flex: none;
    font-family: var(--em-mono);
    font-size: 11.5px;
    color: var(--em-faint);
    font-variant-numeric: tabular-nums;
  }

  .scan-errors-note {
    margin: 8px 4px 2px;
    font-size: 11.5px;
    color: var(--em-faint);
  }

  @media (max-width: 640px) {
    .lane {
      grid-template-columns: 1fr;
      gap: 4px;
    }

    .lane .label {
      text-align: left;
    }
  }
</style>
