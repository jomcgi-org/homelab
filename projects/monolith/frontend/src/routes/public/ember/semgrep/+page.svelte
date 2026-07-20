<script>
  // /ember/semgrep: the production security scanner (Semgrep Pro, ~1,600
  // rules, cross-function taint within the file), pointed at a visitor's own
  // snippet through the same warm fc-invoke path this cluster's own CI scans
  // through. Turnstile-gated, session-cookie-gated, rate-bucketed, and
  // queue-bounded on the backend (ember_public/semgrep_router.py); this
  // component only talks to same-origin proxies under /ember/semgrep/api
  // (see the +server.js routes beside this page), never /api directly: the
  // public tier's rule 2 (public-tier-checklist.md).
  //
  // Visual language: the /ember/* pages are their own small site in the
  // fcstory palette (lib/public/ember/ember.css), not the neobrutalist
  // jomcgi.dev baseline. The topbar wordmark is the only nav: it links home.
  import { onMount } from "svelte";
  import "$lib/public/ember/ember.css";

  let { data } = $props();

  const API = "/ember/semgrep/api";

  // ---------------------------------------------------------------------
  // Canned examples: shipped verbatim from the design plan, verified to
  // fire Pro taint rules on the warm path. Default buffer is the first
  // python example.
  // ---------------------------------------------------------------------
  const EXAMPLES = [
    {
      language: "python",
      label: "command injection across functions",
      code: `import os

from flask import Flask, request

app = Flask(__name__)


def build_command():
    tool = request.args.get("tool")
    return f"/usr/bin/{tool} --report"


@app.route("/run")
def run():
    os.system(build_command())
    return "started"
`,
    },
    {
      language: "python",
      label: "SQL injection",
      code: `import sqlite3

from flask import Flask, request

app = Flask(__name__)


@app.route("/user")
def user():
    name = request.args.get("name")
    db = sqlite3.connect("app.db")
    row = db.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()
    return str(row)
`,
    },
    {
      language: "javascript",
      label: "command injection across functions",
      code: `const express = require("express");
const { exec } = require("child_process");

const app = express();

function buildCommand(req) {
  return "convert " + req.query.file + " out.png";
}

app.get("/convert", (req, res) => {
  exec(buildCommand(req), () => res.send("ok"));
});
`,
    },
    {
      language: "javascript",
      label: "code injection via eval",
      code: `const express = require("express");

const app = express();

app.get("/calc", (req, res) => {
  const result = eval(req.query.expr);
  res.send(String(result));
});
`,
    },
  ];

  const MAX_LINES = 200;
  const MAX_CHARS = 8_000;

  let language = $state(EXAMPLES[0].language);
  let code = $state(EXAMPLES[0].code);
  let activeExampleIndex = $state(0);

  let lineCount = $derived(code.length === 0 ? 1 : code.split("\n").length);
  let charCount = $derived(code.length);
  let linesWarn = $derived(lineCount > MAX_LINES * 0.9);
  let charsWarn = $derived(charCount > MAX_CHARS * 0.9);
  let overCap = $derived(lineCount > MAX_LINES || charCount > MAX_CHARS);

  // Gutter line numbers, synced to the textarea via a scroll listener rather
  // than a shared-scroll container hack: both elements use the same
  // font/line-height/padding so line N in the gutter lines up with line N in
  // the textarea.
  let lineNumbers = $derived(
    Array.from({ length: Math.max(lineCount, 1) }, (_, i) => i + 1),
  );

  let textareaEl;
  let gutterEl;

  function syncGutterScroll() {
    if (gutterEl && textareaEl) gutterEl.scrollTop = textareaEl.scrollTop;
  }

  function pickExample(index) {
    activeExampleIndex = index;
    const ex = EXAMPLES[index];
    language = ex.language;
    code = ex.code;
    highlightedLine = null;
    findings = [];
    scanErrors = [];
    lastResult = null;
  }

  function setLanguage(lang) {
    language = lang;
    // Switching language with no matching example loaded keeps the current
    // buffer; the picker below only lists examples for the active language.
  }

  let examplesForLanguage = $derived(
    EXAMPLES.map((ex, i) => ({ ...ex, i })).filter(
      (ex) => ex.language === language,
    ),
  );

  // ---------------------------------------------------------------------
  // Turnstile + session. Mirrors the postgres page's EmberConsole wiring:
  // a widget renders above the scan button on first load when a site key is
  // configured, and the scan button stays disabled until the solved token
  // mints a session. When no site key is configured (dev), mint sessionlessly
  // on mount, matching the backend's private-tier allowance.
  // ---------------------------------------------------------------------
  let sessionReady = $state(false);
  let sessionError = $state("");
  let scanUnavailable = $state(false);

  async function parseJsonSafe(resp) {
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  async function mintSession(turnstileToken = "") {
    try {
      const resp = await fetch(`${API}/session`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ turnstile_token: turnstileToken }),
      });
      if (resp.status === 429) return;
      const body = (await parseJsonSafe(resp)) ?? {};
      if (resp.ok && body.ok) {
        sessionReady = true;
        sessionError = "";
        scanUnavailable = false;
      } else if (!resp.ok) {
        if (data.turnstileSiteKey) {
          sessionError = "verification failed, try again";
        } else {
          scanUnavailable = true;
        }
      }
    } catch {
      // fire-and-forget: a network hiccup just leaves scanning gated
    }
  }

  const TURNSTILE_SCRIPT_SRC =
    "https://challenges.cloudflare.com/turnstile/v0/api.js";
  let widgetEl;
  let widgetId = null;

  function renderTurnstileWidget() {
    if (
      !window.turnstile ||
      !widgetEl ||
      !data.turnstileSiteKey ||
      sessionReady
    )
      return;
    if (widgetId !== null) return;
    widgetId = window.turnstile.render(widgetEl, {
      sitekey: data.turnstileSiteKey,
      callback: (token) => mintSession(token),
      "error-callback": () => {
        sessionError = "verification failed, try again";
      },
    });
  }

  function removeTurnstileWidget() {
    if (widgetId !== null && window.turnstile) {
      try {
        window.turnstile.remove(widgetId);
      } catch {
        // widget already gone; nothing to clean up
      }
    }
    widgetId = null;
  }

  // ---------------------------------------------------------------------
  // Scan
  // ---------------------------------------------------------------------
  let scanning = $state(false);
  let queuedNarration = $state(false);
  let scanError = $state("");
  let busyWaiting = $state(null);
  let findings = $state([]);
  let scanErrors = $state([]);
  let lastResult = $state(null);
  let highlightedLine = $state(null);

  let queuedTimer = null;

  function selectFinding(f) {
    highlightedLine = f.line;
  }

  async function runScan() {
    if (scanning || overCap || !sessionReady || scanUnavailable) return;
    scanning = true;
    scanError = "";
    busyWaiting = null;
    queuedNarration = false;
    highlightedLine = null;

    queuedTimer = setTimeout(() => {
      queuedNarration = true;
    }, 1_500);

    try {
      const resp = await fetch(`${API}/scan`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ language, content: code }),
      });

      if (resp.status === 429) {
        const body = (await parseJsonSafe(resp)) ?? {};
        scanError = `one scan per few seconds, try again in ${body.retry_after_s ?? "a few"}s`;
        return;
      }
      if (resp.status === 503) {
        const body = (await parseJsonSafe(resp)) ?? {};
        busyWaiting = body.waiting ?? 0;
        return;
      }
      const body = await parseJsonSafe(resp);
      if (body == null) {
        scanError = `unexpected response from the demo (${resp.status})`;
        return;
      }
      if (!resp.ok) {
        scanError = body.detail || body.error || `scan failed (${resp.status})`;
        return;
      }

      findings = body.findings ?? [];
      scanErrors = body.errors ?? [];
      lastResult = body;
      savings = {
        ...savings,
        scans: (savings?.scans ?? 0) + 1,
        saved_ms: (savings?.saved_ms ?? 0) + (body.saved_ms ?? 0),
      };
    } catch (err) {
      scanError = String(err);
    } finally {
      scanning = false;
      queuedNarration = false;
      if (queuedTimer) clearTimeout(queuedTimer);
      queuedTimer = null;
    }
  }

  // ---------------------------------------------------------------------
  // Savings footer
  // ---------------------------------------------------------------------
  let savings = $state(data.initialSavings);

  async function refreshSavings() {
    try {
      const resp = await fetch(`${API}/savings`);
      if (resp.ok) savings = await resp.json();
    } catch {
      // keep the last known value
    }
  }

  function savedMinutes(savedMs) {
    if (typeof savedMs !== "number") return null;
    return Math.round(savedMs / 60_000);
  }

  const SEVERITY_ORDER = { ERROR: 0, WARNING: 1, INFO: 2 };

  let sortedFindings = $derived(
    [...findings].sort(
      (a, b) =>
        (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3),
    ),
  );

  onMount(() => {
    if (!data.turnstileSiteKey) {
      mintSession("");
    } else if (window.turnstile) {
      renderTurnstileWidget();
    } else {
      let script = document.querySelector(
        `script[src="${TURNSTILE_SCRIPT_SRC}"]`,
      );
      if (!script) {
        script = document.createElement("script");
        script.src = TURNSTILE_SCRIPT_SRC;
        script.async = true;
        script.defer = true;
        document.head.appendChild(script);
      }
      script.addEventListener("load", renderTurnstileWidget);
    }
    refreshSavings();
    return () => {
      removeTurnstileWidget();
      if (queuedTimer) clearTimeout(queuedTimer);
    };
  });
