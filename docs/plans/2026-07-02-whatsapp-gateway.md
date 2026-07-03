# WhatsApp Channel Gateway Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; one comprehensive code review per merged PR, tests run on CI only).

**Goal:** Implement ADR 039: a transport-only Go whatsmeow gateway under `projects/monolith/whatsapp/`, a `chat.whatsapp_outbox` send path, an inbound endpoint reusing the ADR 035 attention/depth machinery, group-keyed sessions with the household tier, and the household capabilities (knowledge record, calendar, Q&A, digests).

**Architecture:** The gateway (Go, single replica, own Deployment in the monolith chart) speaks WhatsApp via `go.mau.fi/whatsmeow` with its session in a dedicated `whatsapp` Postgres schema, forwards allow-listed group messages to `POST /internal/whatsapp/inbound`, and drains `chat.whatsapp_outbox` for sends/edits/reactions. All agent behaviour stays in the monolith: `chat/attention.py` (attention + depth), the concierge reply path, `goosecracker/dispatch.py` sessions, `goosecracker/tiers.py` for the new `household` tier. No LLM or tool calls in the gateway.

**Tech Stack:** Go (root `go.mod` + bzlmod `go_deps`, gazelle-generated BUILD via `format`), apko dual-arch image (template: `projects/firecracker/substrate/invoke/`, `projects/firecracker/git-mirror/`), Python monolith (FastAPI/SQLModel), Atlas migrations in `projects/monolith/chart/migrations/`, 1Password operator for secrets.

**Companion spec:** [2026-07-02-whatsapp-gateway-spec.md](2026-07-02-whatsapp-gateway-spec.md) defines behaviour and acceptance criteria. [ADR 039](../decisions/agents/039-whatsapp-channel-gateway.md) holds the rationale.

**Repo rules that override generic practice:**

- No local test runs. Write tests first, implement, commit, push; CI (BuildBuddy) is the verifier. Batch pushes per phase.
- New `chat/*_test.py` files need a hand-added `py_test` in `projects/monolith/BUILD` (gazelle is excluded there); copy the shape of an existing `chat_*_test` target.
- Go BUILD files are gazelle-managed: run `format` after adding Go sources; new external Go deps go through `go get` + the `use_repo` list in `MODULE.bazel`.
- SQLite test fixtures use `create_all`: mirror any SQL CHECK constraints in `__table_args__`.
- New tables need an Atlas migration in `projects/monolith/chart/migrations/YYYYMMDDhhmmss_<desc>.sql`; keep them small (256 KiB ConfigMap cap).
- Deploying monolith changes requires a manual chart bump: `projects/monolith/chart/Chart.yaml` version AND `projects/monolith/deploy/application.yaml` `targetRevision` together.
- Container images are apko, dual-arch, uid 65532, never Dockerfiles.

**Phasing = PR boundaries.** Each phase is one PR (worktree off main, conventional commits, one code review at PR end, merge on green CI before the next phase starts). Phases 1 and 2 are pure plumbing with no user-visible behaviour; the bot only starts talking in Phase 3.

---

## Phase 1: Gateway skeleton, pairing, deployment

New Go service that pairs, connects, logs group events, and parks/alerts on logout. No inbound forwarding, no outbox yet.

### Task 1.1: Go module and whatsmeow client wrapper

**Files:**

- Create: `projects/monolith/whatsapp/` (Go package: `main.go`, `client.go`, `config.go`)
- Modify: `go.mod` / `go.sum` (`go get go.mau.fi/whatsmeow`), `MODULE.bazel` `use_repo` list
- Generated: BUILD files via `format` (gazelle)

**Steps:**

1. Wrapper owning the whatsmeow client: Postgres sqlstore (DSN from env, `search_path=whatsapp`), connect-or-request-pairing-code state machine, allow-list of group JIDs from env/config, structured logging of connection state transitions.
2. Pairing: when no stored session exists, request a phone-number pairing code for the configured number and POST it to the monolith notify path (spec §1); log-only fallback.
3. Parked state: on `LoggedOut`/ban events, stop work, set health state, fire one notify. Health endpoint `/healthz` reports `connected|pairing|parked`.
4. Unit tests for the state machine (fake client interface, no live WhatsApp).
5. Commit: `feat(whatsapp): whatsmeow gateway skeleton with pairing and parked states`

### Task 1.2: apko image + chart deployment

**Files:**

- Create: `projects/monolith/whatsapp/apko.yaml` + image BUILD (copy `projects/firecracker/git-mirror/` shape; dual-arch, uid 65532)
- Create: `projects/monolith/chart/templates/whatsapp-gateway.yaml` (Deployment, replicas: 1, gated by `values.whatsapp.enabled`, default **false**)
- Create: `projects/monolith/chart/templates/onepassworditem-whatsapp.yaml` (bot number config, inbound bearer token)
- Modify: `projects/monolith/chart/values.yaml`, `projects/monolith/deploy/values.yaml`
- Modify: chart bump (`Chart.yaml` + `deploy/application.yaml` together)

**Steps:**

1. Image pinning flows through `helm_images_values` as with existing images (never hand-pinned digests).
2. DB access: dedicated role with ownership of the `whatsapp` schema only; DSN via CNPG secret reference. whatsmeow auto-creates/migrates its own tables in that schema.
3. Resources per convention: CPU request no limit, memory request=limit (small: this is a websocket pump).
4. Migration creating the `whatsapp` schema + role grant.
5. `helm template` render check; commit: `feat(whatsapp): gateway image and chart deployment behind values flag`

**Phase 1 gate:** PR, CI green, merge. Then flip `whatsapp.enabled` in a follow-up values commit, pair the throwaway number using the notify flow, verify `connected` health and group-event logs, and verify pod-delete resume (spec §1 acceptance).

