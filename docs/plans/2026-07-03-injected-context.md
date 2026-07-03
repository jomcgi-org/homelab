# Caller-Provided Context Injection (`/injected-context/`) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give agent guests a third opaque hydration input (an `injectedContext` file bundle unpacked to `/injected-context/`), rebuilt per turn from Discord channel history, so an agent can grep caller-side conversation it otherwise cannot see. Implements ADR 040.

**Architecture:** A `chat.api.build_injected_context(thread_id, tier)` producer (the only layer that knows the source is Discord) turns the parent channel's recent messages into a `{filename: content}` map including a self-describing `README.md`. `goosecracker`'s per-turn `_run_one_turn` calls it beside `sessions.load` and adds the map to the fc-invoke payload as `injectedContext`. The Go guest handler unpacks the map to `/injected-context/`, next to how it already writes `/tmp/goose/task.md`. The substrate, fc-invoke daemon, and handler stay context-agnostic (opaque map, path-traversal guarded). The `agent.yaml` recipe carries one generic, source-blind line pointing goose at the directory.

**Tech Stack:** Python (monolith: `chat`, `goosecracker`), Go (guest-init handler), goose recipe YAML, Bazel/BuildBuddy CI.

**Testing note (repo-specific):** There is NO local test loop, do not run `go test`, `pytest`, or `bazel test` from the workstation (the BuildBuddy `workflows` pool has no darwin runners). Write the test first in each task (TDD ordering preserved), then implement; verification for the whole plan is the end-of-plan CI run on the pushed branch. The "run the test" steps below therefore say **defer to CI** rather than giving a local command.

**Compatibility note:** The two wire sides degrade gracefully and can deploy in any order. Go ignores unknown JSON fields, so an old guest ignores `injectedContext`; a new guest receiving no field sees an empty map and writes nothing. No deploy-ordering constraint.

---

### Task 1: Guest handler unpacks the bundle to `/injected-context/`

Add the opaque field and a path-traversal-guarded unpacker. The handler stays context-agnostic: it writes whatever filename to content pairs it is handed.

**Files:**

- Modify: `projects/firecracker/goosecracker/guest-init/internal/handler/handler.go` (struct at :44-54; unpack site after :168; package vars at :251-254; new `writeInjectedContext` near `writeTaskFile` at :262)
- Test: `projects/firecracker/goosecracker/guest-init/internal/handler/handler_test.go` (`TestMain` at :117 for the temp-dir var; new tests)

**Step 1: Write the failing tests**

Add to `handler_test.go`. In `TestMain` (after the `contextFilePath` line ~:123) redirect the new package var to the temp dir:

```go
	injectedContextDir = filepath.Join(dir, "injected-context")
```

Then add two tests:

```go
func TestInjectedContextUnpacksBundleToDir(t *testing.T) {
	h := New(Config{}) // match the Config{} construction used by existing cold-run tests
	req := AgentRequest{
		Recipe:  "agent",
		Task:    "irrelevant",
		Session: "sess-ic",
		InjectedContext: map[string]string{
			"README.md":     "source: discord channel 42",
			"transcript.md": "alice: hello\nbob: hi",
		},
	}
	// invoke the handler the same way the other tests do (serve the request /
	// call the handler func), then assert files landed:
	callHandler(t, h, req) // replace with the exact call pattern used above in this file
	for name, want := range map[string]string{
		"README.md":     "source: discord channel 42",
		"transcript.md": "alice: hello\nbob: hi",
	} {
		got, err := os.ReadFile(filepath.Join(injectedContextDir, name))
		if err != nil || string(got) != want {
			t.Fatalf("file %s = %q, %v; want %q", name, got, err, want)
		}
	}
}

func TestInjectedContextRejectsPathTraversal(t *testing.T) {
	h := New(Config{})
	req := AgentRequest{
		Recipe:  "agent",
		Session: "sess-ic2",
		InjectedContext: map[string]string{
			"../escape.md":       "nope",
			"/abs.md":            "nope",
			"sub/dir/nested.md":  "nope",
			"ok.md":              "yes",
		},
	}
	callHandler(t, h, req)
	// only the safe basename is written, directly under the dir:
	if _, err := os.Stat(filepath.Join(injectedContextDir, "ok.md")); err != nil {
		t.Fatalf("ok.md not written: %v", err)
	}
	// traversal/absolute/nested names must NOT create anything outside the dir:
	if _, err := os.Stat(filepath.Join(filepath.Dir(injectedContextDir), "escape.md")); err == nil {
		t.Fatal("path traversal escaped the injected-context dir")
	}
}
```

