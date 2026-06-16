# Public Chat (V3) — Implementation Plan

**Date:** 2026-06-16
**Status:** Draft, pending review
**Author:** Joe + Claude
**ADR:** [security/005 — Public Chat Adversarial Hardening](../decisions/security/005-public-chat-adversarial-hardening.md)
**Predecessor:** V1/V2 of `docs/plans/2026-05-07-public-notes-visibility-design.md`

## Goal

Ship the public notes app whose landing is a neo-brutalist chat box backed by the in-cluster Qwen model, with a graph-overlay deep dive that highlights the public notes a conversation touched and expands their content. The endpoint is anonymous, internet-facing, and adversarial: this plan is mostly about the controls that keep it from starving the shared GPU, leaking private notes, or becoming an abuse amplifier. ADR 005 is the rationale; this plan is the build.

## Dependencies and sequencing

This rides on the public/private split. Two cases:

- **If ADR 004 / ADR 010 have landed:** `chat_public` is a new PUBLIC-tier module composed into the existing public binary, and the public SvelteKit SSR app is the front door. Preferred.
- **If they have not:** the chat still ships in the public binary, but the plan must not regress the isolation ADR 004 specifies. Do not co-locate public chat in the private monolith "for now"; that bakes in the exact co-residency ADR 004 removes. If the split is not ready, land a minimal public binary first.

Confirm this before Phase 1. Everything below assumes the chat is a PUBLIC-tier module with the `public_reader` role, a dedicated `chat_public` write role, and a default-deny egress policy already in place.

## Locked-in decisions (from ADR 005)

| Decision | Choice |
| -------- | ------ |
| Front door | SvelteKit SSR is the only public origin; the chat API is internal (ClusterIP, Linkerd mTLS), never on the public HTTPRoute |
| GPU sharing | Reserved-headroom semaphore on the shared `inference` endpoint; trusted slots protected |
| Abuse defense | SSR front door + Cloudflare Turnstile; opaque `httpOnly` session cookie over a server-side row; per-session + per-IP + global budgets |
| State | Server-side sessions; server-authoritative max turns / char cap; compaction via rolling summary |
| Data scope | Note retrieval over the public view via read-only `public_reader`; session/transcript writes via a dedicated `chat_public` role on the primary |
| Retention | Persist transcripts + pseudonymous user/session details in Postgres `chat_public`; TTL + on-demand takedown; SigNoz metrics-only (no chat content) |
| Model authority | No tools, fixed server-side prompt, text in / text out |

## Parameters to set in Phase 0 (then assert in tests)

These are the tuning knobs ADR 005 deliberately left open. Pick starting values, put them in `values.yaml`, and grep the test tree when changing them (per CLAUDE.md):

| Knob | Starting point (tune at load test) |
| ---- | ---------------------------------- |
| Per-message character cap | generous, e.g. 8000 chars |
| Max turns per session | e.g. 20 |
| Max output tokens per turn | e.g. 1024 |
| Max total tokens per session | e.g. 32000 (one context worth) |
| Compaction trigger | when live context exceeds ~70% of the model window |
| Public semaphore size | `max_num_seqs` minus reserved trusted headroom (today `max_num_seqs`=3, so start at 1, validate) |
| Per-IP session mint rate | e.g. 5 / hour |
| Global concurrent public inferences | e.g. equal to the semaphore; the circuit breaker trips above it |
| Session / cookie TTL | short, e.g. 30 min |

## Phases

### Phase 0 — Threat-model sign-off and parameters

