# ADR 005: Public Chat Adversarial Hardening

**Author:** Joe McGinley
**Status:** Implemented
**Created:** 2026-06-16
**Relates to:** [ADR 004: Public Read-Only Service Isolation](004-public-read-only-service-isolation.md), [ADR 010: FastMonolith Modular Framework](../services/010-fastmonolith-modular-framework.md), [ADR 002: Path-Based Ingress Tiers](../../../projects/platform/ARCHITECTURE.md)

---

## Problem

The public surface is gaining a chat: the landing page of the public notes app is a neo-brutalist chat box, and a user can deep-dive into the knowledge graph as an overlay that highlights the public nodes a conversation touched and expands their content. The chat is backed by the in-cluster Qwen model (vLLM, OpenAI-compatible at `inference.inference.svc.cluster.local:8080`). This is V3 of the public-notes design work, which deferred it precisely because it changes the risk profile.

A public, anonymous, internet-facing endpoint that spends GPU on demand is a qualitatively different surface from the read-only JSON endpoints ADR 004 isolated. Every other public route is a cheap point read against a replica. Chat is expensive, stateful, generative, and adversarial by default. The specific threats:

1. **GPU exhaustion (the headline availability threat).** The Qwen vLLM runs on a single 24GB GPU with `max_num_seqs` of 3, and it is _shared_: the Discord bot, the private `/explore` chat, and the agent platform all call the same endpoint. An anonymous flood of long, max-token public requests can saturate the decode slots and starve those trusted workloads. The public surface must never be able to degrade the private one.
2. **Compute and token amplification.** Short of full denial of service, an attacker can burn GPU-seconds: maximal prompts, forced max-output-tokens, many turns, many parallel sessions. The requested "generous character limit and max turns" multiplies the per-conversation cost ceiling, so it has to be bounded explicitly rather than left open.
3. **Data exfiltration through retrieval.** The chat grounds on the knowledge graph. If retrieval can reach private notes, it leaks the exact PII (colleagues, employers, job search, personal life) that the visibility work exists to protect. Confinement to the public subset must be a database property, not a prompt instruction.
4. **Prompt injection and jailbreak.** A user will try to override the system prompt, extract it, impersonate, or coax reputationally or legally damaging output attributable to the site. We cannot prevent a determined user from making a text model say off-brand things; we can bound the blast radius so that saying them achieves nothing privileged.
5. **Automation and identity-rotation abuse.** Per-IP limits alone are weak: carrier-grade NAT shares one IP across many users, and proxies/residential pools rotate IPs faster than any per-IP budget. The endpoint needs an identity that is costlier to forge than an IP.
6. **A new egress path out of the isolated public service.** ADR 004 gives the public binary a default-deny egress NetworkPolicy (Postgres read replica plus DNS only). Chat punches a hole to the inference service. That hole must be exactly one destination, or an RCE in the public path regains cluster reach.
7. **Untrusted generative output rendered in a rich overlay.** The overlay renders model output and note bodies. Model output is untrusted; rendered as raw HTML it is stored/reflected XSS.
8. **A growing store of anonymous user-submitted content.** Persisting transcripts (the chosen retention posture) accumulates content we did not author, including potential PII and abusive material, which carries privacy and legal-takedown obligations.

The decision is _not_ whether to expose chat (V3 is committed) but the control stack that makes an anonymous, GPU-backed, RAG-grounded endpoint safe to operate.

---

## Decision

Ship public chat as a new **PUBLIC-tier domain module** (`chat_public`) composed into the public binary per [ADR 010](../services/010-fastmonolith-modular-framework.md), distinct from the existing private `chat` domain, behind the public ingress tier ([ADR 002](../../../projects/platform/ARCHITECTURE.md)). The public SvelteKit SSR app is the only internet-facing origin for chat: the `chat_public` API is **not** on the public HTTPRoute and is reachable only in-cluster from the SSR app over Linkerd mTLS. The browser talks only to SSR, which gates on Turnstile, holds the session cookie, and proxies the turn to the internal API. This diverges deliberately from the cheap, cacheable public read endpoints (`/api/knowledge/public/*`), which stay browser-direct: the expensive, generative, adversarial endpoint goes behind the SSR front door so the challenge and the only public handler sit at one origin. Wrap it in a defense-in-depth stack where every limit is enforced by a mechanism the client cannot edit, and where the failure of any single control still bounds the damage. Eight layers, each owning one threat:

**1. Admission: the SSR front door plus a Turnstile-bound session.** A Cloudflare Turnstile challenge on the SSR-served landing gates session creation. The SSR route forwards the Turnstile token to the internal session-create endpoint, which verifies it (the Turnstile secret lives in the FastAPI binary, not in SSR) and opens a server-side session row; the SSR app sets an opaque, `httpOnly`, `Secure`, `SameSite` session cookie. The cookie carries no authority of its own: it is an opaque id whose authority is the row, so no signed-token scheme is needed and a stolen cookie still cannot exceed the row's budget. No verified session, no inference. The session row, not the IP, is the unit every budget is charged against. Routing chat through SSR complements Turnstile: the challenge and the only public handler sit at the same single origin, so there is no directly-exposed chat API to hit around the challenge. This raises the cost of automation above the cost of an IP and gives slow-drip and NAT-shared abuse a stable handle. The neo-brutalist landing styles the challenge as a deliberate "start chatting" gate.

**2. Budgets: per-session, per-IP, and a global ceiling.** Three nested limits. Per-session: max turns, a generous per-message character cap, a max output-tokens-per-turn, and a max total-tokens-per-session. Per-IP: Envoy local rate limiting (existing) plus a backend counter, so one IP cannot mint sessions without bound. Global: a cluster-wide token/concurrency budget for the entire public chat surface, a circuit breaker so that even a coordinated swarm of solved challenges cannot exceed an aggregate ceiling. The global budget is the backstop that holds when layers 1 and 2 are partially defeated.

**3. GPU isolation: a reserved-headroom semaphore.** Public chat acquires a slot from a bounded concurrency semaphore sized _below_ the vLLM batch capacity, deliberately reserving decode slots for the Discord bot, private chat, and the agent platform. Sizing rule: `public_concurrency + reserved_trusted_headroom <= max_num_seqs`. When the public semaphore is full, public requests queue briefly (bounded) and then shed load with a friendly "busy" state, and they never block a trusted caller. This is a client-side admission control in front of vLLM's own scheduler, defense in depth: even if the public surface is flooded, trusted workloads keep their reserved slots. A dedicated public model was considered and rejected (see Alternatives); the semaphore is simpler and sufficient until contention is observed.

**4. Server-side conversation state and compaction.** The session row holds the conversation; the browser sends only its session cookie and the new message (through the SSR proxy), never history. The server enforces max turns and the character cap authoritatively, and runs compaction: when the running context approaches a token budget, older turns are summarized into a rolling summary (reusing the existing `chat/summarizer.py` pattern) so the live context stays bounded turn over turn. Compaction is what keeps per-request GPU cost flat as a conversation grows, and server-authoritative history is what makes the turn and length limits un-bypassable. A client that forges or replays history achieves nothing because the server ignores client-supplied history entirely.

Sessions, transcripts, and the pseudonymous user/session details are the first public-tier _writes_: they go to the module's own `chat_public` schema on the Postgres primary through the dedicated `public_writer` role with DML on that one schema, distinct from ADR 004's read-only `public_reader`, which remains the path for note retrieval against the public view. ADR 004's pure-reader assumption still holds for every other public surface; chat narrows the exception to a single schema it alone owns, so a compromise of the chat path can write its own sessions and read public notes, and nothing more.

