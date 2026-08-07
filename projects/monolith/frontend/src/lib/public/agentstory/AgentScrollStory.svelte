<script>
  import { onMount } from "svelte";
  import "../ember/ember.css";
  import "./agentstory.css";
  import {
    CALLS,
    CHAT_REVEAL,
    PHASES,
    clamp,
    easeInOut,
    sub,
  } from "./timeline.js";

  let scrollerEl,
    heroEl,
    topbarEl,
    brandEl,
    crumbEl,
    chatViewEl,
    chatItemsEl,
    wireEl,
    wireTextEl,
    chipEl,
    chipWordEl;
  let paths = {},
    groups = {},
    ram = [],
    chatItems = [];
  let span = 1,
    chatViewH = 0;
  const DIM = 0.55;
  const GHOST = 0.28;

  function setPath(name, progress, alpha = 1) {
    const path = paths[name];
    if (!path) return;
    const q = clamp(progress, 0, 1);
    path.el.style.strokeDashoffset = path.len * (1 - q);
    path.el.style.opacity = q > 0.01 ? alpha : 0;
    // Marker-end is detached until the stroke reaches its endpoint, so the arrowhead never leads the drawn line.
    path.el.style.markerEnd = q > 0.96 ? "" : "none";
    path.label.style.opacity = q > 0.96 ? alpha : 0;
  }

  function setPill(text, mode) {
    const rect = groups.pillRect;
    const label = groups.pillText;
    label.textContent = text;
    const width = Math.max(56, text.length * 6.4 + 18);
    rect.setAttribute("width", width);
    rect.setAttribute("x", 628 - width);
    label.setAttribute("x", 628 - width / 2);
    rect.style.fill =
      mode === "run"
        ? "var(--em-ember)"
        : mode === "asleep"
          ? "var(--em-frost)"
          : "var(--ag-idle)";
    label.style.fill =
      mode === "off" ? "var(--em-muted)" : "var(--em-on-color)";
  }

  function frame(t) {
    const topOpacity = 1 - sub(t, 0.02, 0.055);
    topbarEl.style.opacity = topOpacity;
    brandEl.style.pointerEvents = topOpacity > 0.5 ? "auto" : "none";
    crumbEl.style.pointerEvents = topOpacity > 0.5 ? "auto" : "none";
    const heroOpacity = 1 - sub(t, ...PHASES.heroOut);
    heroEl.style.opacity = heroOpacity;
    heroEl.style.transform = `translateY(${-(1 - heroOpacity) * 8}vh)`;
    heroEl.style.pointerEvents = heroOpacity > 0.5 ? "auto" : "none";
    const w = sub(t, ...PHASES.wake),
      h = sub(t, ...PHASES.hydrate),
      c = sub(t, ...PHASES.creds);
    const pk = sub(t, ...PHASES.park),
      r = sub(t, ...PHASES.resume);
    const beat =
      t < PHASES.wake[0]
        ? "pre"
        : t < PHASES.hydrate[0]
          ? "wake"
          : t < PHASES.creds[0]
            ? "hydrate"
            : t < PHASES.park[0]
              ? "creds"
              : t < PHASES.resume[0]
                ? "park"
                : "resume";
    const light = (name, value) => {
      if (groups[name]) groups[name].style.opacity = value;
    };
    light("brick", beat === "pre" ? DIM : 1);
    light("cp", beat === "wake" ? 1 : DIM);
    light("noded", beat === "wake" ? 1 : DIM);
    light("sidecar", beat === "hydrate" || beat === "creds" ? 1 : DIM);
    light("github", beat === "hydrate" ? 1 : DIM);
    light("api", beat === "creds" ? 1 : DIM);
    light(
      "scratch",
      beat === "wake" || beat === "park" || (beat === "resume" && r < 0.4)
        ? 1
        : DIM,
    );
    light("chipBase", beat === "wake" ? 1 : 0.75);
    light(
      "chipWs",
      beat === "hydrate" || beat === "park" || (beat === "resume" && r < 0.4)
        ? 1
        : 0.75,
    );
    light("s3", beat === "resume" ? 1 : DIM);
    light("s3ws", beat === "resume" ? 1 : 0.75);
    light(
      "brick3",
      beat === "resume"
        ? Math.max(GHOST, easeInOut(sub(r, 0.42, 0.62)))
        : GHOST,
    );
    light(
      "vm",
      beat === "pre"
        ? DIM
        : (beat === "park" && pk > 0.45) || beat === "resume"
          ? 0.45
          : 1,
    );
    setPath("grpc", beat === "wake" ? sub(w, 0.02, 0.22) : 0);
    setPath("load", beat === "wake" ? sub(w, 0.2, 0.48) : 0);
    setPath("patch", beat === "wake" ? sub(w, 0.45, 0.7) : 0);
    setPath(
      "vsock",
      beat === "hydrate" ? sub(h, 0.05, 0.25) : beat === "creds" ? 1 : 0,
    );
    setPath("git", beat === "hydrate" ? sub(h, 0.3, 0.6) : 0);
    setPath("egress", beat === "creds" ? sub(c, 0.15, 0.45) : 0);
    setPath("bank", beat === "resume" ? sub(r, 0.04, 0.32) : 0);
    setPath("restore", beat === "resume" ? sub(r, 0.42, 0.66) : 0);
    const swapped = beat === "creds" && c > 0.55;
    groups.fakeStrike.style.opacity = swapped ? 1 : 0;
    groups.fakeText.style.opacity = swapped ? 0.55 : 1;
    groups.realRect.style.opacity = swapped ? 1 : 0;
    groups.realText.style.opacity = swapped ? 1 : 0;
    if (beat === "pre") setPill("off", "off");
    else if (beat === "wake")
      setPill(
        w < 0.45 ? "restoring" : "awake · 2.5 ms",
        w < 0.45 ? "asleep" : "run",
      );
    else if (beat === "hydrate") setPill("awake · turn running", "run");
    else if (beat === "creds") setPill("awake", "run");
    else if (beat === "park")
      setPill(
        pk > 0.45 ? "asleep · parked" : "idle 20 s…",
        pk > 0.45 ? "asleep" : "run",
      );
    else setPill("expired · 410", "off");
    const level =
      beat === "wake"
        ? easeInOut(sub(w, 0.55, 0.95))
        : beat === "hydrate" || beat === "creds"
          ? 1
          : beat === "park"
            ? 1 - easeInOut(sub(pk, 0.3, 0.65))
            : 0;
    for (const cell of ram) {
      const on = level >= cell.th;
      if (on !== cell.on) {
        cell.on = on;
        cell.el.style.fill = on ? cell.hot : "";
      }
    }
    const local =
      beat === "wake"
        ? w
        : beat === "hydrate"
          ? h
          : beat === "creds"
            ? c
            : beat === "park"
              ? pk
              : beat === "resume"
                ? r
                : 0;
    wireEl.style.opacity = sub(t, PHASES.wake[0], PHASES.wake[0] + 0.02);
    const list = CALLS[beat];
    let active = null;
    for (const call of list || []) if (local >= call.a) active = call;
    // Type within the first ~third of the call's window, then hold the
    // finished line until the next call starts. Typing across the whole
    // window meant the text completed exactly as the next call replaced
    // it, so no call was ever readable at real scroll speed.
    wireTextEl.textContent = active
      ? active.text.slice(
          0,
          Math.round(
            sub(local, active.a, active.a + (active.b - active.a) * 0.35) *
              active.text.length,
          ),
        )
      : "";
    // classList, never className: overwriting className would drop Svelte's
    // scoping hash and detach every .wt rule (fcstory convention).
    wireTextEl.classList.remove("w-ember", "w-amber", "w-good", "w-frost");
    if (active?.cls) wireTextEl.classList.add(active.cls);
    let target = 0;
    for (const item of chatItems) {
      const opacity = sub(t, item.at, item.at + CHAT_REVEAL);
      item.el.style.opacity = opacity;
      item.el.style.transform = `translateY(${(1 - opacity) * 10}px)`;
      if (opacity > 0)
        target += (item.bottom - target) * Math.min(1, opacity * 1.4);
    }
    chatItemsEl.style.transform = `translateY(${-Math.max(0, target + 16 - chatViewH)}px)`;
    const chipMode =
      beat === "pre"
        ? "off"
        : beat === "wake"
          ? w < 0.45
            ? "off"
            : "awake"
          : beat === "hydrate" || beat === "creds"
            ? "awake"
            : beat === "park"
              ? pk > 0.45
                ? "asleep"
                : "awake"
              : r < 0.62
                ? "off"
                : "awake";
    const word =
      chipMode === "awake"
        ? "vm awake"
        : chipMode === "asleep"
          ? "vm asleep"
          : "vm off";
    chipWordEl.textContent = word;
    chipEl.classList.toggle("awake", chipMode === "awake");
    chipEl.classList.toggle("asleep", chipMode === "asleep");
  }

  function measure() {
    span = Math.max(1, scrollerEl.offsetHeight - window.innerHeight);
    chatViewH = chatViewEl.clientHeight;
    for (const item of chatItems) {
      item.top = item.el.offsetTop;
      item.bottom = item.el.offsetTop + item.el.offsetHeight;
    }
  }
  let ticking = false,
    resizing = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      frame(clamp(window.scrollY / span, 0, 1));
      ticking = false;
    });
  }
  function onResize() {
    if (resizing) return;
    resizing = true;
    requestAnimationFrame(() => {
      measure();
      frame(clamp(window.scrollY / span, 0, 1));
      resizing = false;
    });
  }

  onMount(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    document.body.style.overflow = "auto";
    const root = scrollerEl.closest(".agstory");
    for (const el of root.querySelectorAll("[id^='g-']"))
      groups[el.id.slice(2)] = el;
    groups.pillRect = root.querySelector("#r-pill");
    groups.pillText = root.querySelector("#t-pill");
    groups.fakeStrike = root.querySelector("#s-fake");
    groups.fakeText = root.querySelector("#t-fake");
    groups.realRect = root.querySelector("#r-real");
    groups.realText = root.querySelector("#t-real");
    for (const el of root.querySelectorAll("path.epath")) {
      const label = root.querySelector(`#l-${el.id.slice(2)}`);
      const len = el.getTotalLength();
      el.style.strokeDasharray = `${len}`;
      paths[el.id.slice(2)] = { el, label, len };
    }
    const ramGroup = root.querySelector("#g-ram");
    for (let row = 0; row < 3; row++)
      for (let col = 0; col < 8; col++) {
        const el = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "rect",
        );
        el.setAttribute("class", "cellr");
        el.setAttribute("x", 424 + col * 26);
        el.setAttribute("y", 100 + row * 18);
        el.setAttribute("width", 22);
        el.setAttribute("height", 14);
        el.setAttribute("rx", 2);
        ramGroup.appendChild(el);
        ram.push({
          el,
          th: (col + 0.2 + Math.random() * 2.2) / 10.2,
          hot: `hsl(${16 + Math.random() * 9} ${72 + Math.random() * 12}% ${46 + Math.random() * 14}%)`,
          on: false,
        });
      }
    chatItems = [...chatItemsEl.querySelectorAll(".ci")].map((el) => ({
      el,
      at: Number(el.dataset.at),
      bottom: 0,
    }));
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize, { passive: true });
    requestAnimationFrame(() => {
      measure();
      frame(0);
    });
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
    };
  });
