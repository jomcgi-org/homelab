<script>
  // K8s terminal: k9s straight into scratch-k8s, a scale-to-zero 3-node k3s
  // cluster in Firecracker microVMs (see projects/monolith/demos/k8s_terminal_api.py
  // for the wire contract this mirrors).
  //
  // Two moving parts, both backend-proxied:
  //   status poll -> GET /api/demos/k8s/status. Composite-group introspection:
  //                  lifecycle state, whether a banked snapshot exists (warm),
  //                  the 3 member VMs, and recent wake history (for the up/down
  //                  band). Read-only, never opens a session, so polling alone
  //                  cannot keep the cluster awake.
  //   terminal    -> WS /api/demos/k8s/terminal?cols=N&rows=N. Connecting IS
  //                  the wake: the server streams JSON "phase" control frames
  //                  while the group boots or relights, then a "ready" frame
  //                  once k9s is live in a PTY, after which frames are raw
  //                  bytes (binary, both directions). A "resize" TEXT frame
  //                  keeps the PTY's winsize in sync with the terminal.
  //
  // xterm.js is loaded lazily (dynamic import inside connect(), never at
  // module scope): this route tree can be SSR'd, and importing a
  // browser-only PTY renderer at module scope would break that.
  import "@xterm/xterm/css/xterm.css";
  import { onMount, onDestroy } from "svelte";

  const STATUS_URL = "/api/demos/k8s/status";
  const STATUS_POLL_MS = 4000;

  // Fallback terminal geometry, used to pick the initial WS ?cols=&rows=
  // before xterm's FitAddon can measure the real card. Roughly a monospace
  // cell at 15px line-height inside the card's typical rendered size; the
  // FitAddon correction on ready makes the exact numbers here unimportant.
  const FALLBACK_COLS = 100;
  const FALLBACK_ROWS = 28;
  const CHAR_W_PX = 9;
  const CHAR_H_PX = 17;

  const STATE_TONE = {
    banked: "asleep",
    creating: "waking",
    running: "live",
    relighting: "waking",
    fresh_booting: "waking",
    banking: "waking",
    destroyed: "dead",
    failed: "dead",
  };

  const STATE_LABEL = {
    banked: "Asleep",
    creating: "Creating",
    running: "Live",
    relighting: "Relighting",
    fresh_booting: "Cold booting",
    banking: "Banking",
    destroyed: "Destroyed",
    failed: "Failed",
  };

  let status = $state(null);
  let statusError = $state("");

  let connecting = $state(false);
  let connected = $state(false);
  let bootLines = $state([]);
  let sessionError = $state("");
  let exitCode = $state(null);

  let ws = null;
  let term = null;
  let fitAddon = null;
  let resizeObserver = null;
  let statusPollTimer = null;

  let cardEl;

  function stateChip(state) {
    return {
      tone: STATE_TONE[state] ?? "dead",
      label: STATE_LABEL[state] ?? state ?? "Unknown",
    };
  }

  let chip = $derived(stateChip(status?.state));

  let lastEvent = $derived(status?.events?.[0] ?? null);

  function ms(v) {
    if (v == null) return null;
    return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
  }

  function eventTitle(ev) {
    const dur = ms(ev.duration_ms);
    return dur ? `${ev.classification} in ${dur}` : ev.classification;
  }

  function clock(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleTimeString();
  }

  async function pollStatus() {
    try {
      const resp = await fetch(STATUS_URL);
      if (!resp.ok) {
        statusError = `status ${resp.status}`;
        return;
      }
      status = await resp.json();
      statusError = "";
    } catch (err) {
      statusError = String(err);
    }
  }

  function pushBootLine(text) {
    bootLines = [...bootLines, text];
  }

  function phaseLine(msg) {
    const parts = [`[${msg.state}]`];
    if (msg.note) parts.push(msg.note);
    return parts.join(" ");
  }

  function estimateGeometry() {
    if (!cardEl) return { cols: FALLBACK_COLS, rows: FALLBACK_ROWS };
    const rect = cardEl.getBoundingClientRect();
    const cols = Math.max(
      20,
      Math.floor(rect.width / CHAR_W_PX) || FALLBACK_COLS,
    );
    const rows = Math.max(
      8,
      Math.floor(rect.height / CHAR_H_PX) || FALLBACK_ROWS,
    );
    return { cols, rows };
  }

  async function connect() {
    if (connecting || connected) return;
    connecting = true;
    sessionError = "";
    exitCode = null;
    bootLines = [];

    const { cols, rows } = estimateGeometry();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/demos/k8s/terminal?cols=${cols}&rows=${rows}`;

    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      pushBootLine("[connecting] socket open, waking cluster if needed…");
    };

    ws.onmessage = async (event) => {
      if (typeof event.data === "string") {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }
        if (msg.type === "phase") {
          pushBootLine(phaseLine(msg));
        } else if (msg.type === "ready") {
          await onReady();
        } else if (msg.type === "error") {
          sessionError = msg.error || "session error";
          teardownSession();
        } else if (msg.type === "exit") {
          exitCode = msg.code;
          teardownSession();
        }
        return;
      }
      // Binary frame: raw PTY bytes.
      if (term) {
        term.write(new Uint8Array(event.data));
      }
    };

    ws.onerror = () => {
      if (!connected) {
        sessionError = sessionError || "connection failed";
      }
    };

    ws.onclose = () => {
      if (connected || connecting) {
        teardownSession();
      }
    };
  }

  async function onReady() {
    connecting = false;
    connected = true;

    const [{ Terminal }, { FitAddon }] = await Promise.all([
      import("@xterm/xterm"),
      import("@xterm/addon-fit"),
    ]);

    term = new Terminal({
      convertEol: true,
      fontFamily: "var(--font-mono, monospace)",
      fontSize: 13,
      theme: {
        // Maximum-contrast light theme: pure white ground, near-black ink,
        // deep saturated ANSI so every color stays legible on white.
        background: "#ffffff",
        foreground: "#0a0a0a",
        cursor: "#1746a2",
        cursorAccent: "#ffffff",
        selectionBackground: "#b9d3f5",
        selectionForeground: "#0a0a0a",
        black: "#000000",
        red: "#b3261e",
        green: "#0f6d33",
        yellow: "#7a5200",
        blue: "#1746a2",
        magenta: "#7a2f8a",
        cyan: "#0d6e78",
        white: "#3a3a3a",
        brightBlack: "#4a4a4a",
        brightRed: "#8f1c16",
        brightGreen: "#0a5528",
        brightYellow: "#5f4000",
        brightBlue: "#123a86",
        brightMagenta: "#5f2470",
        brightCyan: "#0a5860",
        brightWhite: "#000000",
      },
      fontWeight: 500,
      fontWeightBold: 700,
    });
    fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(cardEl);
    fitAddon.fit();
    sendResize();

    term.onData((data) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    resizeObserver = new ResizeObserver(() => {
      if (!fitAddon) return;
      fitAddon.fit();
      sendResize();
    });
    resizeObserver.observe(cardEl);
  }

  function sendResize() {
    if (!term || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(
      JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
    );
  }

  function teardownSession() {
    connecting = false;
    connected = false;
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
    if (term) {
      term.dispose();
      term = null;
    }
    fitAddon = null;
    if (ws) {
      const socket = ws;
      ws = null;
      if (
        socket.readyState === WebSocket.OPEN ||
        socket.readyState === WebSocket.CONNECTING
      ) {
        socket.close();
      }
    }
    pollStatus();
  }

  function disconnect() {
    if (ws) {
      ws.close();
    } else {
      teardownSession();
    }
  }

  onMount(() => {
    pollStatus();
    statusPollTimer = setInterval(pollStatus, STATUS_POLL_MS);
    return () => {
      clearInterval(statusPollTimer);
    };
  });

  onDestroy(() => {
    teardownSession();
  });
</script>

<section class="k8s-panel">
  <div class="status-strip">
    <div class="chip tone-{chip.tone}">
      <span class="chip-dot" aria-hidden="true"></span>
      <span class="chip-label">{chip.label}</span>
    </div>
    {#if status?.warm}
      <span
        class="warm-badge"
        title="A banked snapshot exists; the next wake attempts a relight"
      >
        warm snapshot ready
      </span>
    {/if}
    <div class="members" aria-hidden="false">
      {#each status?.members ?? [] as member (member.name)}
        <span
          class="member-dot"
          class:member-healthy={member.healthy}
          class:member-unhealthy={member.healthy === false}
          title={`${member.name}: ${member.state}${member.healthy == null ? "" : member.healthy ? " (healthy)" : " (unhealthy)"}`}
        ></span>
      {/each}
    </div>
    {#if statusError}
      <span class="status-error">status: {statusError}</span>
    {/if}
  </div>

  {#if status?.events?.length}
    <div class="wake-history">
      <div class="wake-band" role="list" aria-label="Wake history">
        {#each [...status.events].reverse() as ev (ev.at)}
          <span
            class="wake-mark"
            class:mark-relit={ev.classification === "relit"}
            class:mark-cold={ev.classification?.startsWith("cold")}
            title={`${clock(ev.at)}: ${eventTitle(ev)}`}
            role="listitem"
          ></span>
        {/each}
      </div>
      {#if lastEvent}
        <p class="last-wake">last wake: {eventTitle(lastEvent)}</p>
      {/if}
    </div>
  {/if}

  <div class="terminal-card" bind:this={cardEl}>
    {#if !connected && !connecting}
      <div class="idle-overlay">
        <p class="idle-copy">
          Connect to open a k9s session against the live cluster. If it is
          asleep, connecting wakes it first.
        </p>
        <button type="button" class="connect-btn" onclick={connect}>
          Connect
        </button>
        {#if sessionError || exitCode != null}
          <p class="session-error">
            {sessionError || "session ended"}
            {#if exitCode != null}(exit code {exitCode}){/if}
          </p>
          <button type="button" class="reconnect-btn" onclick={connect}>
            Reconnect
          </button>
        {/if}
      </div>
    {:else if connecting}
      <div class="boot-log" role="log" aria-live="polite">
        {#each bootLines as line, i (i)}
          <p class="boot-line">{line}</p>
        {/each}
      </div>
    {/if}
  </div>

  {#if connected}
    <div class="session-bar">
      <button type="button" class="disconnect-btn" onclick={disconnect}>
        Disconnect
      </button>
    </div>
  {/if}

  <p class="footnote">
    This is a real 3-node k3s cluster in Firecracker microVMs. It wakes when you
    connect and banks to memory snapshots about 10 minutes after you disconnect.
  </p>
</section>

<style>
  .k8s-panel {
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 1100px;
  }

  .status-strip {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 18px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid var(--line);
    font-weight: 700;
    font-size: 13px;
  }

  .chip-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--text-faint);
  }

  .tone-live .chip-dot {
    background: var(--svc-fc);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--svc-fc) 20%, transparent);
  }

  .tone-waking .chip-dot {
    background: var(--loadtest-highlight);
    animation: pulse 0.9s ease-in-out infinite;
  }

  .tone-asleep .chip-dot {
    background: var(--accent);
    opacity: 0.45;
  }

  .tone-asleep .chip-label {
    color: var(--text-dim);
  }

  .tone-dead .chip-dot {
    background: var(--danger);
  }

  @keyframes pulse {
    0%,
    100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.35);
      opacity: 0.55;
    }
  }

  .warm-badge {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 4px 10px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--svc-fc) 16%, transparent);
    color: var(--svc-fc);
  }

  .members {
    display: flex;
    gap: 6px;
    margin-left: auto;
  }

  .member-dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text-faint);
    border: 1px solid var(--line);
  }

  .member-healthy {
    background: var(--svc-fc);
  }

  .member-unhealthy {
    background: var(--danger);
  }

  .status-error {
    color: var(--danger);
    font-size: 12px;
  }

  .wake-history {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .wake-band {
    display: flex;
    gap: 3px;
    align-items: flex-end;
    height: 18px;
  }

  .wake-mark {
    width: 8px;
    height: 12px;
    border-radius: 2px;
    background: var(--text-faint);
    opacity: 0.5;
  }

  .wake-mark.mark-relit {
    background: var(--svc-fc);
    opacity: 1;
  }

  .wake-mark.mark-cold {
    background: var(--loadtest-highlight);
    opacity: 1;
  }

  .last-wake {
    margin: 0;
    font-size: 12px;
    color: var(--text-dim);
  }

  .terminal-card {
    position: relative;
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: 10px;
    min-height: 420px;
    overflow: hidden;
  }

  .idle-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 14px;
    padding: 24px;
    text-align: center;
  }

  .idle-copy {
    margin: 0;
    max-width: 32em;
    font-size: 14px;
    line-height: 1.5;
    color: var(--text-faint, #656e7c);
  }

  .connect-btn,
  .reconnect-btn,
  .disconnect-btn {
    padding: 10px 22px;
    border-radius: 8px;
    border: 1px solid var(--accent);
    background: var(--accent);
    color: var(--surface);
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
  }

  .reconnect-btn {
    background: transparent;
    color: var(--accent);
  }

  .session-error {
    margin: 0;
    color: var(--danger, #a4372e);
    font-size: 13px;
    max-width: 32em;
  }

  .boot-log {
    padding: 16px 20px;
    font-family: var(--font-mono, monospace);
    font-size: 13px;
    color: #0a0a0a;
    font-weight: 500;
    overflow-y: auto;
    max-height: 420px;
  }

  .boot-line {
    margin: 0 0 4px;
    white-space: pre-wrap;
  }

  .session-bar {
    display: flex;
    justify-content: flex-end;
  }

  .disconnect-btn {
    background: var(--surface);
    color: var(--danger);
    border-color: var(--line);
  }

  .footnote {
    margin: 0;
    font-size: 12px;
    color: var(--text-faint);
  }
</style>
