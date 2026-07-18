# Design: /ember public pages (scale-to-zero Postgres demo goes public)

Date: 2026-07-18
Status: Approved (route family, live-driven stage, health alert, cached GB·h
counter confirmed by Joe; /ember landing page deliberately deferred to Joe)

## Goal

Put the scale-to-zero Postgres demo on the public site with the visual polish
of the /app/firecracker explainer, behind Cloudflare Turnstile, without
compromising the private tier's isolation or the demo's sleep story.

## Route family

- `jomcgi.dev/ember/postgres`: the new live demo page (this project).
- `jomcgi.dev/ember/firecracker`: the existing scroll explainer, moved from
  `/app/firecracker`; a permanent redirect stays at the old path (it is linked
  from the homepage and engineering pages).
- `jomcgi.dev/ember`: intentionally NOT built now. Joe will design the landing
  page later. No route ships in this project.

## The live ember stage

The /ember/postgres hero reuses the fcstory visual language (the hot/cold RAM
cell grid with the ragged sweep edge, rAF-imperative writes outside Svelte
reactivity, palette via CSS custom properties) but driven by the LIVE
lifecycle poll instead of scroll position:

- banked: cells cold blue; the all-time GB·h counter overlays as the hero stat.
- waking (relighting / cold_booting): a warming sweep crosses the grid.
- serving: hot ember tones with a subtle flicker.
- banking: a slow cooling sweep.

Reduced motion: static state swap, no sweeps. The demo console (the private
panel's two-column layout, adapted) sits below the stage, followed by two or
three short explainer sections in the fcstory voice (what banking is, why data
survives, what the connect number means).

## Public-tier security plumbing

- **Module split.** The demo-postgres endpoints move to a public-safe module
  registered through the FastMonolith public registry (`register_public`):
  status, query, session. `reset` (force cold boot) stays private-only:
  publicly it is a griefing vector (repeated 30-60 s cold-boot purgatory).
  `main_public_imports_test` and the public BUILD glob are respected by the
  split.
- **No /api on the public origin.** The page fetches via same-origin SvelteKit
  `+server.js` proxy routes (public-tier checklist rule 2).
- **Turnstile.** `POST .../session` requires a verified Turnstile token when
  `TURNSTILE_SECRET_KEY` is set (it will be set on monolith-public; unset on
  the private tier). The widget renders on the public page; the site key
  arrives via public env. A Cloudflare siteverify outage returns a clean 403,
  not a 500.
- **Insert gating.** Public inserts require a session cookie and are limited
  to 1 insert per 5 s per session (in-process token bucket keyed by session
  tag). Aggregate/select stays session-optional.
- **Global concurrency semaphore.** At most 4 concurrent demo roundtrips
  (psycopg connects); excess returns an in-band "busy, one moment" that the
  frontend's existing exponential backoff absorbs invisibly.
- **Status cache.** The control-plane status read is cached server-side for
  500 ms so N visitors polling at 700 ms cost one upstream read per window.
- **Savings accrual on both tiers.** Public status polls must run the
  generation-validated accrual (otherwise public-only activity bumps
  generations unseen and the conservative credit rule discards real sleep).
  `demo_pg_savings` gets SELECT/INSERT/UPDATE granted to the public writer
  role per the ADR security/005 precedent (single table, narrowest grant).
- **Secrets/values.** `DEMO_POSTGRES_DSN`, `EMBERVM_URL`,
  `TURNSTILE_SECRET_KEY` (and the public site key) wired into monolith-public
  via the 1Password operator and deploy values. No secrets in Git.
- **No pooling.** Connections stay short-lived per roundtrip: idle reuse pins
  the VM awake and hides the connect-time exhibit (decided 2026-07-18).

## Cached GB·h counter

The all-time savings counter lives in the monolith's own Postgres, so reading
it can never wake the demo VM. A small public endpoint serves
`{total_saved_mib_s, as_of}` from a 30 s in-process cache (politeness to our
own DB, not a wake concern). The demo page itself gets the value for free in
the status payload; the cached endpoint exists for the future /ember landing
page and any homepage stat. Frontend renders GB·h with humanize() K/M/B.

## Health + alerting

The public `/health` deep check gains a `demo_postgres` component sourced from
the cached control-plane status read (never a DB connect: health checks must
not wake the VM, and asleep is healthy). Unhealthy when any of:

1. The control plane is unreachable or the demo is unconfigured.
2. The workload reports a broken snapshot/volume pairing while banked.
3. **Stuck transition**: the cached status observer tracks when the state last
   changed; a transitional state (relighting, cold_booting, starting, banking)
   persisting beyond 90 s (the workload's wakeTimeoutSeconds is 60 s plus
   margin) marks the component unhealthy. This catches a wedged cold boot
   purely from passive observation.
4. **Slow or failed wakes from real traffic**: the query path records its most
   recent wake outcome (classification, connect_ms, error, timestamp)
   in-process. If the latest attempt within the past 10 minutes failed or its
   connect exceeded 60 s, the component is unhealthy until a newer successful
   wake supersedes it. No traffic means no signal here, which is correct: an
   idle sleeping demo is healthy, and case 3 covers wedges that happen outside
   query traffic.

The existing httpcheck uptime alert on `/health` then covers the demo
(add-httpcheck-alert skill if a new check is needed). Deliberately NOT built:
a synthetic canary query on a schedule. It would give proactive wake-proof at
the cost of waking the VM every interval, gutting the sleep story and the
savings counter; passive detection from real visitors plus stuck-state
observation is the right trade for a demo. Revisit only if the alert proves
too quiet in practice.

## Sequencing

- **PR 1 (backend, tier-neutral).** Module split + register_public surface,
  Turnstile enforcement path, per-session insert bucket, global semaphore,
  status cache, savings endpoint + grant migration, /health component, values
  wiring for monolith-public. Mergeable and verifiable before any public page
  exists.
- **PR 2 (frontend).** /ember/postgres page (ember stage + adapted console +
  explainer sections + Turnstile widget), /ember/firecracker move + redirect,
  visual-regression targets + fixtures for the new public pages.

Verification per the public-tier checklist: live curl of
`https://jomcgi.dev/ember/postgres` (200), `/health` component present, a real
Turnstile-gated session + insert from a browser, and confirmation that idle
public visitors leave the VM asleep.