Match `callHandler` / `New(Config{})` to the exact invocation pattern the neighbouring tests use (e.g. `TestColdRunBuildsArgvStreamsAndReturnsResult` at :143). Do not invent a new harness.

**Step 2: Run tests to verify they fail**: defer to CI (compile error: `InjectedContext` / `injectedContextDir` undefined).

**Step 3: Implement**

Add the field to `AgentRequest` (after `SessionDb` at :53):

```go
	// InjectedContext is an opaque map of filename to text content the caller
	// staged for this run (ADR 040). The handler unpacks it verbatim to
	// injectedContextDir; it never interprets the source (Discord, etc.). Keys
	// are basenames; traversal/absolute/nested keys are skipped defensively.
	InjectedContext map[string]string `json:"injectedContext"`
```

Add the package var (in the `var (...)` block at :251):

```go
	injectedContextDir = "/injected-context"
```

Add the unpacker near `writeTaskFile` (:262):

```go
// writeInjectedContext writes each filename to content pair to injectedContextDir
// (ADR 040). Filenames are basenames only: a key that is absolute, contains a
// path separator, or resolves to ".."/"." is skipped, so a caller-staged bundle
// can never write outside the dir even though the producer is trusted. A write
// failure is soft: injected context is best-effort background, not required for
// the run, so we log and continue rather than fail the turn.
func writeInjectedContext(files map[string]string) {
	if len(files) == 0 {
		return
	}
	if err := os.MkdirAll(injectedContextDir, 0o755); err != nil {
		slog.Warn("handler: create injected-context dir failed", "err", err)
		return
	}
	for name, content := range files {
		base := filepath.Base(name)
		if base != name || base == "." || base == ".." || strings.ContainsRune(name, filepath.Separator) {
			slog.Warn("handler: skipping unsafe injected-context filename", "name", name)
			continue
		}
		p := filepath.Join(injectedContextDir, base)
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			slog.Warn("handler: write injected-context file failed", "name", base, "err", err)
		}
	}
}
```

Wire it into the handler right after the `writeTaskFile` block succeeds (after :168, before the `harness.GooseCommand` call at :170):

```go
		writeInjectedContext(req.InjectedContext)
```

Confirm `strings` is imported (add to the import block if not).

**Step 4: Verify**: defer to CI.

**Step 5: Commit**

```bash
git add projects/firecracker/goosecracker/guest-init/internal/handler/handler.go \
        projects/firecracker/goosecracker/guest-init/internal/handler/handler_test.go
git commit -m "feat(goosecracker): unpack injectedContext bundle to /injected-context/ in guest"
```

---

### Task 2: The `build_injected_context` producer (the only source-aware layer)

A sync function that resolves the thread's parent channel, pulls its recent messages, and returns a `{filename: content}` map with a self-describing `README.md` and a `transcript.md`. Lives in `chat.goosecracker` (has the DB session + `parent_channel_for_thread`), re-exported through `chat.api` so `goosecracker` reaches it across the enforced import boundary.

**Files:**

- Modify: `projects/monolith/chat/goosecracker.py` (new function near `parent_channel_for_thread` at :776)
- Modify: `projects/monolith/chat/api.py` (re-export, alongside :17 `parent_channel_for_thread`)
- Test: `projects/monolith/chat/goosecracker_test.py` (existing, add tests, no new BUILD target needed)

**Step 1: Write the failing test**

