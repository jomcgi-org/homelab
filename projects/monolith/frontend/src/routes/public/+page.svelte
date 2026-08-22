<script>
  import { onMount } from "svelte";
  import { Sticker, Marquee, Footer, Seo } from "$lib/public/components";
  import { LOCATION } from "$lib/public/seo.js";
  import HomepageRack from "./HomepageRack.svelte";

  let { data } = $props();

  /** Build marquee items from /stats data, skipping any item whose source
   *  is unavailable so the ticker never shows fabricated numbers. */
  function buildMarquee(stats) {
    const items = ["~/homelab"];
    const c = stats?.cluster;
    const g = stats?.gpu;
    const k = stats?.knowledge;
    const d = stats?.deploy;

    if (c?.nodes != null && c?.pods != null)
      items.push(`${c.nodes} nodes · ${c.pods} pods`);
    if (c?.cpu_used_cores != null && c?.cpu_capacity_cores != null) {
      items.push(`cpu: ${c.cpu_used_cores} / ${c.cpu_capacity_cores} cores`);
    }
    if (c?.memory_used_gb != null && c?.memory_capacity_gb != null) {
      items.push(`mem: ${c.memory_used_gb} / ${c.memory_capacity_gb} gb`);
    }
    if (g?.utilization_pct != null) {
      const memPart =
        g?.memory_used_gb != null && g?.memory_total_gb != null
          ? ` · ${g.memory_used_gb} / ${g.memory_total_gb} gb`
          : "";
      items.push(`gpu: ${g.utilization_pct}%${memPart}`);
    }
    if (c?.argocd_apps != null) items.push(`argocd: ${c.argocd_apps} apps`);
    if (k?.facts != null) items.push(`kg: ${k.facts.toLocaleString()} notes`);
    if (d?.latest_commit_sha) items.push(`last commit: ${d.latest_commit_sha}`);
    if (d?.deployed_at) {
      const ago = formatAgo(d.deployed_at);
      if (ago) items.push(`deployed ${ago} ago`);
    }
    return items;
  }

  function formatAgo(iso) {
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return null;
    const minutes = Math.max(0, Math.round((Date.now() - then) / 60_000));
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
  }

  let MARQUEE_ITEMS = $state(buildMarquee(data.stats));

  /* ── Scroll-triggered reveals + age tick ─────────────── */
  onMount(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            observer.unobserve(e.target);
          }
        }
      },
      { threshold: 0.15 },
    );
    for (const el of document.querySelectorAll(".reveal")) {
      observer.observe(el);
    }

    // Re-render the marquee on a fixed cadence so "deployed Xm ago" advances
    // as wall-clock time passes. 30s catches every minute boundary within
    // ~30s; below 60min the displayed value is in minutes, above it's hours.
    const tick = setInterval(() => {
      MARQUEE_ITEMS = buildMarquee(data.stats);
    }, 30_000);

    return () => {
      observer.disconnect();
      clearInterval(tick);
    };
  });
</script>

<Seo
  title="Joe McGinley · Senior Platform Engineer"
  description="Joe McGinley's homelab: a four-node Kubernetes cluster running agents, maps, and a knowledge graph."
  path="/"
/>

<!-- Nav is rendered by the root +layout.svelte (shared 1:1 across tiers). -->

<!-- ═══ Top marquee ═══ -->
<Marquee items={MARQUEE_ITEMS} />

<div class="above-fold">
  <!-- ═══ Hero ═══ -->
  <section class="hero">
    <!-- decorative shapes -->
    <svg class="deco deco-diamond-1" width="28" height="28" viewBox="0 0 24 24"
      ><path
        d="M12,2 L22,12 L12,22 L2,12 Z"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >
    <svg class="deco deco-circle-1" width="18" height="18" viewBox="0 0 24 24"
      ><circle
        cx="12"
        cy="12"
        r="10"
        fill="var(--coral)"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >
    <svg class="deco deco-star" width="56" height="56" viewBox="0 0 40 40"
      ><path
        d="M20,2 L22.5,14 L34,10 L26,20 L34,30 L22.5,26 L20,38 L17.5,26 L6,30 L14,20 L6,10 L17.5,14 Z"
        fill="var(--blue)"
        stroke="var(--ink)"
        stroke-width="2"
        stroke-linejoin="round"
      /></svg
    >
    <svg class="deco deco-cloud" width="120" height="60" viewBox="0 0 120 60"
      ><path
        d="M10,45 Q 8,30 22,28 Q 26,14 42,18 Q 54,10 66,20 Q 82,16 88,30 Q 104,28 108,44 Q 108,52 98,52 L 20,52 Q 10,52 10,45 Z"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2.5"
        stroke-linejoin="round"
      /></svg
    >
    <svg class="deco deco-squiggle" width="80" height="24" viewBox="0 0 80 24"
      ><path
        d="M2,12 Q 10,2 18,12 T 34,12 T 50,12 T 66,12 T 78,12"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2.5"
        stroke-linecap="round"
      /></svg
    >
    <svg class="deco deco-diamond-2" width="18" height="18" viewBox="0 0 24 24"
      ><path
        d="M12,2 L22,12 L12,22 L2,12 Z"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >

    <div class="wrap hero-content">
      <h1 class="hero-headline">
        ten years ago i was underwriting policies and winning insurance
        hackathons. now i'm building <span class="hl hl-yellow"
          >production grade infra</span
        >
        for weekend side quests and keeping
        <a href="https://semgrep.dev" class="hero-mono">semgrep</a> online.
      </h1>
      <div class="hero-cta-row">
        <a href="#homelab" class="btn btn-primary"
          >SEE MY HOMELAB <span class="btn-arr">→</span></a
        >
        <a href="/app/notes" class="btn btn-secondary">TALK TO MY NOTES</a>
      </div>
      <Sticker color="var(--coral)" rotate={-5} class="sticker-hero"
        >← BUILT THIS SITE TOO</Sticker
      >
    </div>
  </section>

  <!-- ═══ Bio panel (yellow) ═══ -->
  <section class="bio-panel reveal">
    <!-- decorative shapes -->
    <svg
      class="deco deco-bio-squiggle"
      width="80"
      height="24"
      viewBox="0 0 80 24"
      ><path
        d="M2,12 Q 10,2 18,12 T 34,12 T 50,12 T 66,12 T 78,12"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2.5"
        stroke-linecap="round"
      /></svg
    >
    <svg
      class="deco deco-bio-diamond"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      ><path
        d="M12,2 L22,12 L12,22 L2,12 Z"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >
    <svg class="deco deco-bio-circle" width="24" height="24" viewBox="0 0 24 24"
      ><circle
        cx="12"
        cy="12"
        r="10"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >
    <div class="wrap bio-content">
      <div class="bio-left">
        <Sticker color="var(--paper)" rotate={-3} class="sticker-bio"
          >A LITTLE ABOUT ME</Sticker
        >
        <p class="bio-sub">
          I'm Joe, in {LOCATION.short}.<br />
          Monorepo enthusiast. Care<em>mad</em> about developer experience.
        </p>
      </div>
    </div>
  </section>
