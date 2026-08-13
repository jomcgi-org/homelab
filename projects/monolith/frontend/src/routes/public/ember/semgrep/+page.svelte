<script>
  // /ember/semgrep: the production security scanner (Semgrep Pro, ~1,600
  // rules, cross-function taint within the file), pointed at a visitor's own
  // snippet through the same warm fc-invoke path this cluster's own CI scans
  // through. Turnstile-gated, session-cookie-gated, rate-bucketed, and
  // queue-bounded on the backend (ember_public/semgrep_router.py); this page
  // only talks to same-origin proxies under /ember/semgrep/api (see the
  // +server.js routes beside this page), never /api directly: the public
  // tier's rule 2 (public-tier-checklist.md).
  //
  // Visual language: the /ember/* pages are their own small site in the
  // fcstory palette (lib/public/ember/ember.css), not the neobrutalist
  // jomcgi.dev baseline. The topbar wordmark is the only nav: it links home.
  //
  // Composition: this page owns the session/Turnstile shell, the scan
  // request/rate/queue/error handling, examples data, and the topbar
  // counters; ScanView.svelte owns everything inside the white panel
  // (editor, gutter/ignition, sweep, receipt, race). The whole visual
  // journey plays only after the scan response arrives (see ScanView's
  // runJourney()), driven from here once the fetch settles.
  import { onMount } from "svelte";
  import "$lib/public/ember/ember.css";
  import ScanView from "$lib/public/ember/ScanView.svelte";

  let { data } = $props();

  const API = "/ember/semgrep/api";

  // ---------------------------------------------------------------------
  // Canned examples: shipped verbatim from the design plan, verified to
  // fire Pro taint rules on the warm path. js pair first, then python pair
  // (mock order); default buffer is the first javascript example.
  // ---------------------------------------------------------------------
  const EXAMPLES = [
    {
      language: "javascript",
      label: "command injection",
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
      label: "eval",
      code: `const express = require("express");

const app = express();

app.get("/calc", (req, res) => {
  const result = eval(req.query.expr);
  res.send(String(result));
});
`,
    },
    {
      language: "python",
      label: "command injection",
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
      label: "SSRF",
      code: `import urllib.request

from flask import Flask, request

app = Flask(__name__)


def build_url():
    host = request.args.get("host")
    return f"http://{host}/status"


@app.route("/probe")
def probe():
    return urllib.request.urlopen(build_url()).read()
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
  let overCap = $derived(lineCount > MAX_LINES || charCount > MAX_CHARS);

  let scanViewEl;

  function pickExample(index) {
    activeExampleIndex = index;
    const ex = EXAMPLES[index];
    language = ex.language;
    code = ex.code;
    lastResult = null;
    scanViewEl?.resetView();
  }

  // ---------------------------------------------------------------------
  // Turnstile + session. Mirrors EmberConsole's wiring: a widget renders
  // above the scan button on first load when a site key is configured, and
  // the scan button stays disabled until the solved token mints a session.
  // When no site key is configured (dev), mint sessionlessly on mount,
  // matching the backend's private-tier allowance.
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
  let lastResult = $state(null);

  let queuedTimer = null;

  async function runScan() {
    if (scanning || overCap || !sessionReady || scanUnavailable) return;
    scanning = true;
    scanError = "";
    busyWaiting = null;
    queuedNarration = false;

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

      lastResult = body;
      savings = {
        ...savings,
        scans: (savings?.scans ?? 0) + 1,
        saved_ms: (savings?.saved_ms ?? 0) + (body.saved_ms ?? 0),
      };
      scanViewEl?.runJourney(body);
      refreshSavings();
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
  // Topbar counters
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
  <main class="sg-page">
    <div class="mock-topbar">
      <span
        ><a class="brand" href="/">jomcgi.dev</a> /
        <a class="brand" href="/ember">ember</a> / semgrep</span
      >
      {#if savings?.scans}
        <span class="counters"
          >{savings.scans} snippet{savings.scans === 1 ? "" : "s"} scanned{#if savedMinutes(savings.saved_ms) !== null}
            · {savedMinutes(savings.saved_ms)} min of cold starts skipped{/if}</span
        >
      {/if}
    </div>

    <h1><span class="ember-word">Ember</span> Semgrep</h1>
    <p class="lede">
      Point the production security scanner behind this cluster's CI at your own
      snippet. A <b>Semgrep Pro</b> engine loaded its 1,600 rules once, then was
      frozen as a microVM snapshot. Every scan thaws its own fresh copy in
      <b>21 ms</b>.
    </p>

    <ScanView
      bind:this={scanViewEl}
      examples={EXAMPLES}
      bind:language
      bind:code
      bind:activeExampleIndex
      {scanning}
      {queuedNarration}
      {sessionReady}
      {scanUnavailable}
      {overCap}
      {scanError}
      {busyWaiting}
      result={lastResult}
      onscan={runScan}
      onpickexample={pickExample}
    />

    {#if data.turnstileSiteKey && !sessionReady}
      <div class="turnstile-slot">
        <p class="turnstile-hint">solve the check to scan</p>
        <div bind:this={widgetEl} class="turnstile-widget"></div>
        {#if sessionError}
          <p class="soft-error">{sessionError}</p>
        {/if}
      </div>
    {/if}

    <div class="mock-footer">
      <span
        >each scan runs in its own microVM, destroyed after the response ·
        engine: <a href="https://semgrep.dev">Semgrep Pro</a> · snapshots:
        <a href="/ember/firecracker">Firecracker</a></span
      >
    </div>
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
  }

  .sg-page {
    max-width: 880px;
    margin: 0 auto;
    padding: 20px 24px 60px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .mock-topbar {
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 8px;
    border-bottom: 1px solid var(--em-line);
    padding-bottom: 10px;
  }

  .mock-topbar .brand {
    color: inherit;
    text-decoration: none;
  }

  .mock-topbar .brand:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .mock-topbar .brand:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .counters {
    font-variant-numeric: tabular-nums;
  }

  h1 {
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

  .lede {
    margin: 0;
    color: var(--em-muted);
    max-width: 60ch;
    font-size: 15.5px;
    line-height: 1.5;
  }

  .lede b {
    color: var(--em-ink);
    font-weight: 600;
  }

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

  .mock-footer {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    border-top: 1px solid var(--em-line);
    padding-top: 12px;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
    flex-wrap: wrap;
  }

  .mock-footer a {
    color: var(--em-muted);
    text-decoration: none;
    border-bottom: 1px solid var(--em-line);
  }

  @media (max-width: 640px) {
    .sg-page {
      padding: 16px 16px 48px;
    }
  }
</style>
