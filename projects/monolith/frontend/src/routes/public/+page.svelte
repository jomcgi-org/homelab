<script>
  import { onMount, untrack } from "svelte";
  import { page } from "$app/state";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import { apps } from "$lib/public/apps.js";
  import {
    deployReadout,
    startHomepageStatsPolling,
  } from "./homepage-stats.js";
  import { createPortrait } from "./home-portrait.js";

  // The homepage is one unit, read top to bottom like a Braun calculator: a
  // nameplate, a display window, a grille with the portrait behind it, a
  // keypad with legends, and the writing as legends under the keypad. The
  // display is fixed in size and is the only thing that changes: whatever
  // the visitor hovers is read out there. The portrait has its own engine
  // (home-portrait.js); the chrome stays still.
  let { data } = $props();
  let stats = $state(untrack(() => data.stats));
  // The foot keeps the site's other sections one tap away on a phone, the
  // same list the sheet footer carried; the blog appears once a post exists.
  const sections = $derived([
    { label: "cv", href: "/cv" },
    { label: "engineering", href: "/engineering" },
    ...(page.data?.hasBlog ? [{ label: "blog", href: "/blog" }] : []),
    { label: "docs", href: "/docs" },
  ]);
  const deploy = $derived(deployReadout(stats));
  const posts = $derived(data.posts ?? []);

  // One line per key, in the visitor's register. The app keys are looked up
  // in the apps registry by slug, so a slug that leaves the registry drops
  // its key here rather than failing the page. The notes line carries the
  // live fact count when the stats snapshot has one.
  const APP_LINES = {
    notes: "Ask my notes: a knowledge graph over all of them.",
    grimoire: "A D&D campaign manager built on grants.",
    ships: "Every ship off Scotland right now, from live AIS.",
    stars: "Where the sky will be dark and clear tonight.",
    hikes: "Scottish hill walks, planned by day and weather.",
    trips: "A dash-mounted GoPro turned into a browseable journey.",
  };
  const ORDER = ["notes", "grimoire", "ships", "stars", "hikes", "trips"];
  const keys = $derived.by(() => {
    const facts = stats?.knowledge?.facts;
    const lines = {
      ...APP_LINES,
      ...(facts != null
        ? {
            notes: `Ask my notes: a knowledge graph of ${facts.toLocaleString("en-GB")} of them.`,
          }
        : {}),
    };
    return [
      {
        href: "/slop/factory",
        legend: "software factory",
        primary: true,
        big: "Slop factory",
        small:
          "Agents doing platform work on this cluster, live. Tag one in Discord and it answers.",
      },
      {
        href: "/cv",
        legend: "cv",
        big: "CV",
        small: "Ten years, underwriting to platform engineering.",
      },
      {
        href: "/docs",
        legend: "docs",
        big: "Docs",
        small: "How this is built: cluster, build, agents.",
      },
      ...ORDER.flatMap((slug) => {
        const app = apps.find((a) => a.slug === slug);
        return app
          ? [
              {
                href: app.href,
                legend: slug,
                big: app.label,
                small: lines[slug] ?? app.desc,
              },
            ]
          : [];
      }),
    ];
  });

  // What the display is reading out: a key, a post, or nothing (the name).
  // Touch never reads out, and neither does the phone layout at all (hover,
  // focus or otherwise): its glass fits the name, so changing the readout
  // would resize the unit under the finger. Keep the query in step with the
  // phone media block below.
  const PHONE_QUERY = "(max-width: 760px)";
  let reading = $state(null);
  let slopOn = $state(false);
  let slopTimer;
  function readout(item, pointerType) {
    if (pointerType === "touch") return;
    if (item && window.matchMedia(PHONE_QUERY).matches) return;
    clearTimeout(slopTimer);
    reading = item;
    slopOn = false;
    if (item?.primary) slopTimer = setTimeout(() => (slopOn = true), 60);
  }

  // The neighbours of a pressed key flinch in a ripple that spreads with
  // distance; the key itself sinks through :active.
  let padEl;
  function press(index) {
    const caps = padEl ? [...padEl.querySelectorAll(".key")] : [];
    const at = (i) => ({ x: i % 3, y: Math.floor(i / 3) });
    const from = at(index);
    caps.forEach((k, j) => {
      if (j === index) return;
      const d = Math.hypot(at(j).x - from.x, at(j).y - from.y);
      setTimeout(() => {
        k.classList.add("nudge");
        setTimeout(() => k.classList.remove("nudge"), 90);
      }, d * 45);
    });
  }

  let clock = $state("--:--:--");
  let ledOff = $state(false);
  let cursorOff = $state(false);
  let portEl;
  let canvasEl;

  const scheme = () => {
    const t = document.documentElement.dataset.theme;
    if (t === "dark" || t === "light") return t;
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  };

  onMount(() => {
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const timers = [];
    const later = (fn, ms) => timers.push(setTimeout(fn, ms));

    const stopStats = startHomepageStatsPolling(data.stats, (snapshot) => {
      stats = snapshot;
    });
    const portrait = createPortrait({
      canvas: canvasEl,
      host: portEl,
      src: "/joe-grey.png",
      scheme,
      reduceMotion: reduce,
    });

    const tz = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/London",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    clock = tz.format(new Date());
    const clockTimer = setInterval(() => (clock = tz.format(new Date())), 1000);

    // The light and the cursor blink on their own irregular clocks.
    const blink = () => {
      ledOff = true;
      later(() => (ledOff = false), 90 + Math.random() * 120);
      later(blink, 700 + Math.random() * 3800);
    };
    const cblink = () => {
      cursorOff = !cursorOff;
      later(
        cblink,
        cursorOff ? 180 + Math.random() * 220 : 400 + Math.random() * 1400,
      );
    };
    if (!reduce) {
      blink();
      cblink();
    }

    return () => {
      stopStats?.();
      portrait.destroy();
      clearInterval(clockTimer);
      clearTimeout(slopTimer);
      for (const t of timers) clearTimeout(t);
    };
  });