**5. Retrieval confined to the public subset by the database.** Grounding retrieval runs only over the public knowledge graph, the same `COALESCE(visibility,'private')='public'` predicate the public read endpoints use, enforced by the `public_reader` role and public views from ADR 004 / ADR 010, never by a sentence in the system prompt. The overlay's "nodes your chat touched" set _is_ the set of public nodes retrieved. Private notes are physically unreadable by the public binary's role, so a perfect jailbreak still cannot surface one. Retrieved note text is injected as clearly delimited data, never as instructions, and the model has no tools, so retrieved content cannot act.

**6. Output posture: low blast radius, not prompt-based defense.** The system prompt is fixed server-side, the model is text-in/text-out with no tools and no function-calling, and it can reach nothing private. We explicitly do **not** treat prompt instructions as a security boundary. Jailbreak resistance is structural: a fully jailbroken model can emit off-brand text and nothing else. Residual reputational risk is mitigated by a constrained, clearly-scoped persona, transcript review with purge, and an optional lightweight content filter, not by trying to win the prompt-injection arms race.

**7. Ingress and egress containment.** Ingress: the public HTTPRoute exposes only the SSR app; `/api/chat/public/*` is never added to it, so the chat API has no internet-facing path. The SSR app reaches the `chat_public` API as a ClusterIP over Linkerd mTLS. Egress: ADR 004's default-deny policy on the public binary's namespace gains exactly one rule, egress to the `inference` service (and, if retrieval needs it, `inference-embeddings`) on its port; the SSR app's egress allows only the internal chat API. Postgres (the read replica for note retrieval and the primary `-rw` for the module's own `chat_public` schema writes) and DNS remain the only other destinations. An RCE in the public chat path reaches the read replica (public rows only) and the inference endpoint, and nothing else in the cluster.

**8. Rendering and retention.** The overlay renders model output and note bodies as sanitized markdown with a strict CSP, never raw HTML, closing the XSS path on untrusted generative output. Transcripts and pseudonymous user/session details (hashed IP, Turnstile outcome, coarse geo from `CF-IPCountry`, user-agent hash, timestamps) are persisted together in the public-tier `chat_public` schema keyed by session, so abuse can be correlated to a session without storing raw PII. Postgres is the authoritative record, not telemetry: SigNoz carries operational metrics only (token counts, latencies, rejection rates, never message content). Telemetry was considered as the store and rejected because compaction and the turn/length limits already require the live conversation as transactional read-your-writes state that traces (sampled, async, lossy) cannot provide, and because a targeted takedown is a cheap `DELETE` in Postgres but a heavy mutation in ClickHouse. Because retaining anonymous user content is a liability, a purge mechanism is mandatory and ships with the feature: a scheduled retention/TTL job, an on-demand takedown by session or IP-hash, and a documented retention policy. Retention without a purge path is not an option.

### Before / After

| Aspect           | A public read endpoint today         | Public chat (decided)                                                  |
| ---------------- | ------------------------------------ | ---------------------------------------------------------------------- |
| Cost per request | One indexed point read on a replica  | GPU inference, bounded by semaphore + budgets                          |
| Public exposure  | API directly on the public HTTPRoute | SSR front door only; chat API internal (ClusterIP, mTLS)               |
| Identity         | None needed (cacheable)              | Turnstile-bound session, opaque httpOnly cookie over a server-side row |
| Abuse limit      | Envoy per-IP rate limit              | Per-session + per-IP + global ceiling                                  |
| GPU sharing      | N/A                                  | Reserved-headroom semaphore, trusted slots protected                   |
| State            | Stateless                            | Server-side session, compaction, server-authoritative limits           |
| Data scope       | `public` view via `public_reader`    | Same view via `public_reader`; retrieval cannot see private            |
| Model authority  | N/A                                  | No tools, text-only, fixed server-side prompt                          |
| Egress           | Postgres `-ro` + DNS                 | Adds inference service only                                            |
| Output handling  | JSON                                 | Sanitized markdown + CSP in the overlay                                |
| User content     | None stored                          | Transcripts persisted with mandatory purge tooling                     |

---

## Architecture

### Request flow

