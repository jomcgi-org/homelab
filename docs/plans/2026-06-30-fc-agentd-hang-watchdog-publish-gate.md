# fc-agentd Hang Watchdog + Publish Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop two classes of silently-stuck agent runs: a goose process wedged mid-turn (the VM stays alive, the build counter freezes forever), and a run that finishes cleanly but never produced an artifact (marked COMPLETED with nothing to show).

**Architecture:** Two changes that share one foundation. (1) The daemon's `OnDone` handler currently throws away the `status`/`result` the guest already sends and always marks `COMPLETED`; make it classify the outcome (errored / timed out / no-artifact -> FAILED with a Discord failure message; genuine success -> COMPLETED). (2) Add an in-guest progress watchdog to `fc-agent-init`: it tees goose's output through an activity monitor and, if goose produces no output for a configurable stall window, kills goose and reports `Done{status:"timeout"}`, which flows through the same classifier. Feature B (no-artifact-as-failure) falls out of the foundation; Feature A (hang detection) is the watchdog plus the `timeout` status.

**Tech Stack:** Go. Daemon: `projects/agent_platform/fc-agentd/internal/reconcile`. Guest harness: `projects/agent_platform/fc-agent-init`. Control protocol: `projects/agent_platform/vsockproto`. No new dependencies.

**Context for the implementer (read first):**

- There is **no local test loop** in this repo. Do NOT run `go test` / `bazel test` from the workstation. For each task, verify with `go build ./projects/agent_platform/...` and `go vet ./<changed-package>/...`, then **defer test execution to end-of-plan CI** on the pushed branch. The "Run the test" steps below are written so CI runs them; locally you confirm compile + vet only.
- Commit after each task with Conventional Commits. Never use em-dashes in any prose, comment, or message.
- This work builds on PRs #2933 (stale-vsock unlink) and #2939 (host-side death reaper). #2939 added the `died` channel + `reapDied` + a Discord failure-notify pattern; reuse that pattern, do not duplicate it.

**Key facts established during design (cite these, do not re-derive):**

- Guest already sends everything Feature B needs: `fc-agent-init` sends `Done{Status, Result}` where `Status` is `"ok"` on goose exit 0 or the error string otherwise, and `Result` is the published artifact URL or `""` when `/tmp/artifact.html` did not exist (`projects/agent_platform/fc-agent-init/cmd/main.go:199-203`, `:554-562`).
- Daemon `OnDone` ignores `status` and always `SetState(COMPLETED)` (`projects/agent_platform/fc-agentd/internal/reconcile/reconcile.go:320-343`).
- "Artifact was expected" is knowable daemon-side: the per-thread env (`threadEnv` in `startControl`) carries `ARTIFACT_PUBLISH_URL` only for the artifact tier.
- Guest tees goose stdout/stderr through an `io.MultiWriter` (`main.go:151-166`); the progress streamer is one writer on that tee and may be nil for non-artifact tiers (`main.go:149`). The watchdog must observe activity independently of the progress streamer so it works for every tier.
- The idle detector (`projects/agent_platform/fc-agent-init/internal/idle`) uses an injectable `now func()` clock and a `Run(ctx, interval, onIdle)` loop; mirror that shape for the watchdog so it is unit-testable without real time.
- `goose run --recipe ...` executes and exits (it is not an interactive session), so for the artifact tier there is no legitimate "goose alive but idle" period; sustained zero output while goose is running means wedged.

---

## Task 1: Daemon classifies the harness outcome (foundation + Feature B)

Make `OnDone` honor `status`/`result`. This delivers no-artifact-as-failure immediately and is the landing point for the watchdog's `timeout` status in Task 4.

**Files:**

- Modify: `projects/agent_platform/fc-agentd/internal/reconcile/reconcile.go` (the `OnDone` handler inside `startControl`, ~lines 311-343; and extract a shared failure-notify helper reused by `reapDied`)
- Test: `projects/agent_platform/fc-agentd/internal/reconcile/reconcile_test.go`