</script>

<div class="ember-site agstory">
  <header class="topbar" bind:this={topbarEl}>
    <span
      ><a class="brand" bind:this={brandEl} href="/"
        ><strong>jomcgi.dev</strong></a
      >
      / <a class="brand" bind:this={crumbEl} href="/ember">ember</a> / agents</span
    >
  </header>
  <div class="scroller" bind:this={scrollerEl}>
    <div class="stage">
      <div class="hero" bind:this={heroEl}>
        <div class="kicker">
          ember / agents · how this site runs its AI agents
        </div>
        <h1>One microVM per <span class="em">agent session.</span></h1>
        <p class="sub">
          Killed on idle, rebuilt from disk days later on whichever node has
          room.
        </p>
        <div class="stats">
          <span><b>2.5 ms</b> VM resume</span><span class="sep">·</span><span
            ><b>20 s</b> idle → VM destroyed</span
          ><span class="sep">·</span><span
            ><b>0</b> real credentials inside</span
          >
        </div>
        <div class="cue">scroll ▼</div>
      </div>
      <div class="stagegrid">
        <div class="chat-col">
          <div class="chat">
            <div class="chat-head">
              <span class="name">agent session</span><span
                class="vmchip"
                bind:this={chipEl}
                ><span class="d"></span><span bind:this={chipWordEl}
                  >vm off</span
                ></span
              >
            </div>
            <div class="chat-view" bind:this={chatViewEl}>
              <div class="chat-items" bind:this={chatItemsEl}>
                <div class="ci msg you" data-at="0.08">
                  did last night's chart bump deploy?
                </div>
                <div class="ci evt" data-at="0.14">
                  <b>vm awake · 2.5 ms</b>
                </div>
                <div class="ci evt" data-at="0.30">
                  clone jomcgi/homelab · <b>11.2 MiB</b>
                </div>
                <div class="ci msg bot" data-at="0.37">
                  Checking ArgoCD and the chart versions.
                </div>
                <div class="ci evt" data-at="0.50">
                  <b>creds attached outside the vm</b>
                </div>
                <div class="ci msg bot" data-at="0.57">
                  Yes. The chart moved and the rollout is healthy.
                </div>
                <div class="ci evt" data-at="0.71">
                  idle 20 s · <b>vm destroyed</b> · disk kept
                </div>
                <div class="ci evt divider frost" data-at="0.84">
                  +2 days · workspace in s3
                </div>
                <div class="ci msg you" data-at="0.87">
                  add a regression test for that fix
                </div>
                <div class="ci evt frost" data-at="0.905">
                  restored on <b>brick-3</b> · --resume
                </div>
                <div class="ci msg bot" data-at="0.935">
                  Picking up where we left off.
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="machine-col">
          <div class="wire" bind:this={wireEl} aria-hidden="true">
            <span class="wp">▸</span><span class="wt" bind:this={wireTextEl}
            ></span><span class="wc">▍</span>
          </div>
          <div class="dg-frame">
            <svg
              class="dg"
              viewBox="0 0 880 500"
              role="img"
              aria-label="Agent session architecture: the control plane places a session on a Firecracker node, the VM restores from a shared base snapshot with the session's workspace volume patched in, all egress including the git clone leaves through a credential-holding sidecar outside the VM, and parked or expired workspaces move between node scratch and SeaweedFS S3."
              ><defs
                >{#each ["ember", "amber", "frost", "good"] as color}<marker
                    id={`m-${color}`}
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="4"
                    orient="auto"
                    ><path
                      class="mk"
                      style={`stroke: var(--em-${color})`}
                      d="M1,1 L7,4 L1,7"
                    /></marker
                  >{/each}</defs
              >
              <g id="g-cp"
                ><rect
                  class="box paper"
                  x="16"
                  y="64"
                  width="150"
                  height="46"
                  rx="8"
                /><text class="nlabel" x="91" y="83" text-anchor="middle"
                  >control plane</text
                ><text class="nsub" x="91" y="98" text-anchor="middle"
                  >elixir/otp</text
                ></g
              ><g id="g-brick"
                ><rect
                  class="lane-r"
                  x="196"
                  y="28"
                  width="470"
                  height="330"
                  rx="10"
                /><text class="llabel" x="212" y="50"
                  >brick-2 · firecracker node</text
                ></g
              ><g id="g-noded"
                ><rect
                  class="box"
                  x="220"
                  y="64"
                  width="170"
                  height="46"
                  rx="8"
                /><text class="nlabel" x="305" y="83" text-anchor="middle"
                  >noded</text
                ><text class="nsub" x="305" y="98" text-anchor="middle"
                  >go daemon</text
                ></g
              >
              <g id="g-vm"
                ><rect
                  class="box paper ember-b"
                  x="410"
                  y="64"
                  width="232"
                  height="124"
                  rx="10"
                /><text class="nlabel" x="424" y="87">session vm</text><rect
                  class="pill"
                  id="r-pill"
                  x="548"
                  y="72"
                  width="80"
                  height="18"
                  rx="9"
                  style="fill: var(--ag-idle)"
                /><text
                  class="pill-t"
                  id="t-pill"
                  x="588"
                  y="85"
                  text-anchor="middle">off</text
                ><g id="g-ram"></g><text class="nsub" x="424" y="176"
                  >shim :1027 · vsock · no NIC</text
                ></g
              >
              <g id="g-scratch"
                ><rect
                  class="box"
                  x="220"
                  y="236"
                  width="170"
                  height="104"
                  rx="10"
                /><text
                  class="llabel"
                  x="232"
                  y="256"
                  style="text-transform: none">node scratch · nvme</text
                ><g id="g-chipBase"
                  ><rect
                    class="box paper ember-b"
                    x="228"
                    y="266"
                    width="154"
                    height="26"
                    rx="6"
                  /><text
                    class="nsub"
                    x="236"
                    y="283"
                    style="fill: var(--em-muted)">base memfile · shared</text
                  ></g
                ><g id="g-chipWs"
                  ><rect
                    class="box paper amber-b"
                    x="228"
                    y="300"
                    width="154"
                    height="26"
                    rx="6"
                  /><text
                    class="nsub"
                    x="236"
                    y="317"
                    style="fill: var(--em-muted)">workspace.img · yours</text
                  ></g
                ></g
              >
              <g id="g-sidecar"
                ><rect
                  class="box paper good-b"
                  x="410"
                  y="236"
                  width="232"
                  height="104"
                  rx="10"
                /><text class="nlabel" x="424" y="257">egress sidecar</text
                ><text class="nsub" x="424" y="272">holds the real secrets</text
                ><rect
                  class="swap-r"
                  x="422"
                  y="284"
                  width="208"
                  height="20"
                /><text class="swap-t" id="t-fake" x="428" y="298"
                  >Bearer ember-guest…dummy…</text
                ><line
                  class="strike"
                  id="s-fake"
                  x1="424"
                  y1="294"
                  x2="620"
                  y2="294"
                /><rect
                  class="swap-r"
                  id="r-real"
                  x="422"
                  y="310"
                  width="208"
                  height="20"
                  opacity="0"
                  style="fill: var(--em-good-dim)"
                /><text
                  class="swap-t good"
                  id="t-real"
                  x="428"
                  y="324"
                  opacity="0">Bearer ●●●●●●●● · real</text
                ></g
              >
              <g id="g-api"
                ><rect
                  class="box paper"
                  x="700"
                  y="236"
                  width="164"
                  height="46"
                  rx="8"
                /><text class="nlabel" x="782" y="255" text-anchor="middle"
                  >api.anthropic.com</text
                ><text class="nsub" x="782" y="270" text-anchor="middle"
                  >tls ends at sidecar</text
                ></g
              ><g id="g-github"
                ><rect
                  class="box paper"
                  x="700"
                  y="294"
                  width="164"
                  height="46"
                  rx="8"
                /><text class="nlabel" x="782" y="313" text-anchor="middle"
                  >github.com</text
                ><text class="nsub" x="782" y="328" text-anchor="middle"
                  >clone + push</text
                ></g
              >
              <g id="g-s3"
                ><rect
                  class="lane-s3-r"
                  x="196"
                  y="390"
                  width="470"
                  height="92"
                  rx="10"
                /><text class="llabel frost" x="212" y="412"
                  >seaweedfs s3 · bucket embervm</text
                ><g id="g-s3base"
                  ><rect
                    class="box paper frost-b"
                    x="220"
                    y="424"
                    width="130"
                    height="30"
                    rx="6"
                  /><text class="nsub frost" x="232" y="443"
                    >base/ snapshots</text
                  ></g
                ><g id="g-s3ws"
                  ><rect
                    class="box paper frost-b"
                    x="358"
                    y="424"
                    width="272"
                    height="30"
                    rx="6"
                  /><text class="nsub frost" x="370" y="443"
                    >session-workspace/&lt;lineage&gt; · 7 d gc</text
                  ></g
                ></g
              ><g id="g-brick3"
                ><rect
                  class="lane-r"
                  x="700"
                  y="390"
                  width="164"
                  height="92"
                  rx="10"
                /><text class="llabel" x="712" y="410">brick-3</text><rect
                  class="box paper ember-b"
                  x="712"
                  y="418"
                  width="140"
                  height="52"
                  rx="8"
                /><text class="nlabel" x="782" y="439" text-anchor="middle"
                  >fresh vm</text
                ><text class="nsub" x="782" y="455" text-anchor="middle"
                  >--resume · intact</text
                ></g
              >
              <path
                class="epath ep-ember"
                id="p-grpc"
                d="M166,87 L216,87"
              /><text class="elabel el-ember" id="l-grpc" x="176" y="78"
                >gRPC</text
              ><path
                class="epath ep-ember"
                id="p-load"
                d="M300,264 C315,235 370,215 440,192"
              /><text class="elabel el-ember" id="l-load" x="204" y="210"
                >load · 2.5 ms</text
              ><path
                class="epath ep-amber"
                id="p-patch"
                d="M320,300 C335,258 390,220 468,192"
              /><text class="elabel el-amber" id="l-patch" x="204" y="226"
                >patch</text
              ><path
                class="epath ep-good"
                id="p-vsock"
                d="M526,188 L526,232"
              /><text class="elabel el-good" id="l-vsock" x="534" y="212"
                >vsock</text
              ><path
                class="epath ep-good"
                id="p-egress"
                d="M642,259 L694,259"
              /><text class="elabel el-good" id="l-egress" x="700" y="228"
                >swap → real header</text
              ><path
                class="epath ep-good"
                id="p-git"
                d="M642,317 L694,317"
              /><text class="elabel el-good" id="l-git" x="700" y="358"
                >clone · token attached</text
              ><path
                class="epath ep-frost"
                id="p-bank"
                d="M300,328 C310,368 380,384 418,424"
              /><text class="elabel el-frost" id="l-bank" x="200" y="378"
                >retire → export</text
              ><path
                class="epath ep-frost"
                id="p-restore"
                d="M634,439 L706,437"
              /><text class="elabel el-frost" id="l-restore" x="630" y="470"
                >restore</text
              >
            </svg>
            <div class="dg-legend">
              <span><i class="lg-ember"></i> lifecycle</span><span
                ><i class="lg-amber"></i> data in flight</span
              ><span><i class="lg-good"></i> credentialed egress</span><span
                ><i class="lg-frost"></i> durable, to and from s3</span
              >
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="static-story">
    <h1>One microVM per agent session.</h1>
    <p class="body">
      Killed on idle, rebuilt from disk days later on whichever node has room.
      Credentials stay <b>outside</b> the VM.
    </p>
    <ol>
      <li>
        01 · wake: vm awake · 2.5 ms, one shared base snapshot, volume patched
        while paused
      </li>
      <li>02 · hydrate: clone jomcgi/homelab through the egress sidecar</li>
      <li>03 · creds: real credentials stay outside the vm</li>
      <li>04 · park: idle 20 s, vm destroyed, disk kept</li>
      <li>05 · resume: workspace in s3, restored on brick-3</li>
    </ol>
  </div>
  <main class="doc">
    <h2 class="h2">Where a session lives</h2>
    <dl class="tiers-mini">
      <div>
        <dt class="ram">guest ram</dt>
        <dd>disposable · rebuilt in <b>2.5 ms</b> · never snapshotted</dd>
      </div>
      <div>
        <dt class="disk">node scratch</dt>
        <dd>
          shared base memfiles + <b>workspace.img</b> · what <b>--resume</b> reads
        </dd>
      </div>
      <div>
        <dt class="s3">seaweedfs s3</dt>
        <dd>retired workspaces · <b>survives the machine</b> · 7 d gc</dd>
      </div>
    </dl>
    <h2 class="h2">Keep going</h2>
    <div class="doors">
      <a class="door" href="/ember/firecracker"
        ><span class="k">explainer</span>
        <h3>How Firecracker resumes a VM</h3>
        <p>
          What a microVM snapshot actually contains, and how a full machine
          comes back in ~22 ms.
        </p>
        <span class="go">ember/firecracker →</span></a
      ><a class="door" href="/ember"
        ><span class="k">the mini-site</span>
        <h3>Ember</h3>
        <p>
          The workload orchestrator all of this runs on, with live demos you can
          wake yourself.
        </p>
        <span class="go">ember →</span></a
      >
    </div>
    <footer class="foot">
      <span
        >Elixir/OTP control plane · Go node daemon · Firecracker microVMs ·
        running on this cluster</span
      ><span>jomcgi.dev</span>
    </footer>
  </main>
</div>

<style>
  .agstory {
    min-height: 100vh;
    background: var(--em-ground);
    color: var(--em-ink);
    font-family: var(--em-sans);
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    position: fixed;
    inset: 0 0 auto;
    z-index: 40;
    display: flex;
    padding: 14px 28px;
    font: 12.5px var(--em-mono);
    color: var(--em-muted);
    pointer-events: none;
  }
  .topbar a {
    color: inherit;
    pointer-events: auto;
    text-decoration: none;
  }
  .topbar strong {
    color: var(--em-ink);
    font-weight: 600;
  }
  .scroller {
    height: 700vh;
    position: relative;
  }
  .stage {
    position: sticky;
    top: 0;
    height: 100dvh;
    overflow: hidden;
    display: flex;
    align-items: center;
  }
  .hero {
    position: absolute;
    inset: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    padding: 24px;
    background: var(--em-ground);
  }
  .hero .kicker,
  .stats,
  .cue {
    font: 12.5px var(--em-mono);
    color: var(--em-faint);
  }
  .hero .kicker {
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 18px;
  }
  .hero h1 {
    margin: 0;
    max-width: 15ch;
    font-size: clamp(38px, 6.4vw, 72px);
    font-weight: 850;
    letter-spacing: -0.035em;
    line-height: 1.02;
    text-wrap: balance;
  }
  .hero .em {
    color: var(--em-ember);
  }
  .hero .sub {
    max-width: 46ch;
    margin: 20px 0 0;
    color: var(--em-muted);
    font-size: clamp(16px, 2vw, 20px);
    line-height: 1.5;
  }
  .stats b {
    color: var(--em-ink);
    font-weight: 650;
  }
  .stats {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 6px 14px;
    margin-top: 22px;
    font-variant-numeric: tabular-nums;
  }
  .stats b {
    color: var(--em-ember-deep);
    font-weight: 600;
  }
  .stats .sep {
    color: var(--em-line);
  }
  .cue {
    position: absolute;
    bottom: 26px;
    left: 50%;
    transform: translateX(-50%);
    animation: cue-bob 2.2s ease-in-out infinite;
  }
  @keyframes cue-bob {
    0%,
    100% {
      transform: translate(-50%, 0);
    }
    50% {
      transform: translate(-50%, 6px);
    }
  }
  .stagegrid {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(270px, 330px) minmax(0, 1fr);
    gap: 24px;
    padding: 68px clamp(18px, 3.5vw, 48px) 32px;
    max-width: 1280px;
    margin: 0 auto;
  }
  .chat-col,
  .machine-col {
    min-width: 0;
    position: relative;
  }
  .chat {
    height: 100%;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
  }
  .chat-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    border-bottom: 1px solid var(--em-line-soft);
    font: 12px var(--em-mono);
    color: var(--em-faint);
  }
  .name {
    color: var(--em-ink);
    font-weight: 600;
    white-space: nowrap;
  }
  .vmchip {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 3px 11px;
    border: 1px solid var(--em-line);
    border-radius: 20px;
    background: var(--em-ground);
    color: var(--em-muted);
    font: 600 11.5px var(--em-mono);
    white-space: nowrap;
  }
  .vmchip .d {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--ag-idle);
  }
  /* awake/asleep arrive via classList from frame(), so they must be
     :global or the compiler prunes them as unused (fcstory convention). */
  .vmchip:global(.awake) .d {
    background: var(--em-ember);
    box-shadow: 0 0 6px 1px var(--ag-chip-shadow);
  }
  .vmchip:global(.asleep) .d {
    background: var(--em-frost);
  }
  .chat-view {
    flex: 1;
    overflow: hidden;
    position: relative;
  }
  .chat-items {
    display: flex;
    flex-direction: column;
    gap: 15px;
    padding: 18px 16px;
  }
  .ci {
    opacity: 0;
  }
  .msg {
    max-width: 88%;
    padding: 9px 13px;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.5;
  }
  .msg.you {
    align-self: flex-end;
    background: var(--em-ink);
    color: var(--em-on-color);
    border-bottom-right-radius: 4px;
  }
  .msg.bot {
    align-self: flex-start;
    background: var(--em-ground);
    border: 1px solid var(--em-line-soft);
    border-bottom-left-radius: 4px;
  }
  .evt {
    align-self: center;
    color: var(--em-faint);
    text-align: center;
    font: 11px/1.6 var(--em-mono);
  }
  .evt b {
    color: var(--em-ember-deep);
    font-weight: 600;
  }
  .evt.frost,
  .evt.frost b {
    color: var(--ag-frost-deep);
  }
  .evt.divider {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--ag-frost-deep);
  }
  .evt.divider::before,
  .evt.divider::after {
    content: "";
    flex: 1;
    border-top: 1px solid var(--em-line);
  }
  .machine-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .dg-frame {
    flex: 1;
    padding: 16px 18px 10px;
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
  }
  .dg {
    display: block;
    width: 100%;
    max-height: 62vh;
  }
  .dg .lane-r {
    fill: none;
    /* --em-line vanished on the white panel; the landing arch's strong
       rule color is the precedent for diagram lane borders. */
    stroke: var(--ag-line-strong);
    stroke-dasharray: 4 4;
  }
  .dg .lane-s3-r {
    fill: var(--ag-s3-fill);
    stroke: var(--em-frost);
    stroke-dasharray: 4 4;
  }
  .dg .box {
    fill: var(--em-ground);
    stroke: var(--em-line);
  }
  .dg .box.paper {
    fill: var(--em-panel);
  }
  .dg .box.good-b {
    stroke: var(--em-good);
  }
  .dg .box.frost-b {
    stroke: var(--em-frost-dim);
  }
  .dg .box.amber-b {
    stroke: var(--em-amber);
  }
  .dg .box.ember-b {
    stroke: var(--em-ember-dim);
  }
  .dg text {
    font-family: var(--em-sans);
    fill: var(--em-ink);
  }
  .dg .llabel,
  .dg .nsub,
  .dg .elabel {
    font-family: var(--em-mono);
  }
  .dg .llabel,
  .dg .nsub {
    fill: var(--em-faint);
    font-size: 11px;
  }
  .dg .llabel {
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .dg .llabel.frost,
  .dg .nsub.frost {
    fill: var(--ag-frost-deep);
  }
  .dg .nlabel {
    font-size: 13px;
    font-weight: 600;
  }
  .dg .epath {
    fill: none;
    stroke-width: 2;
  }
  .dg .ep-ember {
    stroke: var(--em-ember);
    marker-end: url(#m-ember);
  }
  .dg .ep-amber {
    stroke: var(--em-amber);
    marker-end: url(#m-amber);
  }
  .dg .ep-frost {
    stroke: var(--em-frost);
    marker-end: url(#m-frost);
  }
  .dg .ep-good {
    stroke: var(--em-good);
    marker-end: url(#m-good);
  }
  .dg .elabel {
    font-size: 11.5px;
    font-weight: 600;
  }
  .dg .el-ember {
    fill: var(--em-ember-deep);
  }
  .dg .el-amber {
    fill: var(--ag-amber-deep);
  }
  .dg .el-frost {
    fill: var(--ag-frost-deep);
  }
  .dg .el-good {
    fill: var(--em-good-deep);
  }
  /* RAM cells are built with createElementNS, so they never get the
     scoping hash: the selector must be :global. The transition is what
     makes the memory sweep fade instead of popping cell by cell. */
  .dg :global(.cellr) {
    fill: var(--ag-idle);
    transition: fill 0.3s ease;
  }
  .dg .mk {
    fill: none;
    stroke-width: 1.6;
  }
  .dg .pill {
    rx: 9px;
  }
  .dg .pill-t {
    font: 600 10px var(--em-mono);
    fill: var(--em-on-color);
  }
  .dg .swap-r {
    fill: var(--em-track);
    rx: 4px;
  }
  .dg .swap-t {
    fill: var(--em-muted);
    font: 10px var(--em-mono);
  }
  .dg .swap-t.good {
    fill: var(--em-good-deep);
  }
  .dg .strike {
    stroke: var(--em-ember-deep);
    stroke-width: 1.4;
    opacity: 0;
  }
  .dg-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 18px;
    padding: 8px 4px 4px;
    color: var(--em-muted);
    font: 11px var(--em-mono);
  }
  .dg-legend span {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  .dg-legend i {
    display: inline-block;
    width: 18px;
    height: 0;
    border-top: 2px solid;
  }
  .lg-ember {
    border-color: var(--em-ember);
  }
  .lg-amber {
    border-color: var(--em-amber);
  }
  .lg-frost {
    border-color: var(--em-frost);
  }
  .lg-good {
    border-color: var(--em-good);
  }
  .wire {
    display: flex;
    gap: 8px;
    overflow: hidden;
    padding: 9px 13px;
    border: 1px solid var(--em-line);
    border-radius: 9px;
    background: var(--em-panel);
    box-shadow: var(--em-shadow-soft);
    white-space: nowrap;
    font: 12px var(--em-mono);
    opacity: 0;
  }
  .wp {
    color: var(--em-faint);
  }
  .wt {
    color: var(--em-ink);
  }
  /* w-* classes arrive via classList from frame(): must be :global. */
  .wt:global(.w-ember) {
    color: var(--em-ember-deep);
  }
  .wt:global(.w-amber) {
    color: var(--ag-amber-deep);
  }
  .wt:global(.w-good) {
    color: var(--em-good-deep);
  }
  .wt:global(.w-frost) {
    color: var(--ag-frost-deep);
  }
  .wc {
    color: var(--em-ember);
    animation: wc-blink 1s steps(1) infinite;
  }
  @keyframes wc-blink {
    50% {
      opacity: 0;
    }
  }
  .doc {
    max-width: 880px;
    margin: 0 auto;
    padding: 20px 24px 90px;
  }
  .h2 {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin: 56px 0 16px;
    font-size: 24px;
    font-weight: 750;
    letter-spacing: -0.015em;
  }
  .h2::before {
    content: "##";
    color: var(--em-ember);
    font: 16px var(--em-mono);
  }
  .body {
    margin: 0 0 14px;
    max-width: 68ch;
    color: var(--em-muted);
    font-size: 15.5px;
    line-height: 1.55;
  }
  .body b {
    color: var(--em-ink);
    font-weight: 600;
  }
  .tiers-mini {
    margin: 0;
    border-top: 1px solid var(--em-line);
  }
  .tiers-mini > div {
    display: grid;
    grid-template-columns: 140px 1fr;
    gap: 16px;
    padding: 10px 4px;
    border-bottom: 1px solid var(--em-line);
  }
  .tiers-mini dt,
  .tiers-mini dd {
    margin: 0;
    font: 12.5px/1.6 var(--em-mono);
  }
  .tiers-mini dt {
    font-weight: 600;
  }
  .ram {
    color: var(--em-ember-deep);
  }
  .disk {
    color: var(--ag-amber-deep);
  }
  .s3 {
    color: var(--ag-frost-deep);
  }
  .tiers-mini dd {
    color: var(--em-muted);
  }
  .tiers-mini dd b {
    color: var(--em-ink);
    font-weight: 600;
  }
  .doors {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 16px;
  }
  .door {
    display: block;
    padding: 20px 22px 18px;
    border: 1px solid var(--em-line);
    border-radius: 12px;
    background: var(--em-panel);
    box-shadow: var(--em-shadow-soft);
    text-decoration: none;
    transition:
      border-color 0.18s ease,
      transform 0.18s ease;
  }
  .door:hover {
    border-color: var(--em-ember-dim);
    transform: translateY(-2px);
  }
  .door .k,
  .go {
    color: var(--em-faint);
    font: 11px var(--em-mono);
    text-transform: uppercase;
  }
  .door h3 {
    margin: 6px 0;
    color: var(--em-ink);
    font-size: 18px;
  }
  .door p {
    margin: 0;
    color: var(--em-muted);
    font-size: 14px;
    line-height: 1.5;
  }
  .go {
    display: inline-block;
    margin-top: 12px;
    color: var(--em-ember-deep);
    text-transform: none;
  }
  .foot {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-top: 60px;
    padding-top: 18px;
    border-top: 2px solid var(--em-ember-deep);
    color: var(--em-faint);
    font: 12.5px var(--em-mono);
  }
  .static-story {
    display: none;
    max-width: 720px;
    margin: 0 auto;
    padding: 90px 24px 20px;
  }
  .static-story li {
    margin: 12px 0;
    color: var(--em-muted);
    font: 13px/1.5 var(--em-mono);
  }
  @media (prefers-reduced-motion: reduce) {
    .scroller {
      display: none;
    }
    .static-story {
      display: block;
    }
    .cue {
      animation: none;
    }
  }
  @media (max-width: 880px) {
    .stagegrid {
      grid-template-columns: 1fr;
      grid-template-rows: minmax(0, 1fr) auto;
      padding: 60px 14px 14px;
    }
    .machine-col {
      order: 1;
    }
    .chat-col {
      order: 2;
      height: 210px;
    }
    .msg {
      font-size: 12.5px;
    }
    .evt {
      font-size: 10px;
    }
    .dg {
      max-height: 46vh;
    }
    .tiers-mini > div {
      grid-template-columns: 1fr;
      gap: 2px;
    }
  }
</style>