```mermaid
graph LR
    User[Anonymous user] -->|Turnstile challenge| CF[Cloudflare: WAF + Turnstile + Tunnel]
    CF -->|only public origin| SSR[public SvelteKit SSR - front door]
    SSR -->|Turnstile token + forwarded client IP, Linkerd mTLS| PChat[chat_public API - internal only]
    SSR -->|httpOnly session cookie| User
    PChat -->|siteverify + enforce budgets + turns + chars| Sess[(chat_public sessions)]
    PChat -->|acquire reserved-headroom slot| Sem{public semaphore}
    Sem -->|retrieve public nodes| View[(knowledge_public view via public_reader)]
    Sem -->|chat completion| Qwen[inference vLLM Qwen]
    Qwen -->|SSE token stream + node-touched events| PChat
    PChat -->|stream| SSR
    SSR -->|proxied SSE: sanitized markdown + highlighted public nodes| User
```

### Control layering and what enforces each

```mermaid
graph TD
    A[Turnstile -> signed session] -->|automation costs more than an IP| B[Admission]
    C[Per-session + per-IP + global budgets] -->|nested ceilings, global is the backstop| D[Abuse + cost containment]
    E[Reserved-headroom semaphore] -->|public concurrency below batch capacity| F[Trusted GPU workloads protected]
    G[Server-side sessions + compaction] -->|limits un-bypassable, context bounded| H[Turn + length + cost control]
    I[public_reader role + public views] -->|private rows physically unreadable| J[No exfiltration even when jailbroken]
    K[No tools + fixed prompt + sanitized render + CSP] -->|jailbreak yields only off-brand text| L[Bounded reputational blast radius]
    M[Default-deny egress + inference allow] -->|one destination added| N[Blast-radius containment]
```

### GPU reservation

```mermaid
graph LR
    subgraph vLLM [Qwen vLLM, max_num_seqs = N]
        Slots[decode slots]
    end
    Trusted[Discord + private chat + agents] -->|unthrottled| Slots
    PublicSem[public semaphore = N minus reserved] -->|capped concurrency| Slots
    Flood[public flood] -->|queued then shed at semaphore| PublicSem
```

The semaphore lives in the `chat_public` module and limits how many public requests are in flight to vLLM at once, leaving the remaining batch slots for trusted callers. It is admission control on the public client side, complementary to vLLM's internal scheduler.

---

## Alternatives Considered

- **Dedicated smaller model for public chat.** Rejected for now: full GPU blast-radius isolation, but it needs more VRAM or a second node and doubles model ops. The reserved-headroom semaphore on the shared endpoint gives availability isolation for trusted workloads at near-zero cost. Revisit only if measured contention shows the semaphore starving public users or failing to protect trusted ones.
- **Share the inference endpoint with no reservation, rate limits only.** Rejected: a public spike degrades the Discord bot, private chat, and agents directly. The whole point of the public/private split is that public cannot harm private.
- **IP-only rate limiting, no Turnstile.** Rejected: NAT-shared IPs punish legitimate users while rotation defeats the limit for attackers. An anonymous generative endpoint needs an identity costlier than an IP.
- **Direct browser-to-chat-API, exposed on the public HTTPRoute (like the notes API).** Rejected: it puts a second internet-facing handler next to the Turnstile-gated page, so an attacker can skip the SSR challenge and hit the API directly, and it spreads the public surface across two origins. Routing chat through the SSR front door keeps the challenge and the only public handler at one origin and lets SSR own the session cookie. The cheap cacheable read endpoints stay direct because they carry no per-request cost or session.
- **Stateless, client-supplied conversation history.** Rejected: every limit (turns, length, total tokens) would be re-validated against an untrusted payload each turn, context could not be bounded, and max-turns could be replayed around. Server-side sessions make the limits authoritative and compaction possible.
- **Prompt-instruction data protection ("never reveal private notes").** Rejected as a boundary: prompt instructions are not a security control. The `public_reader` role plus public views make private rows physically unreadable, which holds under any jailbreak.
- **Put public chat in the private monolith behind a filter.** Rejected: it is the highest-risk anonymous surface and must live in the isolated public binary (ADR 004 / ADR 010), never co-resident with secrets and write paths.
- **Ephemeral transcripts (no persistence).** Rejected by the chosen retention posture: transcripts are kept for abuse forensics and tuning, but the liability of indefinite anonymous storage is answered by mandatory purge tooling rather than by not storing at all.
- **Telemetry as the transcript store (reconstruct chats from SigNoz).** Rejected as the record of truth: the live conversation must be transactional read-your-writes state for compaction and the turn/length limits, which sampled, async traces cannot provide, and a per-user takedown is a cheap `DELETE` in Postgres but a heavy ClickHouse mutation. Postgres holds the content and the pseudonymous user details; telemetry stays metrics-only.
- **vLLM server-side priority/QoS scheduling instead of a client semaphore.** Deferred: a client-side reserved-headroom cap is entirely within our control and simple to reason about. Revisit if finer-grained QoS between trusted callers becomes necessary.