</script>

<Seo
  title="Joe McGinley · Senior Platform Engineer"
  description="Joe McGinley's homelab: a Kubernetes cluster running agents, maps, and a knowledge graph."
  path="/"
/>

<main class="td sheet home">
  <div class="unit">
    <div class="plate">
      <span class="brand">jomcgi.dev</span>
      <span class="meta">
        <span>scotland <b>{clock}</b></span>
        {#if deploy}
          <span class="deploy">deployed <b>{deploy.ago}</b> ago</span>
        {/if}
        <span class="led"
          ><i class:off={ledOff}></i> <span>{deploy?.sha ?? "live"}</span></span
        >
      </span>
      <SchemeToggle />
    </div>

    <div class="glass" class:slop={slopOn} aria-live="polite">
      <h1 class="big" class:sr-only={reading}>
        Joe McGinley, Scotland.{#if !reading}<span
            class="cursor"
            class:off={cursorOff}
          ></span>{/if}
      </h1>
      {#if reading}
        <p class="big" aria-hidden="true">
          {#if reading.primary}
            <span class="sw">Software</span>
            <span class="sl">Slop&nbsp;</span>factory
          {:else}
            {reading.big}
          {/if}<span class="cursor" class:off={cursorOff}></span>
        </p>
        <p class="small">{reading.small}</p>
      {:else}
        <p class="small">
          Building production grade infra for weekend side quests and keeping
          <a href="https://semgrep.dev">Semgrep</a> online.
        </p>
      {/if}
    </div>

    <div class="deck">
      <div
        class="portrait"
        role="img"
        aria-label="Joe, a greyscale print behind a perforated grille, cut into strips that scatter when you move"
      >
        <div class="print" bind:this={portEl}>
          <canvas bind:this={canvasEl}></canvas>
        </div>
        <div class="holes"></div>
      </div>

      <div class="right">
        <nav
          class="pad"
          aria-label="Site"
          bind:this={padEl}
          onpointerleave={() => readout(null)}
        >
          {#each keys as key, i (key.href)}
            <a
              class="key"
              class:go={key.primary}
              href={key.href}
              aria-label={key.primary ? "Slop factory" : undefined}
              onpointerenter={(e) => readout(key, e.pointerType)}
              onpointerdown={() => press(i)}
              onfocus={() => readout(key)}
              onblur={() => readout(null)}
            >
              <span class="cap"><i></i></span>
              <span class="legend">{key.legend}</span>
            </a>
          {/each}
        </nav>

        {#if posts.length}
          <section
            class="writing"
            aria-label="Writing"
            onpointerleave={() => readout(null)}
          >
            <span class="lab">writing</span>
            {#each posts as post (post.slug)}
              <a
                href={`/blog/${post.slug}`}
                onpointerenter={(e) =>
                  readout(
                    { big: post.title, small: post.summary },
                    e.pointerType,
                  )}
                onfocus={() =>
                  readout({ big: post.title, small: post.summary })}
                onblur={() => readout(null)}
              >
                <time datetime={post.date}>{post.date}</time>
                <span>{post.title}</span>
              </a>
            {/each}
          </section>
        {/if}
      </div>
    </div>

    <div class="foot">
      <span class="links">
        <a href="mailto:joe@jomcgi.dev">joe@jomcgi.dev</a>
        <a href="https://github.com/jomcgi" target="_blank" rel="noreferrer"
          >github</a
        >
        <a
          href="https://linkedin.com/in/jomcgi"
          target="_blank"
          rel="noreferrer">linkedin</a
        >
      </span>
      <nav aria-label="Continue reading">
        {#each sections as section (section.href)}
          <a href={section.href}>{section.label}</a>
        {/each}
      </nav>
    </div>
  </div>
</main>

<style>
  /* The unit's own tones, derived from the sheet tokens so the scheme
     toggle moves everything together. Only the display window needs values
     of its own: it is dark glass in both schemes. */
  .home {
    --housing: var(--band);
    --panel: var(--sheet);
    --panel-2: var(--card-bg);
    --edge: var(--stroke);
    --edge-2: var(--line);
    --green: var(--ok);
    --green-ink: color-mix(in srgb, var(--ok) 80%, var(--ink));
    --glass: #0e100e; /* nosemgrep: svelte-hardcoded-color-in-style */
    --glass-text: #dfe3d2; /* nosemgrep: svelte-hardcoded-color-in-style */
    --glass-dim: #8f957f; /* nosemgrep: svelte-hardcoded-color-in-style */
    padding: 1.6rem clamp(1rem, 4vw, 3rem) 4rem;
    font-variant-numeric: tabular-nums;
    /* The keys and the switch give their own press feedback; the browser's
       tap highlight (a blue box on Android) would paint over it. */
    -webkit-tap-highlight-color: transparent;
  }

  .unit {
    max-width: 60rem;
    margin: 0 auto;
    background: var(--housing);
    border: 1px solid var(--edge);
    border-radius: 10px;
    box-shadow: 0 3px 0 var(--edge);
    padding: 1rem;
    display: grid;
    gap: 1rem;
  }

  .plate {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.3rem 1.4rem;
    padding: 0 0.2rem;
    font-family: var(--font-code);
    font-size: 0.72rem;
    color: var(--ink-2);
  }
  .plate .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1.4rem;
    min-width: 0;
  }
  .plate .brand {
    color: var(--ink);
    font-family: var(--font-ui);
    font-weight: 600;
    font-size: 0.95rem;
  }
  .plate b {
    color: var(--ink);
    font-weight: 500;
  }
  .led {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }
  .led i {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--green);
    display: inline-block;
  }
  .led i.off {
    background: var(--panel-2);
    box-shadow: 0 0 0 1px var(--edge) inset;
  }
  /* The day/night switch sits on the nameplate as a small round key, the
     same object as the keypad's caps, instead of floating over the page. */
  .plate :global(.scheme) {
    width: 1.9rem;
    height: 1.9rem;
    border-radius: 50%;
    border: 1px solid var(--edge);
    background: var(--panel);
    color: var(--ink-2);
    box-shadow: 0 2px 0 var(--edge);
    align-self: center;
  }
  .plate :global(.scheme:hover) {
    color: var(--ink);
  }
  .plate :global(.scheme:active) {
    transform: translateY(2px);
    box-shadow: none;
  }

  .glass {
    background: var(--glass);
    color: var(--glass-text);
    border-radius: 6px;
    border: 1px solid var(--edge);
    padding: 0.95rem 1.3rem;
    /* Fixed: one big line and one small line, so a readout never resizes
       the unit. The rest-state sub line runs the full width to stay on one
       line at desktop widths. */
    height: 6.3rem;
    display: grid;
    align-content: start;
    gap: 0.4rem;
    overflow: hidden;
  }
  .glass .big {
    font-weight: 400;
    font-size: clamp(1.4rem, 0.9rem + 1.6vw, 2.1rem);
    line-height: 1.12;
    letter-spacing: -0.024em;
    margin: 0;
    /* One line, always: a long post title is cut with an ellipsis rather
       than wrapping and pushing the sentence out of the box. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .glass .small {
    font-size: 0.98rem;
    line-height: 1.35;
    color: var(--glass-dim);
    margin: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .glass .small a {
    color: var(--glass-text);
    text-decoration: none;
    border-bottom: 1px solid var(--glass-dim);
  }
  .cursor {
    display: inline-block;
    width: 0.08em;
    height: 0.9em;
    background: currentColor;
    vertical-align: -0.1em;
    margin-left: 0.06em;
  }
  .cursor.off {
    opacity: 0;
  }
  /* The primary key corrects itself in the glass: Software is struck
     through, then Slop slides in after it. */
  .sw {
    position: relative;
  }
  .sw::after {
    content: "";
    position: absolute;
    left: 0;
    top: 54%;
    height: 2px;
    width: 0;
    background: currentColor;
    transition: width 0.22s ease;
  }
  .sl {
    display: inline-block;
    max-width: 0;
    overflow: hidden;
    white-space: nowrap;
    vertical-align: bottom;
    opacity: 0;
    transition:
      max-width 0.3s ease,
      opacity 0.25s ease;
  }
  .glass.slop .sw::after {
    width: 100%;
  }
  .glass.slop .sl {
    max-width: 4em;
    opacity: 1;
    transition:
      max-width 0.3s ease 0.18s,
      opacity 0.25s ease 0.2s;
  }

  .deck {
    display: grid;
    grid-template-columns: minmax(0, 20rem) minmax(0, 1fr);
    gap: 1rem;
    align-items: stretch;
  }
  /* The grille: the portrait sits behind a perforated plate of the housing
     material. The holes are a mask, so the plate costs no image. */
  .portrait {
    position: relative;
    aspect-ratio: 3 / 4;
    background: var(--panel);
    overflow: hidden;
    border-radius: 6px;
    border: 1px solid var(--edge);
    touch-action: pan-y;
  }
  /* The print is the 3:4 picture the engine draws into; it fills the frame
     except on a tall phone, where the frame is wider than the picture and
     the grille runs on either side. The canvas is out of flow, so its pixel
     size can never feed back into the box size() measures. */
  .print {
    position: absolute;
    inset: 0;
  }
  .print canvas {
    position: absolute;
    inset: 0;
    display: block;
    width: 100%;
    height: 100%;
  }
  .print:global(.neg) canvas {
    filter: invert(1);
  }
  .holes {
    position: absolute;
    inset: 0;
    background: var(--housing);
    pointer-events: none;
    -webkit-mask: radial-gradient(
        circle at center,
        transparent 0 2.2px,
        #000 2.8px
      )
      0 0 / 6px 6px;
    mask: radial-gradient(circle at center, transparent 0 2.2px, #000 2.8px) 0
      0 / 6px 6px;
  }

  .right {
    display: grid;
    grid-template-rows: auto 1fr;
    gap: 1rem;
    min-width: 0;
  }
  /* The keypad: round convex keys with legends beneath. */
  .pad {
    display: grid;
    grid-template-columns: repeat(3, 6.2rem);
    justify-content: start;
    gap: 0.7rem 0.6rem;
    align-content: start;
    padding: 0.3rem 0.4rem 0;
  }
  .key {
    display: grid;
    justify-items: center;
    gap: 0.45rem;
    text-decoration: none;
    color: var(--ink);
    min-width: 0;
    cursor: pointer;
  }
  .key .cap {
    width: 3.4rem;
    height: 3.4rem;
    border-radius: 50%;
    background: var(--panel);
    border: 1px solid var(--edge);
    box-shadow:
      0 3px 0 var(--edge),
      inset 0 1px 0 color-mix(in srgb, var(--panel) 60%, transparent);
    display: grid;
    place-items: center;
    transition:
      background-color 0.16s ease,
      border-color 0.16s ease,
      box-shadow 0.16s ease,
      transform 0.16s ease;
  }
  .key .cap i {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--edge);
    transition: background-color 0.16s ease;
  }
  .key .legend {
    font-family: var(--font-code);
    font-size: 0.7rem;
    color: var(--ink-2);
    letter-spacing: 0.01em;
    text-align: center;
    line-height: 1.2;
    transition: color 0.16s ease;
  }
  .key.go .cap {
    background: var(--green);
    border-color: var(--green-ink);
    box-shadow: 0 3px 0 var(--green-ink);
  }
  .key.go .cap i {
    background: var(--panel);
  }
  .key:focus-visible {
    outline: none;
  }
  .key:focus-visible .cap,
  .key:active .cap {
    background: var(--ink);
    border-color: var(--ink);
  }
  .key:focus-visible .cap i,
  .key:active .cap i {
    background: var(--housing);
  }
  .key:focus-visible .legend,
  .key:active .legend {
    color: var(--ink);
  }
  .key:active .cap {
    transform: translateY(3px);
    box-shadow: 0 0 0 var(--edge);
  }
  .key:global(.nudge) .cap {
    transform: translateY(1px);
    box-shadow: 0 2px 0 var(--edge);
  }
  @media (hover: hover) {
    .key:hover .cap {
      background: var(--ink);
      border-color: var(--ink);
      transform: translateY(1px);
      box-shadow: 0 2px 0 var(--edge);
    }
    .key:hover .cap i {
      background: var(--housing);
    }
    .key.go:hover .cap {
      background: var(--ink);
      box-shadow: 0 2px 0 var(--green-ink);
    }
    .key.go:hover .cap i {
      background: var(--green);
    }
    .key:hover .legend {
      color: var(--ink);
    }
  }

  .writing {
    display: grid;
    gap: 0.35rem;
    align-content: end;
    padding: 0.8rem 0.4rem 0;
    border-top: 1px solid var(--edge-2);
  }
  .writing .lab {
    font-family: var(--font-code);
    font-size: 0.68rem;
    color: var(--ink-2);
    letter-spacing: 0.02em;
  }
  .writing a {
    display: grid;
    grid-template-columns: 6.2rem minmax(0, 1fr);
    gap: 1rem;
    text-decoration: none;
    align-items: baseline;
    padding: 0.25rem 0;
  }
  .writing time {
    font-family: var(--font-code);
    font-size: 0.7rem;
    color: var(--ink-2);
  }
  .writing span {
    font-size: 0.98rem;
    font-weight: 500;
    letter-spacing: -0.01em;
    line-height: 1.25;
    transition: color 0.16s ease;
  }
  @media (hover: hover) {
    .writing a:hover span {
      color: var(--accent-ink);
    }
  }

  .foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.4rem 1.4rem;
    padding: 0.2rem 0.2rem 0;
    font-family: var(--font-code);
    font-size: 0.72rem;
    color: var(--ink-2);
  }
  .foot .links,
  .foot nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1.1rem;
  }
  .foot a {
    color: var(--ink);
    text-decoration: none;
    border-bottom: 1px solid var(--edge);
  }
  .foot a:hover {
    border-bottom-color: var(--ink);
  }

  @media (prefers-reduced-motion: reduce) {
    .key .cap,
    .key .cap i,
    .key .legend,
    .sw::after,
    .sl,
    .writing span {
      transition: none;
    }
  }
  @media (max-width: 760px) {
    /* On a phone the unit is one object on one screen: it fills the visible
       viewport (dvh, so the browser's toolbars are excluded) and the deck
       takes whatever height the nameplate, glass and footer leave. A screen
       too short for the keypad (a phone on its side) scrolls rather than
       crushing it. */
    .home {
      display: flex;
      flex-direction: column;
      min-height: 100vh;
      min-height: 100dvh;
      padding: 0.5rem;
    }
    .unit {
      flex: 1 0 auto;
      width: 100%;
      margin: 0;
      padding: 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.7rem;
    }
    /* The nameplate: brand and switch on the first row, readouts beneath.
       The switch is placed explicitly; auto-placement would put it after
       the full-width readouts. */
    .plate {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 0.35rem 1rem;
    }
    .plate :global(.scheme) {
      grid-row: 1;
      grid-column: 2;
    }
    .plate .meta {
      grid-column: 1 / -1;
    }
    /* The portrait and the keypad share the deck. The deck is a size
       container, so the portrait can be as tall as the deck allows while
       leaving the keypad a floor wide enough for three caps and their
       legends (8.7rem, plus the gap); its width follows from the 3:4
       ratio. The deck's own floor is the keypad's height. */
    .deck {
      flex: 1 1 0;
      min-height: 13rem;
      container: deck / size;
      display: flex;
      flex-wrap: wrap;
      gap: 0.8rem;
      align-items: center;
      align-content: center;
    }
    /* Side by side, the grille frame runs the deck's full height and the
       picture stands on its floor (the cut-out's shoulders meet the frame),
       so spare height is headroom in the grille, not empty housing; the
       keypad stands on the same line. The width leaves the keypad its floor and is capped at
       18rem (a 24rem tall picture) for a small tablet; 13.5rem by 18rem
       where container units are missing (Safari 15). */
    .portrait {
      flex: none;
      aspect-ratio: auto;
      width: 13.5rem;
      width: min(100cqw - 9.6rem, 18rem, 75cqh);
      height: 18rem;
      height: 100cqh;
    }
    .print {
      inset: auto 0 0;
      aspect-ratio: 3 / 4;
    }
    .right {
      display: contents;
    }
    .pad {
      flex: 1;
      min-width: 8.7rem;
      max-width: 14rem;
      grid-template-columns: repeat(3, 1fr);
      grid-template-rows: repeat(3, minmax(0, 1fr));
      /* As tall as the portrait beside it, never below the keypad's own
         height, so the keys spread over the picture's height and a tall
         phone centres the pair rather than stretching the keys apart. */
      height: 18rem;
      height: min(100cqh, max(13rem, min((100cqw - 9.6rem) * 4 / 3, 24rem)));
      align-self: end;
      justify-content: stretch;
      align-content: stretch;
      align-items: center;
      gap: 0.55rem 0.2rem;
      padding: 0.1rem 0 0;
    }
    .key {
      gap: 0.3rem;
    }
    .key .cap {
      width: 2.5rem;
      height: 2.5rem;
    }
    .key .legend {
      font-size: 0.56rem;
      letter-spacing: 0;
    }
    /* The latest posts do not fit one screen; the blog link in the footer
       leads to them. */
    .writing {
      display: none;
    }
    /* The glass is narrow on a phone: the top line may take two lines
       (clamped, so a long title still cannot push the sentence out) and
       the sentence wraps beneath it. */
    /* The desktop pitch is right at desktop size; a phone-sized portrait
       gets a finer grille so the face still reads through. */
    .holes {
      -webkit-mask: radial-gradient(
          circle at center,
          transparent 0 1px,
          #000 1.25px
        )
        0 0 / 2.8px 2.8px;
      mask: radial-gradient(circle at center, transparent 0 1px, #000 1.25px) 0
        0 / 2.8px 2.8px;
    }
    /* A deck taller than 4:3 (a tall phone) stacks: the grille spans the
       unit's width above the keypad, the picture centred behind it at the
       height the keypad leaves, which is then at least as tall as it would
       be side by side. The deck wraps, so each part taking the full width
       is what stacks them. */
    @container deck (max-aspect-ratio: 3 / 4) {
      .portrait {
        width: 100%;
        height: calc(100cqh - 13.8rem);
        aspect-ratio: auto;
      }
      .print {
        inset: 0 auto 0 50%;
        height: 100%;
        aspect-ratio: 3 / 4;
        transform: translateX(-50%);
      }
      .pad {
        flex: none;
        width: 100%;
        max-width: none;
        height: 13rem;
      }
      .key .cap {
        width: 2.9rem;
        height: 2.9rem;
      }
      .key .legend {
        font-size: 0.62rem;
      }
    }
    /* Nothing reads out at this width (readout() checks PHONE_QUERY), so
       the glass can fit the name instead of reserving room for a readout
       without ever resizing the unit. */
    .glass {
      height: auto;
      min-height: 6.4rem;
    }
    .glass .big {
      white-space: normal;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      line-clamp: 2;
    }
    .glass .small {
      white-space: normal;
      font-size: 0.92rem;
    }
  }
</style>