Add to `chat/goosecracker_test.py`. Follow the existing message-insert fixture pattern in the chat test suite (e.g. `chat/summarizer_agent_reply_test.py` inserts `Message` rows under SQLite, handling the pgvector `embedding` column, reuse that exact helper; do NOT hand-roll embedding handling). Insert a `GoosecrackerSession` row with `parent_channel_id="chan-1"` for `thread_id="thr-1"`, and a few `Message` rows in `chan-1`.

```python
def test_build_injected_context_bundles_channel_transcript(session_fixture):
    # ... seed GoosecrackerSession(thread_id="thr-1", parent_channel_id="chan-1")
    # ... seed Message rows in channel "chan-1": alice "hello", bob "world"
    bundle = build_injected_context("thr-1", tier="")
    assert set(bundle) == {"README.md", "transcript.md"}
    assert "hello" in bundle["transcript.md"]
    assert "world" in bundle["transcript.md"]
    assert "chan-1" in bundle["README.md"]        # provenance names the source
    assert "injected-context" in bundle["README.md"].lower()

def test_build_injected_context_empty_when_no_parent_channel(session_fixture):
    # thread with no session row / no parent channel -> empty bundle, no crash
    assert build_injected_context("unknown-thread", tier="") == {}
```

**Step 2: Run test to verify it fails**: defer to CI (`build_injected_context` undefined).

**Step 3: Implement** in `chat/goosecracker.py`:

```python
# ADR 040: how many recent parent-channel messages to stage, and the per-message
# content cap. Kept modest so the bundle never bloats the fc-invoke hot path; a
# truncation is logged so a silent cap never reads as "full history injected".
_INJECTED_CONTEXT_MSG_LIMIT = 50
_INJECTED_CONTEXT_PER_MSG_CHARS = 2000


def build_injected_context(thread_id: str, tier: str = "") -> dict[str, str]:
    """Build the caller-provided context bundle for an agent turn (ADR 040).

    Returns a ``{filename: content}`` map staged into the guest's
    ``/injected-context/``. Source-aware (Discord): resolves the thread's parent
    channel and packs its recent messages as ``transcript.md`` plus a
    self-describing ``README.md``. Empty map when the thread has no parent
    channel or the channel has no messages, so callers can inject unconditionally.
    Sync; call via ``asyncio.to_thread``. ``tier`` is accepted for the trust-tier
    filter (ADR 040 Security); all current tiers may see the invoking channel.
    """
    parent = parent_channel_for_thread(thread_id)
    if not parent:
        return {}
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Message)
            .where(Message.channel_id == parent)
            .order_by(Message.created_at.desc())
            .limit(_INJECTED_CONTEXT_MSG_LIMIT)
        ).all()
    if not rows:
        return {}
    rows = list(reversed(rows))  # oldest -> newest reads naturally
    lines = []
    truncated = 0
    for m in rows:
        body = m.content or ""
        if len(body) > _INJECTED_CONTEXT_PER_MSG_CHARS:
            body = body[:_INJECTED_CONTEXT_PER_MSG_CHARS] + " …[truncated]"
            truncated += 1
        lines.append(f"{m.username}: {body}")
    if truncated:
        logger.info(
            "build_injected_context: truncated %d/%d messages for channel %s",
            truncated,
            len(rows),
            parent,
        )
    transcript = "\n".join(lines)
    readme = (
        "# Injected context\n\n"
        "This directory holds context the caller staged for this task. You did "
        "not gather it and it is not in the repo. Grep or read it when the user "
        "refers to an earlier discussion.\n\n"
        f"- Source: recent messages from Discord channel `{parent}` "
        f"(the parent of this agent thread).\n"
        f"- `transcript.md`: the last {len(rows)} message(s), oldest first, "
        "as of this turn. Rebuilt every turn, so it grows as the thread advances.\n"
    )
    return {"README.md": readme, "transcript.md": transcript}
```