---

## Security

Builds on the `docs/security.md` baseline (Cloudflare Tunnel perimeter, Linkerd mTLS, non-root hardened pods) and inherits ADR 004's four-layer isolation and ADR 010's per-domain data and build boundaries. This ADR adds the controls specific to an anonymous, GPU-backed, generative surface. Deviations and additions:

- **New anonymous compute surface.** Unlike every other public route, chat spends GPU per request. The reserved-headroom semaphore plus the three-tier budget plus the global ceiling are the controls that keep that spend bounded and keep trusted workloads whole.
- **SSR is the only public origin for chat.** The chat API is never added to the public HTTPRoute; it is a ClusterIP reached from the SSR app over Linkerd mTLS. There is no internet path that skips the Turnstile-gated SSR front door. The default-deny egress policy from ADR 004 is extended with a single allow to the inference service (and embeddings if used) from the chat binary, and SSR egress allows only the internal chat API. These are the only widenings and must be reviewed as such.
- **The Turnstile secret stays in the backend, not in SSR.** Consistent with ADR 004's "public SSR holds no backend secrets," siteverify runs in the FastAPI binary; SSR forwards the user's Turnstile token and holds no secret. The Turnstile _site_ key is public by design; the _secret_ key is an `OnePasswordItem` scoped to verification only, not a backend or model credential.
- **Forwarded client IP is trusted only from SSR.** Because SSR is now the API's client, the real client IP (Cloudflare `CF-Connecting-IP`) is forwarded SSR-to-API for per-IP limiting and hashing. The API trusts that header only on connections carrying SSR's Linkerd identity, never from any other peer, so a direct caller cannot spoof an IP even if the API were reachable.
- **Confidentiality stays database-enforced.** Retrieval uses `public_reader` and the public view; the model has no path to private rows. This is consistent with ADR 004 and ADR 010 and is not weakened by any prompt content.
- **Untrusted output is sanitized.** Model output and note bodies render as sanitized markdown under a strict CSP. No raw HTML from model output reaches the DOM.
- **Anonymous user content is governed.** Persisted transcripts and pseudonymous user details get a stated retention policy, a scheduled TTL/purge job, and an on-demand takedown path for abuse and legal requests. Access to the transcript store is private-tier only. Chat content lives only in Postgres; telemetry to SigNoz is metrics-only and carries no message content or user details.

---

## Risks