- Confirm the dependency case above (split ready or not).
- Lock the parameter table starting values into a `chat_public` block in `projects/monolith/deploy/values.yaml` (or the public binary's values).
- Provision the Turnstile site (Cloudflare dashboard) and the Turnstile secret as a `OnePasswordItem` consumed by the FastAPI public binary (not the SSR app). Site key is public; secret key is server-side only.
- No code. Output: agreed numbers + secret wired.

### Phase 1 — `chat_public` module skeleton, SSR front door, server-side sessions

Stand up the module with no GPU yet, so the session and limit machinery can be built and tested in isolation. The chat API is internal-only from day one; SvelteKit SSR is the public front door.

- New module `projects/monolith/chat_public/` (PUBLIC tier per ADR 010): `module.py` (the `Module` descriptor, `tier=PUBLIC`, `schema="chat_public"`), `router.py`, `models.py`, `sessions.py`, `limits.py`. Routes mount under an internal prefix (e.g. `/internal/chat/*`) and are **not** added to the public HTTPRoute.
- SSR BFF routes in the public SvelteKit app under `routes/public/` (`+server.ts`): a session-create route and a message route that proxy to the internal chat API over in-cluster service DNS, set/read the opaque `httpOnly` `Secure` `SameSite` session cookie, and stream the SSE response straight through unbuffered. The browser only ever talks to SSR.
- Migration `chart/migrations/<ts>_chat_public.sql`: `chat_public` schema; `sessions` table holding the pseudonymous user/session details (id, created_at, last_seen_at, turn_count, total_tokens, ip_hash, turnstile_outcome, country from `CF-IPCountry`, user_agent_hash, status); `messages` table holding the transcript (session_id, role, content, tokens, created_at). No raw IP or PII is stored, only hashes and coarse geo. Grants: a dedicated `chat_public` role with DML on the `chat_public` schema only, writing to the **primary** (this is the first public-tier write path); `public_reader` stays read-only and is the note-retrieval path, never a writer.
- Session create: verify a Turnstile token (stubbed-accept in Phase 1, real in Phase 2), open a session row, return an opaque session id the SSR route stores in the cookie. No signed-token scheme; the row is the authority.
- Message: accept the cookie's session id + a single user message; enforce char cap, max turns, and per-session token ceiling server-side; for now echo a canned response over SSE (reuse `chat/sse.py` transport).
- `limits.py` is the single home for every budget check; no scattered `if len(x) > ...` anywhere else (mirror the `visibility.py` discipline from V1).
- Chart version bump (`Chart.yaml` + `deploy/application.yaml` `targetRevision`).

Tests (land in the phase PR): session create/expire; char-cap rejection; max-turns rejection; per-session token ceiling; SSE shape; the chat API is not on the public HTTPRoute (only SSR is); the SSR proxy sets and forwards the cookie; replayed/edited client history ignored (server is authoritative).

### Phase 2 — Turnstile admission, IP forwarding, budgets

- Turnstile widget rendered into the neo-brutalist SSR landing (public site key) as the "start chatting" gate.
- The SSR session-create route forwards the user's Turnstile token and the Cloudflare `CF-Connecting-IP` to the internal chat API. Siteverify runs **in the FastAPI binary** (the 1Password Turnstile secret lives there, not in SSR). No valid token, no session.
- The chat API trusts the forwarded `CF-Connecting-IP` only on connections bearing SSR's Linkerd identity; it keys the per-IP session-mint limiter (backend counter on hashed IP). Confirm the Envoy local rate limit covers the SSR public route (extend `cfIngress.public` if path-scoped).
- Global circuit breaker: a Postgres-backed counter of in-flight public inferences; over the ceiling, the message path sheds with a friendly 429/503 and a "busy" SSE event proxied through SSR.

Tests: siteverify failure rejects; per-IP mint cap; forwarded IP honored only from SSR identity and ignored from any other peer; global ceiling sheds load with the right status; IP and Turnstile values stored hashed, never raw.

### Phase 3 — Inference wiring, reserved-headroom semaphore, compaction

- Reserved-headroom semaphore in `chat_public` (asyncio.Semaphore sized from `values.yaml`); every call to vLLM acquires it; saturation queues briefly then sheds. This is the GPU isolation control.
- Call the shared vLLM (`inference.inference.svc.cluster.local:8080`, OpenAI-compatible `/v1/chat/completions`, streaming) with the fixed server-side system prompt and the server-held conversation. No tools, no function-calling.
- **NetworkPolicy:** the chat API is a ClusterIP, not on the public HTTPRoute. Allow SSR egress to that ClusterIP only; allow the chat binary egress to the `inference` service port (and `inference-embeddings` if Phase 4 needs live embeddings) and to the Postgres primary `-rw` for the `chat_public` schema writes. Everything else stays default-deny (ADR 004); review these as the only widenings.
- Compaction in `sessions.py`: when live context exceeds the configured fraction of the window, summarize older turns into a rolling summary (reuse `chat/summarizer.py`), store it on the session, and send only summary + recent turns to vLLM.
- Wire real token accounting back into the per-session and global budgets from the model's usage.

Tests: semaphore caps concurrency (N+1th public call queues/sheds, a trusted call is unaffected in a simulated shared-load test); compaction triggers at threshold and keeps context bounded; token usage updates budgets; system prompt is server-fixed and not overridable by user input.

### Phase 4 — Retrieval, graph overlay, neo-brutalist frontend

- Retrieval over the public knowledge graph only: reuse the V1 public graph/notes filter (`COALESCE(visibility,'private')='public'`) via `public_reader`. Decide embeddings: precompute and cache public-note embeddings (preferred, retrieval becomes a vector read, no request-time GPU) vs live under the semaphore. Default to precompute given the small, slow-changing public set.
- Emit node-touched events over SSE (reuse the `node_discovered` event from the private `/explore` path), proxied through the SSR route, so the overlay highlights retrieved public nodes live.
- Frontend under `routes/public/`: neo-brutalist chat landing; graph overlay deep-dive that highlights touched public nodes and expands their content. Node bodies come from the existing browser-direct V2 public notes API (cheap, cacheable, no session); only the chat turn goes through the SSR proxy. Render model output and note bodies as **sanitized markdown under a strict CSP**, never raw HTML.

Tests: retrieval never returns a private node (assert a seeded private note is not retrievable as `public_reader`); touched-node set equals retrieved public nodes; renderer strips/escapes injected HTML/script in model output and note bodies; overlay highlights match the SSE node events.

### Phase 5 — Retention, purge, abuse runbook

- Scheduled retention/TTL job (framework scheduler, private-tier) honoring the configured policy; on-demand purge by session id or IP-hash for takedown (a cheap Postgres `DELETE`).
- Postgres is the authoritative record for transcripts and user details. Telemetry to SigNoz stays metrics-only (token counts, latencies, rejections); no chat content or user details are emitted to traces or logs.
- A `docs/` retention/abuse runbook: what is stored, for how long, how to purge, how to handle a takedown or abuse report. Plus the public-facing notice wording.
- Access to the `chat_public` transcript store is private-tier only (no public route, and no SSR route, reads transcripts back).

Tests: TTL job removes rows past policy and nothing newer; on-demand purge by session and by IP-hash; no public or SSR endpoint can read transcripts; assert no span/log attribute carries message content.

### Phase 6 — Load test, tuning, observability

- Dashboards/alerts (SigNoz): public queue depth, semaphore saturation, **trusted-caller inference latency** (the canary that the reservation is working), budget-rejection rates, global-ceiling trips, Turnstile solve-to-abuse ratio, SSR-proxy SSE latency.
- Load test the reservation: drive a public flood and assert Discord/private/agent inference latency stays within SLO. Confirm SSE streams token-by-token through the SSR proxy (not buffered). Tune the semaphore and budgets from the result.
- Only after the load test confirms trusted workloads are protected do we open the endpoint publicly.

## Security checklist (per ADR 005, verify before each merge)

- [ ] `chat_public` is PUBLIC-tier; no private module or secret enters the public binary (the ADR 010 `bazel cquery` test still passes).
- [ ] The chat API is not on the public HTTPRoute; SSR is the sole public origin for chat.
- [ ] New egress is limited to SSR-to-internal-chat-API, chat-binary-to-inference, and chat-binary-to-Postgres-primary for the `chat_public` schema; default-deny otherwise.
- [ ] Forwarded `CF-Connecting-IP` is trusted only from SSR's Linkerd identity.
- [ ] Retrieval is `public_reader` + public view; a private note is provably unretrievable; `public_reader` stays read-only (writes use the dedicated `chat_public` role).
- [ ] The model has no tools; the system prompt is server-fixed and not user-overridable.
- [ ] Model output and note bodies render as sanitized markdown under a strict CSP.
- [ ] Turnstile siteverify runs in the FastAPI binary (secret an `OnePasswordItem`, not in SSR); IP and Turnstile correlates stored hashed.
- [ ] Chat content and user details are stored only in Postgres; telemetry to SigNoz carries no message content.
- [ ] Purge tooling ships with the feature, not after.
- [ ] Containers stay non-root, read-only root filesystem (ADR 004 §Security), resource-limited.

## Operational notes

- **No local test loop** (CLAUDE.md): implement per phase, commit with Conventional Commits, push, watch CI via `gh pr checks <n> --watch`, iterate from BuildBuddy logs.
- **Chart version bumps:** any phase touching `chart/migrations/` bumps `Chart.yaml` + `deploy/application.yaml` `targetRevision` together.
- **RBAC:** the chat path reads/writes only its own Postgres schema; no cluster-resource reads, so no ClusterRole changes expected. Re-verify if a phase adds a cluster read.
- **Revert path:** each phase is its own PR; reverting drops the migration and routes cleanly. The endpoint is not opened publicly until Phase 6.

## Out of scope

- A dedicated public model (ADR 005 alternative; revisit only on measured contention).
- Model-based content moderation (ADR 005 open question; reconsider from real transcripts).
- Tool-use / function-calling in public chat (explicit non-goal; keeps blast radius low).
- Authenticated/private chat changes (the existing `/explore` and Discord paths are untouched).