**Step 1: Write the failing tests**

Add to `reconcile_test.go`. These drive the `OnDone` classifier directly. Because `OnDone` is a closure built inside `startControl` (which needs a real vsock), refactor the classification into a testable method `l.onHarnessDone(ctx, log, doneInfo)` and call it from both the closure and the tests. `doneInfo` is a small struct: `{threadID, discordThread, progressURL string; artifactExpected bool; status, result string}`.

```go
func TestOnHarnessDoneSuccessCompletes(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StateRunning, Node: "node-4", DiscordThread: "d-1"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.live["t1"] = substrate.Handle{ThreadID: "t1", ID: "vm-t1", Node: "node-4"}

	l.onHarnessDone(context.Background(), testLogger(), doneInfo{
		threadID: "t1", discordThread: "d-1", artifactExpected: true, status: "ok", result: "https://art/abc",
	})

	if reg.state("t1") != substrate.StateCompleted {
		t.Fatalf("state = %q, want COMPLETED", reg.state("t1"))
	}
	if len(reg.outbox) != 1 || reg.outbox[0].content == "" || reg.outbox[0].content[:13] != "Artifact ready" {
		t.Fatalf("want one 'Artifact ready' outbox row, got %+v", reg.outbox)
	}
}

func TestOnHarnessDoneErrorStatusFails(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StateRunning, Node: "node-4", DiscordThread: "d-1"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.live["t1"] = substrate.Handle{ThreadID: "t1", ID: "vm-t1", Node: "node-4"}

	l.onHarnessDone(context.Background(), testLogger(), doneInfo{
		threadID: "t1", discordThread: "d-1", artifactExpected: true, status: "exit status 1", result: "",
	})

	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("state = %q, want FAILED", reg.state("t1"))
	}
	if len(reg.outbox) != 1 {
		t.Fatalf("want one failure outbox row, got %+v", reg.outbox)
	}
}

func TestOnHarnessDoneNoArtifactFails(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StateRunning, Node: "node-4", DiscordThread: "d-1"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.live["t1"] = substrate.Handle{ThreadID: "t1", ID: "vm-t1", Node: "node-4"}

	// goose exited 0 but published nothing on an artifact-tier thread.
	l.onHarnessDone(context.Background(), testLogger(), doneInfo{
		threadID: "t1", discordThread: "d-1", artifactExpected: true, status: "ok", result: "",
	})

	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("state = %q, want FAILED (no artifact)", reg.state("t1"))
	}
}

func TestOnHarnessDoneNonArtifactTierSucceedsWithoutResult(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StateRunning, Node: "node-4"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.live["t1"] = substrate.Handle{ThreadID: "t1", ID: "vm-t1", Node: "node-4"}

	// A non-artifact tier legitimately produces no artifact URL.
	l.onHarnessDone(context.Background(), testLogger(), doneInfo{
		threadID: "t1", artifactExpected: false, status: "ok", result: "",
	})

	if reg.state("t1") != substrate.StateCompleted {
		t.Fatalf("state = %q, want COMPLETED", reg.state("t1"))
	}
	if len(reg.outbox) != 0 {
		t.Fatalf("non-artifact success should not enqueue Discord output, got %+v", reg.outbox)
	}
}
```

**Step 2: Run tests to verify they fail**

Run: `go build ./projects/agent_platform/fc-agentd/...`
Expected: FAIL to compile (`doneInfo` and `onHarnessDone` undefined). That is the failing-test state for this repo (compile gate). Do not run `go test`.

**Step 3: Implement the classifier**