| Risk                                                                            | Likelihood | Impact | Mitigation                                                                                                                                                                              |
| ------------------------------------------------------------------------------- | ---------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Semaphore mis-sized, public traffic still starves trusted workloads             | Medium     | High   | Size conservatively below `max_num_seqs`; alert on public queue depth and on trusted-caller inference latency; load test the reservation before launch                                  |
| Turnstile defeated by solver farms / token replay                               | Medium     | Medium | Tokens short-lived and server-signed, bound to a session; per-session and global budgets cap damage even with valid tokens; monitor solve-to-abuse ratio                                |
| Jailbreak produces reputationally damaging output                               | High       | Medium | Blast radius is structurally low (no tools, no private data); constrained persona; sanitized render; transcript review + purge; optional content filter; accept residual                |
| Retrieval leaks a private note                                                  | Low        | High   | `public_reader` role + public view make private rows physically unreadable; cover with a test asserting a private note is not retrievable as `public_reader`; same guarantee as ADR 004 |
| Indirect injection embedded in note content steers the model                    | Low        | Low    | Notes are Joe's own public notes; retrieved text is delimited data, the model has no tools, so a steered model still cannot act                                                         |
| Transcript store accumulates PII / abusive content (indefinite retention)       | High       | Medium | Mandatory purge job + on-demand takedown by session/IP-hash + documented policy + private-tier-only access; ship purge tooling with the feature, not after                              |
| XSS via model output rendered in the overlay                                    | Medium     | High   | Sanitized markdown only, strict CSP, no raw HTML from model output; cover the renderer with injection test cases                                                                        |
| Compaction summarization adds its own GPU load                                  | Medium     | Low    | Summaries run under the same semaphore and budget; cap summary frequency; a summary is far cheaper than an unbounded growing context                                                    |
| Embeddings GPU contention from public retrieval                                 | Medium     | Medium | Apply the reserved-headroom treatment to `inference-embeddings` too, or cache/precompute public-note embeddings so retrieval is a vector read, not a GPU call                           |
| Global ceiling set so low it blocks legitimate use, or so high it permits abuse | Medium     | Medium | Start conservative, expose budget counters in dashboards, tune from real traffic; budgets are config, not code                                                                          |
| Forwarded client IP spoofed, defeating per-IP limits                            | Low        | Medium | The API trusts `CF-Connecting-IP` only on connections bearing SSR's Linkerd identity; the chat API is not internet-reachable, so no untrusted peer can set it                           |
| SSE proxying through SvelteKit SSR adds latency or buffers the stream           | Medium     | Low    | SvelteKit streams `Response` bodies; pass the SSE through unbuffered and verify token-by-token delivery in the load test                                                                |

---

## Open Questions

1. **Budget values.** Concrete numbers for the character cap, max turns, per-turn and per-session token ceilings, semaphore size, and the global ceiling are tuning parameters resolved in the plan and a pre-launch load test, not in this ADR.
2. **Content moderation pass.** Whether to add a lightweight model-based or heuristic content filter on input and/or output, or to rely solely on the low blast radius plus transcript review. Lean to none at launch, reconsider from observed transcripts.
3. **Embeddings strategy.** Whether public-note embeddings are precomputed and cached (retrieval becomes a pure vector read, no GPU at request time) or computed live under the semaphore. Precompute is the likely answer given the public set is small and changes slowly.
4. **Retention policy specifics.** Default TTL (if any beyond on-demand purge), the takedown process, and the public-facing notice wording.
5. **Overlay node-touched transport.** Whether to reuse the existing `node_discovered` SSE event from the private `/explore` path to stream highlighted public nodes (proxied through SSR), or compute the touched set post-hoc.
6. **Session/transcript write path.** Sessions, budget counters, and transcripts write to the `chat_public` schema on the Postgres primary via a dedicated role; the cookie is an opaque id over that row. Confirm whether the latency-sensitive budget counters stay in Postgres or move to a faster store, and that the public namespace egress allows the primary `-rw` endpoint for this one schema.
7. **Shared-snapshot retention and takedown.** Opt-in sharing (see the addendum) adds `chat_public.shared_snapshots`, an independent frozen copy of transcript content. The purge tooling mandated by layer 8 must therefore cover snapshots too: a takedown by session or IP-hash must also delete that session's snapshots, and snapshots want their own TTL. No scheduled retention job exists for `chat_public` yet (only the read-time session-TTL flip), so this is recorded as an obligation the retention tooling must satisfy when it lands, not a built control.

---