</div>

<!-- ═══ Project stack ═══ -->
<HomepageRack {stats} />

<!-- ═══ Footer ═══ -->
<Footer />

<style>
  /* ── Above-fold wrapper ─────────────────── */
  .above-fold {
    display: flex;
    flex-direction: column;
  }

  /* ── Hero ─────────────────────────────────── */
  .hero {
    background: var(--cream);
    padding: 88px 0 96px;
    position: relative;
    overflow: hidden;
    border-bottom: 2px solid var(--ink);
  }

  .hero-content {
    max-width: 1360px;
    text-align: left;
    position: relative;
  }

  .hero-headline {
    font-family: var(--mono);
    font-weight: 700;
    font-size: clamp(20px, 2.6vw, 38px);
    line-height: 1.4;
    letter-spacing: -0.01em;
    margin: 0 0 44px;
    max-width: 46ch;
    text-wrap: balance;
    color: var(--ink);
    /* .hl's ::before sits at z-index -1 — isolate so it stays above the
       section background instead of vanishing behind it. */
    isolation: isolate;
  }

  .hero-mono {
    text-decoration-line: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 4px;
    transition: text-decoration-color 160ms ease;
  }

  .hero-mono:hover {
    text-decoration-color: var(--coral);
  }

  .hero-cta-row {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }

  .btn-arr {
    margin-left: 6px;
  }

  :global(.sticker-hero) {
    position: absolute;
    right: 8%;
    bottom: -4px;
    font-size: 11px;
  }

  /* ── Decorative shapes ───────────────────── */
  .deco {
    position: absolute;
    pointer-events: none;
  }

  /* Decos live in the gutter outside .hero-content's 1360px max-width.
     `max(Xpx, calc(50% - 720px - offset))` keeps them ≥X from the
     viewport edge but never inside the content box. */
  .deco-diamond-1 {
    width: 28px;
    height: 28px;
    top: 56px;
    left: max(16px, calc(50% - 720px));
  }

  .deco-circle-1 {
    width: 18px;
    height: 18px;
    top: 140px;
    left: max(8px, calc(50% - 740px));
  }

  .deco-star {
    width: 56px;
    height: 56px;
    top: 60px;
    right: max(16px, calc(50% - 720px));
    transform: rotate(-12deg);
  }

  .deco-cloud {
    width: 120px;
    height: 60px;
    bottom: 40px;
    left: max(8px, calc(50% - 760px));
    opacity: 0.9;
  }

  .deco-squiggle {
    width: 80px;
    height: 24px;
    bottom: 80px;
    right: max(24px, calc(50% - 730px));
  }

  .deco-diamond-2 {
    width: 18px;
    height: 18px;
    bottom: 120px;
    right: max(8px, calc(50% - 740px));
  }

  .deco-bio-squiggle {
    width: 80px;
    height: 24px;
    top: 32px;
    right: max(16px, calc(50% - 720px));
  }

  .deco-bio-diamond {
    width: 20px;
    height: 20px;
    top: 60px;
    left: max(16px, calc(50% - 720px));
  }

  .deco-bio-circle {
    width: 24px;
    height: 24px;
    bottom: 50px;
    left: max(8px, calc(50% - 740px));
  }

  /* ── Bio panel ───────────────────────────── */
  .bio-panel {
    background: var(--accent);
    border-bottom: 2px solid var(--ink);
    padding: 40px 0;
    position: relative;
    overflow: hidden;
  }

  .bio-content {
    max-width: 1360px;
  }

  .bio-sub {
    font-family: var(--sans);
    font-size: clamp(20px, 1.8vw, 26px);
    line-height: 1.5;
    color: var(--ink-2);
  }

  .bio-left {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 22px;
  }

  /* ── Responsive ──────────────────────────── */
  @media (max-width: 768px) {
    .hero {
      padding: 56px 0 64px;
    }
    .bio-panel {
      padding: 32px 0;
    }
    :global(.sticker-hero) {
      display: none !important;
    }
    /* Keep one signature shape per band on mobile so the shape language
       survives small screens; the rest would collide with text. */
    .deco {
      display: none;
    }
    .deco-star {
      display: block;
      width: 34px;
      height: 34px;
      top: 10px;
      right: 12px;
    }
    .deco-bio-squiggle {
      display: block;
      width: 56px;
      top: 14px;
      right: 14px;
    }
  }
</style>