---

## Phase 2: Outbox send path

### Task 2.1: `chat.whatsapp_outbox` migration + monolith enqueue helpers

**Files:**

- Create: migration `chart/migrations/<ts>_chat_whatsapp_outbox.sql` (schema per spec §3)
- Modify: `projects/monolith/chat/models.py` (WhatsappOutbox model), new `projects/monolith/chat/whatsapp_outbox.py` (enqueue_message / enqueue_edit / enqueue_reaction, mirroring `chat/outbox.py` writer helpers)
- Test: `projects/monolith/chat/whatsapp_outbox_test.py` (hand-register `py_test` in BUILD)

**Steps:**

1. Failing tests: kind/field validation (exactly one shape per kind, CHECK mirrored in `__table_args__`), edit rows must reference a sent original.
2. Implement; commit: `feat(whatsapp): outbox table and enqueue helpers`

### Task 2.2: Gateway drain loop

**Files:**

- Create: `projects/monolith/whatsapp/outbox.go` (+ tests with a fake sender)

**Steps:**

1. Poll oldest-unposted per group, translate: `message` (optionally quoting), `edit` (via `edit_of` → `sent_message_id`), `reaction` add/remove. Stamp `posted_at`/`sent_message_id`; on repeated failure park the row with `last_error` and continue (no poison pill).
2. Respect the 15-minute edit window: a failed edit for age reasons marks the row so the monolith reposts (spec §4).
3. Commit: `feat(whatsapp): outbox drain with edits and reactions`

**Phase 2 gate:** PR, CI, merge, chart bump. Live check: insert a test outbox row, see the group message; edit it; react to it.

---

## Phase 3: Inbound + conversational replies (the bot starts talking)

### Task 3.1: Group registry + household tier

**Files:**

- Create: migration `chart/migrations/<ts>_chat_whatsapp_group.sql` (spec §6)
- Modify: `projects/monolith/chat/models.py`, `projects/monolith/goosecracker/tiers.py` (add `household`: local-Qwen env, ADR 034 tool subset: knowledge + calendar + reminders, no repo/cluster/artifact)
- Test: registry + tier mapping tests (BUILD-registered)

### Task 3.2: Inbound endpoint + attention/depth wiring

**Files:**

- Create: `projects/monolith/chat/whatsapp_inbound.py` (FastAPI router: bearer auth, dedupe on (`group_jid`,`message_id`), registry check)
- Modify: `projects/monolith/chat/attention.py` call sites for a non-Discord channel (trigger-name/reply engagement + ambient classify against the group directive seed)
- Modify: concierge reply path to sink into `whatsapp_outbox` when the originating channel is WhatsApp
- Modify: `projects/monolith/whatsapp/` gateway to forward per spec §2 (ordered, retried)
- Test: inbound auth/dedupe tests; attention routing tests; reply-sink test

**Steps:**

1. Follow the ADR 035 Phase 4 seam: chat answers in-monolith; `needs_agent` escalation is Phase 4 of this plan (until then, escalations get an honest "not wired up yet" reply behind a flag).
2. Commit series; chart bump.

**Phase 3 gate:** PR, CI, merge. Live: question in the group gets a conversational reply; unknown-group traffic drops at the gateway; off-topic ambient messages stay silent (spec §4 acceptance).

---

## Phase 4: Sessions, reactions, checklist, steering

**Files:**

- Modify: `projects/monolith/chat/goosecracker.py` + `goosecracker/dispatch.py` + `chart/migrations/<ts>_goosecracker_sessions_provider.sql` (add `provider` discriminator; session key `wa:<group_jid>`; one active session per group)
- Modify: reaction lifecycle + checklist writer to emit through `whatsapp_outbox` (reactions on trigger message; checklist as edited message with window-expiry repost per spec §4)
- Modify: steering: group messages during a running `wa:` session enqueue as steering with author attribution (existing pending-queue machinery)
- Tests: session keying, single-active-session, checklist repost-on-expiry, steering attribution (BUILD-registered)

**Phase 4 gate:** PR, CI, merge, chart bump. Live: task request produces ack + reactions + evolving checklist; partner mid-run message steers rather than forking.

---

## Phase 5: Household capabilities

### Task 5.1: Record-to-knowledge with confirmation turn

Route record intents to a confirm-then-capture flow into the existing knowledge raw pipeline with group/author provenance (spec §5a). Never capture without the confirmation.

### Task 5.2: Calendar scheduling

Resolve ADR 039 open question 1: provision a cluster-side Google Calendar credential (1Password-managed) and implement create-event with a clarify-once rule (spec §5b). Fallback if the credential path slips: scheduling intents land in the daily digest as drafts.

### Task 5.3: Reminders + morning digest

Scheduler CronWorkflow (existing registry pattern) rendering calendar + open reminders into a digest outbox message; conversational reminder creation; quiet hours from `digest_config` (spec §5d).

**Phase 5 gate:** PR(s), CI, merge, chart bump. Live acceptance: the three spec §5 acceptance scenarios pass in the real group.

---

## Verification strategy

- Unit tests per task (CI-only, BUILD-registered where gazelle is excluded).
- Each phase ends with a live check in the real group (the household group is also the staging environment; Phase 1-2 checks are invisible plumbing, so nothing half-built talks to the partner before Phase 3 is deliberately enabled).
- STPA: after Phase 3 merges, run the stpa skill over `projects/monolith/whatsapp/` (new external-input surface + new principal class).
- The `/improve-recipes`-style session review loop applies once sessions exist (Phase 4+); household transcripts stay out of any shared eval corpora (privacy, ADR 039).