In `reconcile.go`, add the `doneInfo` type and `onHarnessDone` method, and a shared `notifyBuildFailed` helper. Refactor the existing `reapDied` (from #2939) to call `notifyBuildFailed` so the failure-notify path is DRY.

```go
// doneInfo is the harness's terminal report, normalised for classification.
type doneInfo struct {
	threadID         string
	discordThread    string
	progressURL      string
	artifactExpected bool // the thread's tier injects ARTIFACT_PUBLISH_URL
	status           string
	result           string
}

// onHarnessDone classifies a guest's Done report and records the terminal state.
// The guest already distinguishes success (status "ok") from failure (the goose
// error string, or "timeout" from the in-guest watchdog) and reports the
// published artifact URL in result; the daemon honours that instead of assuming
// success. A run is successful only when goose exited cleanly AND, for a tier
// that was meant to publish an artifact, it actually produced one. Everything
// else is a failure the user should see, not a COMPLETED with nothing to show.
func (l *Loop) onHarnessDone(ctx context.Context, log *slog.Logger, d doneInfo) {
	failed, reason := classifyDone(d)
	if failed {
		log.Warn("reconcile: harness reported failure", "thread", d.threadID, "status", d.status, "reason", reason)
		if err := l.Registry.SetState(ctx, d.threadID, substrate.StateFailed); err != nil {
			log.Error("reconcile: mark failed on done", "thread", d.threadID, "err", err)
		}
		l.notifyBuildFailed(ctx, log, d.discordThread, d.progressURL, "Build failed: "+reason)
		return
	}
	if err := l.Registry.SetState(ctx, d.threadID, substrate.StateCompleted); err != nil {
		log.Error("reconcile: mark completed on done", "thread", d.threadID, "err", err)
	}
	// Close the live build message even when the guest could not (VM torn down
	// before goose returned). Idempotent; safe to double-send.
	go postProgressDone(context.WithoutCancel(ctx), log, d.progressURL, d.discordThread)
	if d.discordThread != "" && d.result != "" {
		if err := l.Registry.EnqueueDiscordOutbox(ctx, d.discordThread, "Artifact ready: "+d.result); err != nil {
			log.Error("reconcile: enqueue discord outbox on done", "thread", d.threadID, "err", err)
		}
	}
}

// classifyDone returns whether the run failed and a human reason for Discord.
func classifyDone(d doneInfo) (failed bool, reason string) {
	switch {
	case d.status == "timeout":
		return true, "the agent stopped making progress and was timed out."
	case d.status != "ok":
		return true, "the agent exited with an error."
	case d.artifactExpected && d.result == "":
		return true, "the agent finished without producing an artifact."
	default:
		return false, ""
	}
}

// notifyBuildFailed flips the live build message out of its spinning state and
// posts a failure note to the Discord thread, so a stuck or empty run shows a
// terminal result instead of a frozen counter. Shared by onHarnessDone and
// reapDied. postProgressDone is best-effort with its own timeout; run it off the
// loop goroutine so a slow monolith never stalls reconciliation.
func (l *Loop) notifyBuildFailed(ctx context.Context, log *slog.Logger, discordThread, progressURL, message string) {
	go postProgressDone(context.WithoutCancel(ctx), log, progressURL, discordThread)
	if discordThread == "" {
		return
	}
	if err := l.Registry.EnqueueDiscordOutbox(ctx, discordThread, message); err != nil {
		log.Error("reconcile: enqueue discord outbox on failure", "thread", discordThread, "err", err)
	}
}
```

Then rewrite the `OnDone` closure in `startControl` to delegate (it keeps `doneFired` for the death-reaper interplay from #2939):

```go
OnDone: func(threadID, status, result string) {
	doneFired.Store(true)
	log.Info("reconcile: thread harness done", "thread", threadID, "status", status, "result", result)
	l.onHarnessDone(context.WithoutCancel(cctx), log, doneInfo{
		threadID:         threadID,
		discordThread:    t.DiscordThread,
		progressURL:      progressURL,
		artifactExpected: threadEnv["ARTIFACT_PUBLISH_URL"] != "",
		status:           status,
		result:           result,
	})
},
```

And refactor `reapDied` (from #2939) to reuse the helper:

```go
// inside reapDied, replace the inline postProgressDone + EnqueueDiscordOutbox with:
l.notifyBuildFailed(ctx, log, ev.discordThread, ev.progressURL, "Build failed: the agent stopped before producing an artifact.")
```

**Step 4: Run tests to verify they pass**

Run: `go build ./projects/agent_platform/fc-agentd/...` then `go vet ./projects/agent_platform/fc-agentd/internal/reconcile/...`
Expected: both clean. Unit tests run in end-of-plan CI.

**Step 5: Commit**

```bash
git add projects/agent_platform/fc-agentd/internal/reconcile/
git commit -m "feat(fc-agentd): honor harness Done status and fail runs with no artifact"
```

---

## Task 2: Watchdog activity monitor (pure, unit-tested)

A self-contained package that tracks "time since last goose output" with an injectable clock, plus a `Run` loop that fires a callback once when the stall window is exceeded. Mirrors `internal/idle`.

**Files:**

- Create: `projects/agent_platform/fc-agent-init/internal/watchdog/watchdog.go`
- Test: `projects/agent_platform/fc-agent-init/internal/watchdog/watchdog_test.go`

**Step 1: Write the failing tests**

```go
package watchdog

import (
	"context"
	"testing"
	"time"
)

func TestWriteMarksActivity(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	if _, err := w.Write([]byte("goose output")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	now = now.Add(30 * time.Second)
	if got := w.IdleFor(); got != 30*time.Second {
		t.Fatalf("IdleFor = %s, want 30s", got)
	}
}

func TestStalledReportsAfterWindow(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	_, _ = w.Write([]byte("x")) // first activity
	now = now.Add(59 * time.Second)
	if w.Stalled() {
		t.Fatal("should not be stalled before the window elapses")
	}
	now = now.Add(2 * time.Second) // 61s since last write
	if !w.Stalled() {
		t.Fatal("should be stalled after the window elapses")
	}
	// Fresh output re-arms it.
	_, _ = w.Write([]byte("y"))
	if w.Stalled() {
		t.Fatal("a write should re-arm the monitor")
	}
}

func TestRunFiresOnceOnStall(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	_, _ = w.Write([]byte("x"))

	fired := make(chan struct{}, 4)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// Drive Run with a tiny real interval but a controlled clock: advance the
	// clock past the window, then assert the callback fires exactly once.
	now = now.Add(2 * time.Minute)
	go w.Run(ctx, 5*time.Millisecond, func() { fired <- struct{}{} })

	select {
	case <-fired:
	case <-time.After(time.Second):
		t.Fatal("onStall never fired")
	}
	// It must not fire repeatedly for the same stall.
	select {
	case <-fired:
		t.Fatal("onStall fired more than once for one stall")
	case <-time.After(50 * time.Millisecond):
	}
}
```

**Step 2: Run to verify it fails**

Run: `go build ./projects/agent_platform/fc-agent-init/...`
Expected: FAIL to compile (`watchdog` package does not exist).

**Step 3: Implement the monitor**

```go
// Package watchdog detects a wedged agent run: goose stops producing output
// while it is still supposed to be working (e.g. a model or MCP call hangs).
// The harness tees goose's stdout/stderr through Monitor, so any output re-arms
// it; Run fires onStall once when output has been silent for StallAfter. The
// clock is injectable so the decision is unit-testable without real time.
package watchdog

import (
	"context"
	"sync"
	"time"
)

type Monitor struct {
	// StallAfter is the silence window after which a run is considered wedged.
	StallAfter time.Duration

	now  func() time.Time // injectable clock; nil => time.Now
	mu   sync.Mutex
	last time.Time
	done bool // onStall already fired for the current stall
}

func (m *Monitor) clock() time.Time {
	if m.now != nil {
		return m.now()
	}
	return time.Now()
}

// Write records activity (it is an io.Writer so it can sit on the goose output
// tee) and re-arms the monitor. It never consumes or copies the bytes.
func (m *Monitor) Write(b []byte) (int, error) {
	m.mu.Lock()
	m.last = m.clock()
	m.done = false
	m.mu.Unlock()
	return len(b), nil
}

// IdleFor reports how long output has been silent. Zero before the first write.
func (m *Monitor) IdleFor() time.Duration {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.last.IsZero() {
		return 0
	}
	return m.clock().Sub(m.last)
}

// Stalled reports whether the silence window has elapsed since the last write.
func (m *Monitor) Stalled() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return !m.last.IsZero() && m.clock().Sub(m.last) >= m.StallAfter
}

// Run polls every interval and calls onStall exactly once per stall (re-armed by
// the next Write). It returns when ctx is cancelled. The first Write should
// happen before or early in the run; until then the monitor is unarmed.
func (m *Monitor) Run(ctx context.Context, interval time.Duration, onStall func()) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.mu.Lock()
			fire := !m.last.IsZero() && !m.done && m.clock().Sub(m.last) >= m.StallAfter
			if fire {
				m.done = true
			}
			m.mu.Unlock()
			if fire {
				onStall()
			}
		}
	}
}
```

**Step 4: Verify**

Run: `go build ./projects/agent_platform/fc-agent-init/...` then `go vet ./projects/agent_platform/fc-agent-init/internal/watchdog/...`
Expected: both clean. Tests run in CI.

**Step 5: Commit**

```bash
git add projects/agent_platform/fc-agent-init/internal/watchdog/
git commit -m "feat(fc-agent-init): add goose-output stall monitor"
```

---

## Task 3: Wire the watchdog into fc-agent-init

Tee goose output through the monitor (independent of the progress streamer so it works for every tier), run the watchdog, and on stall kill goose and report `Done{status:"timeout"}`.

**Files:**

- Modify: `projects/agent_platform/fc-agent-init/cmd/main.go` (goose launch + wait + Done, ~lines 149-207)
- Reference: `projects/agent_platform/fc-agent-init/internal/idle/idle.go` (the `Run` loop + `durationEnv` pattern)

**Step 1: Read the current launch/wait block** (`main.go:149-207`) so the edit is precise; it changes how goose's `Stdout`/`Stderr` writers are composed and how the exit status is derived.

**Step 2: Add the stall window config near the idle config** (`main.go:74`):

```go
// FC_STALL_AFTER bounds how long goose may produce no output before the run is
// treated as wedged and killed. It must be larger than the longest legitimate
// quiet stretch (a slow model/MCP call) but small enough that a hung run fails
// in minutes, not at the 24h TTL. Default 10m, matching the idle window.
stallAfter := durationEnv("FC_STALL_AFTER", 10*time.Minute)
```

**Step 3: Compose the watchdog into the output tee and run it.** Replace the goose-launch writer composition (`main.go:151-166`) so the monitor is always on the tee:

```go
mon := &watchdog.Monitor{StallAfter: stallAfter}
gooseCtx, killGoose := context.WithCancel(ctx)
defer killGoose()
var timedOut atomic.Bool

if len(harnessArgv) > 0 {
	harnessProc = exec.CommandContext(gooseCtx, harnessArgv[0], harnessArgv[1:]...)
	writers := []io.Writer{os.Stdout, mon}
	if pw != nil {
		writers = append(writers, pw)
		go pw.flushLoop(ctx, logger)
	}
	out := io.MultiWriter(writers...)
	harnessProc.Stdout = out
	harnessProc.Stderr = out
	harnessProc.Env = os.Environ()
	if err := harnessProc.Start(); err != nil {
		return err
	}
	go mon.Run(gooseCtx, 15*time.Second, func() {
		timedOut.Store(true)
		logger.Warn("watchdog: goose produced no output within stall window; killing", "stall_after", stallAfter.String())
		killGoose()
	})
}
```

**Step 4: Derive the Done status from the timeout flag.** In the wait/exit block (`main.go:199-203`), set `status` to `"timeout"` when the watchdog fired, so the daemon's classifier (Task 1) maps it to FAILED with the right message:

```go
status := "ok"
switch {
case timedOut.Load():
	status = "timeout"
case err != nil:
	status = err.Error()
}
```

**Step 5: Add imports** to `main.go`: `"sync/atomic"` and the watchdog package path `"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/watchdog"`.

**Step 6: Verify**

Run: `go build ./projects/agent_platform/fc-agent-init/...` then `go vet ./projects/agent_platform/fc-agent-init/...`
Expected: both clean.

**Step 7: Commit**

```bash
git add projects/agent_platform/fc-agent-init/cmd/main.go
git commit -m "feat(fc-agent-init): kill goose and report timeout on output stall"
```

---

## Task 4: Update BUILD files and confirm the whole module compiles

The new `watchdog` package needs a BUILD target; `format` (gazelle) generates it.

**Files:**

- Generated: `projects/agent_platform/fc-agent-init/internal/watchdog/BUILD.bazel`
- Possibly modified: `projects/agent_platform/fc-agent-init/cmd/BUILD.bazel` (new dep edge)

**Step 1:** Run `format` (vendored; regenerates BUILD files + formats). If `format` is not on PATH, run `gofmt -w` on the changed files and `buildozer`/`gazelle` via the repo's wrapper, or rely on CI's `ci-format-bot` to add the BUILD target on the PR branch.

**Step 2: Verify the module builds**

Run: `go build ./projects/agent_platform/...`
Expected: clean.

**Step 3: Commit** (only if `format` changed files)

```bash
git add projects/agent_platform/fc-agent-init/
git commit -m "build(fc-agent-init): wire watchdog package into bazel"
```

---

## Task 5: Push, CI, and rollout

**Step 1:** Push the branch and open a PR titled `feat(fc-agentd): hang watchdog + publish gate`. Body should cite incidents 1 (goose wedge) and 2 (no-artifact COMPLETED), summarise the two changes, and note the deferred daemon-side heartbeat (the `KindHeartbeat` plumbing exists if guest/kernel-lockup coverage is wanted later).

**Step 2:** Watch CI with `gh pr checks <n> --watch`. The gating `Test` job runs the new unit tests (`onHarnessDone` classifier, `watchdog.Monitor`). Read failures via the `mcp__buildbuddy__*` tools; quote the assertion before hypothesising.

**Step 3:** Do NOT auto-merge (consistent with the recent fc-agentd PRs). Hand the green PR back for a merge decision.

**Step 4 (post-merge):** The `chart-version-bot` bumps `fc-agentd` `Chart.yaml` + `application.yaml`; CI rebuilds the dual-arch image (this changes both `fc-agentd` and `fc-agent-init`, which ship in the same image) and ArgoCD syncs. Verify on the next artifact run: a deliberately empty run flips the Discord message to the failure note; a wedged run fails within `FC_STALL_AFTER` instead of hanging.

---

## Out of scope (deliberate)

- **Daemon-side heartbeat watchdog** (guest/kernel lockup with the vsock connection still up). The in-guest progress watchdog covers the goose-wedge incident; the connection-drop reaper (#2939) covers ungraceful VM death. A heartbeat would add coverage for the rare "VM and connection alive but guest frozen" case. The `KindHeartbeat` wire type already exists, and Task 1's classifier + the `died` channel are the landing points, so this is a small follow-up if it proves needed.
- **inFlight-call-duration signal** as an alternative to output-silence. Output-silence is simpler and tier-agnostic; revisit only if a legitimate long quiet stretch (a deliberately slow tool) trips the watchdog, in which case raise `FC_STALL_AFTER` for that tier first.