## Addendum: opt-in transcript sharing (2026-06-20)

Public chat shipped deliberately unshareable: a transcript lived only on the server (layer 4) and in the visitor's session, with no addressable URL. This addendum adds an **opt-in, read-only "share this chat"** capability without weakening any of the eight layers.

**Decision.** A visitor can explicitly share a conversation. The share action mints an immutable, read-only **snapshot** of the transcript at an unguessable opaque id (`secrets.token_urlsafe`, the same posture as a session id), served at `/public/app/notes/s/<id>`. The live session, its `httpOnly` cookie, and its budget are never exposed: sharing does not hand anyone the session, only a frozen copy. This was chosen over putting the live session id in a URL, which would let a recipient append to the conversation and burn the owner's budget, and would share by default rather than on intent.

**Why server-authoritative minting.** The snapshot is built server-side from the stored transcript (`sessions.get_transcript`), never from client-supplied content. This is the integrity counterpart to layer 4: just as the server ignores client-supplied history when generating, it ignores client-supplied content when sharing, so a forged request body cannot fabricate a public artifact that puts words in the model's mouth. A test asserts forged body fields never reach the snapshot.

**Consistency with the existing layers.**

- **Rendering (layer 8).** The shared view renders the transcript through the same sanitized-markdown path as the live overlay (HTML-escaped, no raw HTML, no `javascript:`/`data:` URLs). Model output stays untrusted on the read path too. The page is `noindex`: an anonymous visitor's shared link is their content, not site content we want crawled.
- **Read/write isolation (ADR 004, layer 4).** Minting writes to `chat_public` on the primary via `public_writer`; the public read route reads the snapshot via `public_reader` on the replica, so the public read path never touches the primary. The streaming-replication lag (single-digit ms) is dwarfed by the human and network round-trip between minting a link and anyone opening it, so a freshly minted link is reliably visible by the time it is followed; the isolation is worth more than closing a sub-second window.
- **Confinement (layer 5).** Grounding is persisted per assistant turn (`chat_public.messages.touched`, the public notes the turn touched) and carried into the snapshot so the shared view shows the same "grounded in" chips. That set is, by construction, public-notes-only, so nothing private is exposed by surfacing it.

**New obligation (threat 8 / layer 8).** `shared_snapshots` is a second store of anonymous user content, an independent frozen copy that outlives its session by design (the `source_session_id` FK is `ON DELETE SET NULL` so routine session aging never breaks a live link). That independence means the mandatory purge/takedown tooling must explicitly cover snapshots: a takedown by session or IP-hash must also delete that session's snapshots, and snapshots want their own retention TTL. Recorded as Open Question 7.

---

## References

| Resource                                                                                     | Relevance                                                                                                                                            |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| [ADR 004: Public Read-Only Service Isolation](004-public-read-only-service-isolation.md)     | The isolated public service this chat module is composed into; source of `public_reader`, public views, replica, and default-deny egress             |
| [ADR 010: FastMonolith Modular Framework](../services/010-fastmonolith-modular-framework.md) | The module/tier/profile mechanism that makes `chat_public` a PUBLIC-tier module composed into the public binary, with private code physically absent |
| [ADR 002: Path-Based Ingress Tiers](../../../projects/platform/ARCHITECTURE.md)           | Public hostname and tier the chat sits behind                                                                                                        |
| Public-notes visibility design                                                               | V1/V2 visibility work; this is the deferred V3 chat surface                                                                                          |
| Public chat V3 implementation plan                                                           | Implementation plan for this decision                                                                                                                |
| `projects/agent_platform/inference/deploy/values-prod.yaml`                                  | The shared Qwen vLLM the semaphore reserves headroom against                                                                                         |
| `projects/monolith/chat/summarizer.py`                                                       | Existing rolling-summary pattern reused for compaction                                                                                               |
| [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/)                         | The admission challenge minting the session token                                                                                                    |
| `docs/security.md`                                                                           | Defense-in-depth baseline this ADR extends                                                                                                           |