Add imports if missing at the top of `chat/goosecracker.py`: `from sqlmodel import select`, `from chat.models import Message` (confirm the module's existing import style; `Session`, `get_engine`, `logger`, `parent_channel_for_thread` are already present in this file).

Re-export in `chat/api.py` (mirror line :17 and the `__all__` entry at :36):

```python
from chat.goosecracker import build_injected_context  # re-exported
```

```python
    "build_injected_context",
```

**Step 4: Verify**: defer to CI.

**Step 5: Commit**

```bash
git add projects/monolith/chat/goosecracker.py projects/monolith/chat/api.py \
        projects/monolith/chat/goosecracker_test.py
git commit -m "feat(chat): add build_injected_context producer for /injected-context/ (ADR 040)"
```

---

### Task 3: `goosecracker` builds the bundle per turn and ships it in the payload

Populate the bundle inside `_run_one_turn` (per-turn, beside `sessions.load`) so it persists and evolves across turns, and add it to the fc-invoke payload as `injectedContext`.

**Files:**

- Modify: `projects/monolith/goosecracker/runner.py` (`_run_one_turn`, bundle build near :525, payload dict :533-543)
- Test: `projects/monolith/goosecracker/tests/runner_test.py` (existing, add a test)

**Step 1: Write the failing test**

Add to `runner_test.py`, modelled on the existing `test_run_and_deliver_*` tests (they monkeypatch `_post_agent_run` / the HTTP post). Monkeypatch `chat.api.build_injected_context` to return a known map and capture the payload:

```python
async def test_run_one_turn_injects_context_bundle(monkeypatch):
    captured = {}

    async def fake_post(url, payload, on_retry):
        captured["payload"] = payload
        return {"status": "ok", "result": "done", "sessionDb": ""}

    monkeypatch.setattr(runner, "_post_agent_run", fake_post)
    # build_injected_context is reached via `from chat.api import build_injected_context`
    # inside _run_one_turn; patch it on chat.api:
    import chat.api
    monkeypatch.setattr(
        chat.api, "build_injected_context", lambda tid, tier="": {"transcript.md": "hi"}
    )
    # ... patch the other chat.api boundary calls the existing tests already stub
    #     (ensure_steering_token, mark_completed, _deliver, sessions.load, etc.)

    await runner._run_one_turn(
        "sess-1", task="q", recipe="agent", tier="", git_mirror="", git_ref="",
        discord_thread="thr-1",
    )
    assert captured["payload"]["injectedContext"] == {"transcript.md": "hi"}
```

Reuse whatever stubbing the neighbouring `_run_one_turn` tests already do for the boundary calls, do not re-stub from scratch.

**Step 2: Run test to verify it fails**: defer to CI (`injectedContext` absent from payload).

**Step 3: Implement** in `runner.py`. After the `session_db = await asyncio.to_thread(sessions.load, session)` line (:525), build the bundle via the boundary (only when there is a Discord thread to source from):

```python
        # ADR 040: stage caller-provided context for this turn. Reached through
        # chat.api like the other chat.* calls in this module (import_boundaries_test);
        # rebuilt every turn so /injected-context/ persists and accumulates across
        # turns on the guest's ephemeral tmpfs. Best-effort: a build failure must
        # not fail the run, the agent just runs without the extra context.
        injected_context: dict[str, str] = {}
        if discord_thread:
            from chat.api import build_injected_context

            try:
                # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
                injected_context = await asyncio.to_thread(
                    build_injected_context, discord_thread, tier
                )
            except Exception:
                logger.exception(
                    "goosecracker: build_injected_context failed for %s; "
                    "continuing without it",
                    session,
                )
```

Add the field to the `payload` dict (after the `sessionDb` line at :542):

```python
            "injectedContext": injected_context,
```

**Step 4: Verify**: defer to CI.

**Step 5: Commit**

```bash
git add projects/monolith/goosecracker/runner.py \
        projects/monolith/goosecracker/tests/runner_test.py
git commit -m "feat(goosecracker): build and inject the context bundle per turn (ADR 040)"
```

---

### Task 4: Recipe tells goose the directory exists (source-blind)

One generic line so goose discovers `/injected-context/` when present. The recipe names no source, keeping the recipe/substrate agnostic per ADR 040.

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml` (instructions block, after the `/workspace` sentence at :5-8)

**Step 1: Edit the instructions**

Insert after line 8 (the "dispatch ONE sub-recipe" sentence), before the "Route the task" line:

```yaml
  If a directory /injected-context/ exists, it holds context the caller staged
  for this task that is not in the repo and not in your own history (for example
  an earlier conversation). Read /injected-context/README.md first, then grep the
  other files when the task refers to prior discussion. Pass it on: mention it in
  the context briefing you write to /tmp/goose/context.md so the worker sub-recipe
  can use it too.
```

Keep the wording source-blind (no "Discord", no "conversation-only"), so the line stays correct for any future injector.

**Step 2: Verify**: defer to CI (the recipe is loaded/validated in the guest image build; a YAML error fails that build). No unit test.

**Step 3: Commit**

```bash
git add projects/firecracker/goosecracker/guest/recipes/agent.yaml
git commit -m "feat(goosecracker): point the agent recipe at /injected-context/ (ADR 040)"
```

---

### Task 5: Deploy plumbing (chart version bumps)

Code-only PRs deploy nothing in this repo, releases roll out by bumping chart versions so CI rebuilds the images (a manual step; the chart-version-bot is not reliable for this, ADR 009 is Draft). Two artifacts changed: the monolith (Python) and the goosecracker guest image (Go handler + `agent.yaml` recipe).

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (version) **and** `projects/monolith/deploy/application.yaml` (`targetRevision`): keep in sync.
- Modify: the goosecracker guest image chart. **Verify which chart owns the guest image build** (fc-invoke vs a goosecracker guest chart) before bumping. Memory is contradictory on whether guest/recipe changes auto-bump fc-invoke (#3042 said fixed, #3090 said still not bumping), so treat this as manual: find the chart whose image bakes `projects/firecracker/goosecracker/guest-init` + `guest/recipes`, bump its `Chart.yaml` version and matching `deploy/application.yaml` `targetRevision`.

**Step 1:** Run `format` (regenerates BUILD files and the home-cluster root kustomization) after all code changes:

```bash
format
```

**Step 2:** Bump the two charts (Chart.yaml + application.yaml each), following `feedback_chart_version_bumps`. Confirm no test asserts on a value you changed (none here: this feature adds no numeric config the tests pin).

**Step 3: Commit**

```bash
git add -A
git commit -m "build(goosecracker): bump monolith and guest image charts for injected context"
```

---

### Final: push, open PR, watch CI, follow through

1. Push the branch and open the PR:
   ```bash
   git push -u origin feat/injected-context
   gh pr create --fill --base main
   ```
2. One comprehensive end-of-PR code review (Opus reviewer reading the full diff) per the repo's review cadence, before relying on CI.
3. Watch CI: `gh pr checks <number> --watch`. On red, fetch the actual log via the BuildBuddy MCP tools (`get_invocation` by `commitSha` → `get_target` → `get_log`), quote the failure verbatim before hypothesising, and push fixes.
4. After green, merge with **rebase** (`gh pr merge --rebase`; squash is disabled).
5. Poll the rollout (ArgoCD app sync + the guest image / monolith rollout) to confirm the change is live.
6. **Discord-notify on merge** (user asked): once the PR is merged, send one `monolith-monolith-agent-notify` line stating the injected-context feature merged and is rolling out.

---

## Verification checklist (post-merge, live)

- In a Discord `#channel`, hold a short conversation, then `/agent` a question that references "our earlier conversation". Confirm the agent's answer uses the channel history (no "I don't have access to the prior conversation").
- Send a follow-up in the same agent thread; confirm the agent still has `/injected-context/` on turn 2/3 (persistence) and that new channel messages appear (evolution).
- Confirm a non-Discord (MCP-dispatched) run still works with an empty bundle (no regression).