</script>

<svelte:head>
  <title>Ember Semgrep</title>
  <meta
    name="description"
    content="The production security scanner behind this cluster's CI, pointed at your own snippet: Semgrep Pro, warm in a Firecracker microVM, scanning in about a second."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> /
      <a class="brand" href="/ember">ember</a> / semgrep</span
    >
    <a class="topbar-cross" href="/ember/firecracker"
      >how does firecracker work?</a
    >
  </header>

  <main class="sg-page">
    <header class="masthead">
      <h1><span class="ember-word">Ember</span> Semgrep</h1>
      <p class="subtitle">
        The production security scanner behind this cluster's CI, pointed at
        your snippet. Semgrep Pro, ~1,600 rules, cross-function taint within
        the file.
      </p>
    </header>

    <section class="sg-panel">
      <div class="left-col">
        <div class="editor-card">
          <div class="editor-toolbar">
            <div class="lang-toggle" role="group" aria-label="language">
              <button
                type="button"
                class="lang-btn"
                class:active={language === "python"}
                onclick={() => setLanguage("python")}>python</button
              >
              <button
                type="button"
                class="lang-btn"
                class:active={language === "javascript"}
                onclick={() => setLanguage("javascript")}
                >javascript</button
              >
            </div>
            <div class="example-picker">
              {#each examplesForLanguage as ex (ex.i)}
                <button
                  type="button"
                  class="example-btn"
                  class:active={activeExampleIndex === ex.i}
                  onclick={() => pickExample(ex.i)}>{ex.label}</button
                >
              {/each}
            </div>
          </div>

          <div class="editor-body">
            <pre
              class="gutter"
              bind:this={gutterEl}
              aria-hidden="true">{#each lineNumbers as n (n)}<span
                  class="gutter-line"
                  class:gutter-line-active={n === highlightedLine}
                  >{n}</span
                >
{/each}</pre>
            <textarea
              class="code-input"
              class:code-input-over={overCap}
              bind:this={textareaEl}
              bind:value={code}
              onscroll={syncGutterScroll}
              spellcheck="false"
              autocapitalize="off"
              autocorrect="off"
              aria-label="code snippet to scan"
            ></textarea>
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

        {#if data.turnstileSiteKey && !sessionReady}
          <div class="turnstile-slot">
            <p class="turnstile-hint">solve the check to scan</p>
            <div bind:this={widgetEl} class="turnstile-widget"></div>
            {#if sessionError}
              <p class="soft-error">{sessionError}</p>
            {/if}
          </div>
        {/if}

        <button
          type="button"
          class="scan-btn"
          disabled={!sessionReady ||
            scanUnavailable ||
            scanning ||
            overCap}
          onclick={runScan}
        >
          {#if scanning}
            {queuedNarration ? "queued…" : "scanning…"}
          {:else if scanUnavailable}
            scan unavailable
          {:else if overCap}
            snippet too large
          {:else}
            scan snippet
          {/if}
        </button>

        {#if scanError}
          <p class="run-error">{scanError}</p>
        {/if}
        {#if busyWaiting !== null}
          <p class="busy-notice">
            demo is busy, {busyWaiting} waiting, try again in a moment
          </p>
        {/if}
      </div>

      <div class="right-col">
        {#if lastResult}
          <div class="scan-stat">
            <div class="scan-stat-row">
              <span class="scan-stat-label">this scan</span>
              <span class="scan-stat-value"
                >{(lastResult.scan_ms / 1000).toFixed(2)}s</span
              >
            </div>
            <div class="scan-bar-track">
              <div
                class="scan-bar-fill"
                style:width="{Math.min(
                  100,
                  (lastResult.scan_ms / lastResult.baseline_ms) * 100,
                )}%"
              ></div>
            </div>
            <p class="scan-bar-caption">
              hosted single-file scan services median ~11s
            </p>
          </div>
        {/if}

        <div class="results-card">
          {#if sortedFindings.length === 0 && scanErrors.length === 0}
            <p class="empty-state">
              {lastResult
                ? "no findings, edit the snippet or load an example"
                : "run a scan to see findings here"}
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
                {scanErrors.length} rule error{scanErrors.length === 1
                  ? ""
                  : "s"} during this scan
              </p>
            {/if}
          {/if}
        </div>
      </div>
    </section>

    {#if savings?.scans}
      <p class="savings-footer">
        {savings.scans} snippet{savings.scans === 1 ? "" : "s"} scanned
        {#if savedMinutes(savings.saved_ms) !== null}
          , {savedMinutes(savings.saved_ms)} min of scan time saved
        {/if}
      </p>
    {/if}
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
  }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 14px 28px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .topbar strong {
    color: var(--em-ink);
    font-weight: 600;
  }

  .topbar .brand {
    color: inherit;
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar .brand:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .topbar .brand:focus-visible,
  .topbar-cross:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .topbar-cross {
    color: var(--em-muted);
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar-cross:hover {
    color: var(--em-ember-deep);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .sg-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 4px 24px 48px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .masthead {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-bottom: 4px;
  }

  .masthead h1 {
    margin: 0;
    font-size: clamp(24px, 2.4vw, 30px);
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.1;
    color: var(--em-ink);
  }

  .ember-word {
    color: var(--em-ember);
  }

  .subtitle {
    margin: 0;
    font-size: 14.5px;
    line-height: 1.5;
    color: var(--em-muted);
    max-width: 720px;
  }

  /* ---------- panel layout ---------- */
  .sg-panel {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    align-items: start;
    gap: 16px;
  }

  .left-col,
  .right-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }

  /* ---------- editor ---------- */
  .editor-card {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .editor-toolbar {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--em-line);
  }

  .lang-toggle,
  .example-picker {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .lang-btn,
  .example-btn {
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid var(--em-line);
    background: var(--em-ground);
    color: var(--em-muted);
    font-family: var(--em-mono);
    font-size: 11.5px;
    cursor: pointer;
    transition:
      background-color 0.15s ease,
      border-color 0.15s ease,
      color 0.15s ease;
  }

  .lang-btn:hover,
  .example-btn:hover {
    border-color: var(--em-faint);
  }

  .lang-btn.active,
  .example-btn.active {
    background: var(--em-ember);
    border-color: var(--em-ember-deep);
    color: var(--em-on-color);
  }

  .editor-body {
    display: flex;
    height: 320px;
  }

  .gutter {
    margin: 0;
    padding: 12px 8px;
    background: var(--em-ground);
    border-right: 1px solid var(--em-line);
    font-family: var(--em-mono);
    font-size: 13px;
    line-height: 1.5;
    color: var(--em-faint);
    text-align: right;
    overflow: hidden;
    user-select: none;
    flex: none;
    min-width: 3.5ch;
  }

  .gutter-line {
    display: block;
  }

  .gutter-line-active {
    color: var(--em-ember-deep);
    font-weight: 700;
  }

  .code-input {
    flex: 1;
    min-width: 0;
    padding: 12px;
    border: none;
    resize: none;
    background: var(--em-panel);
    color: var(--em-ink);
    font-family: var(--em-mono);
    font-size: 13px;
    line-height: 1.5;
    outline: none;
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

  /* ---------- turnstile + scan button ---------- */
  .turnstile-slot {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .turnstile-hint {
    margin: 0;
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .soft-error {
    margin: 0;
    color: var(--em-ember-deep);
    font-size: 12px;
  }

  .scan-btn {
    padding: 11px 18px;
    border-radius: 10px;
    font-family: inherit;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    background: var(--em-ember);
    border: 1px solid var(--em-ember-deep);
    color: var(--em-on-color);
    box-shadow: var(--em-shadow-soft);
    transition:
      background-color 0.15s ease,
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .scan-btn:hover:not(:disabled) {
    background: var(--em-ember-deep);
  }

  .scan-btn:disabled {
    opacity: 0.55;
    cursor: default;
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

  /* ---------- scan-time stat ---------- */
  .scan-stat {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .scan-stat-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }

  .scan-stat-label {
    font-size: 13px;
    color: var(--em-muted);
  }

  .scan-stat-value {
    font-family: var(--em-mono);
    font-size: 20px;
    font-weight: 700;
    color: var(--em-good-deep);
    font-variant-numeric: tabular-nums;
  }

  .scan-bar-track {
    height: 8px;
    border-radius: 999px;
    background: var(--em-track);
    overflow: hidden;
  }

  .scan-bar-fill {
    height: 100%;
    border-radius: 999px;
    background: var(--em-good);
    transition: width 0.3s ease;
  }

  .scan-bar-caption {
    margin: 0;
    font-size: 11.5px;
    color: var(--em-faint);
  }

  /* ---------- results ---------- */
  .results-card {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
    padding: 8px;
    min-height: 260px;
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

  /* ---------- savings footer ---------- */
  .savings-footer {
    margin: 8px 0 0;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
  }

  @media (max-width: 900px) {
    .topbar {
      padding: 12px 16px;
    }

    .sg-page {
      padding: 4px 16px 48px;
      gap: 14px;
    }

    .sg-panel {
      grid-template-columns: 1fr;
    }
  }
</style>
