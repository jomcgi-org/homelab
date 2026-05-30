# LIB-NATS — projects/lakehouse/nats_client (async NATS JetStream wrapper)

**Unit:** LIB-NATS (Wavefront 2, parallel library unit)
**ADR:** [agents/016 — NATS as the Canonical Event Stream](../../decisions/agents/016-nats-canonical-event-stream.md)
**Branch:** `feat/lakehouse-lib-nats` — purely additive, only new files under
`projects/lakehouse/nats_client/`. No existing file modified.

## What shipped

- `projects/lakehouse/nats_client/__init__.py` — re-exports `NatsClient`,
  `resolve_url`, `DEFAULT_URL`.
- `projects/lakehouse/nats_client/client.py`:
  - `DEFAULT_URL = "nats://nats.nats.svc.cluster.local:4222"` (in-cluster service
    discovery; NATS is internal-only per ADR 016).
  - `resolve_url(env: Mapping | None = None) -> str` — pure, unit-testable; reads
    `NATS_URL` (defaults to `os.environ`), falls back to `DEFAULT_URL`. Treats
    blank/whitespace-only as unset and strips surrounding whitespace.
  - `class NatsClient`:
    - `connect()` — `await nats.connect(self.url)` then `self.js = self.nc.jetstream()`.
    - `publish(subject, payload, *, msg_id=None, headers=None)` — merges headers,
      sets `Nats-Msg-Id` from `msg_id` (JetStream dedup per ADR 017), forwards to
      `self.js.publish(subject, payload, headers=...)`. Sends `headers=None` when no
      dedup/headers requested. Structurally satisfies LIB-EVENTS' `Publisher`
      protocol (matched by shape — `events` is NOT imported).
    - `pull_subscribe(subject, durable, *, batch=10)` — durable (consumer-group)
      pull consumer; returns a `_PullSubscription` wrapper holding the configured
      batch with a small `fetch(batch=None, *, timeout=5.0)` helper.
    - `close()` — drains/closes if connected (no-op otherwise).
- `projects/lakehouse/nats_client/client_test.py` — 14 hermetic tests, NATS fully
  mocked (`unittest.mock.AsyncMock`/`MagicMock` + `patch`), coroutines driven with
  `asyncio.run` (no `pytest_asyncio` plugin → no new pip dep). Covers `resolve_url`
  default/override/blank/strip; `connect` resolved-URL + default-URL; `publish`
  msg-id header + subject/payload forwarding + header merge + no-header path +
  pre-connect guard; `pull_subscribe` durable creation + `fetch` batch defaulting
  and override; `close` connected + not-connected.
- `projects/lakehouse/nats_client/BUILD` — best-effort: `py_library nats_client`
  (srcs glob excluding `*_test.py`, visibility `//:__subpackages__`,
  deps `["@pip//nats_py"]`) + `py_test client_test`
  (deps `[":nats_client", "@pip//nats_py", "@pip//pytest"]`). `py_test` loaded from
  `//bazel/tools/pytest:defs.bzl`, `py_library` from `@aspect_rules_py//py:defs.bzl`.

## Key decisions / conventions

- pip dep label: **`@pip//nats_py`** (underscored Bazel name; `import nats`).
  No new pip deps added.
- Import style: workspace-root absolute
  (`from projects.lakehouse.nats_client.client import NatsClient`).
- Tests use `asyncio.run(...)` instead of `@pytest.mark.asyncio` so the suite stays
  hermetic without pulling in `pytest_asyncio` (which is not in the allowed BUILD
  deps for this unit). Matches ships' AsyncMock/MagicMock mocking idiom but avoids
  the asyncio-plugin dependency.
- `nats-py` API confirmed against the vendored 2.x client:
  `js.pull_subscribe(subject, durable=...)` and `PullSubscription.fetch(batch, timeout=...)`.

## Deviations / notes

- `format`/gazelle was not available on PATH in the implementation shell; the
  BUILD is best-effort per the unit brief. CI's `ci-format-bot` runs the
  authoritative gazelle and normalizes the BUILD (+ adds `semgrep_test`) on the
  PR branch.
- Local sanity run (`.venv` pytest, not the bazel loop): 14 passed.
