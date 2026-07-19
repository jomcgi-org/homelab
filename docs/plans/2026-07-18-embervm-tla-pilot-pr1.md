# EmberVM TLA+ Pilot PR1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Land PR1 of the ADR embervm/006 TLA+ pilot: the adoption-protocol PlusCal spec checked by TLC in CI, plus the layer-1 vocabulary sync test.

**Architecture:** A new `bazel/tla` prebuilt toolchain (tla2tools.jar + pinned Temurin JRE, same pattern as `bazel/erlang`'s prebuilt OTP) runs TLC in a genrule over `projects/embervm/specs/adoption.tla`. The spec models the control plane (dispatcher inventory + node registry health machine) against noded and an adversarial crash scheduler. Two negative-mode configs prove the model re-finds the two historical bugs (dispatch restart wedge, straggler resurrection). An ExUnit test asserts the spec's declared vocabulary partitions the real implementation enums (proto RPC verbs, health states, op-log kinds).

**Tech Stack:** TLA+/PlusCal, TLC (tla2tools.jar 1.8.0), Temurin JRE 21 (linux x64), Bazel genrules on BuildBuddy RBE, Elixir/ExUnit.

**Scope guard (PR2, NOT here):** layer-2 trace validation (op-log event -> TLA+ action translator, drill-trace TLC check) is deliberately excluded. Do not build it.

**Verification reality:** There is no local test loop in this repo (no local `bazel test`). TLC-checking the spec and running the ExUnit test happen on the pushed branch's CI. If a local `java` >= 11 happens to be available, implementers MAY sanity-run TLC locally against a downloaded tla2tools.jar, but CI is the arbiter.

---

## Context an implementer needs

- ADR: `docs/decisions/embervm/006-tla-formal-specification-pilot.md`. Read it first. PR1 = pilot spec (protocol 1: VM lifecycle + adoption) + layer-1 vocabulary test + TLC in CI.
- Toolchain pattern to copy: `bazel/erlang/repositories.bzl` (module extension fetching prebuilt tarballs at repo-fetch time; RBE executor is Ubuntu 22.04 x86_64 with network-less executors) and `bazel/erlang/BUILD` (`mix_test_smoke` genrule pattern; genrules ARE built by CI's `bazel test //...`).
- Implementation surfaces the spec models:
  - `projects/embervm/control/lib/embervm/dispatcher.ex`: `adopt_inventory/1` (line ~995), `put_vm_if_unknown/4` + `known_vm_ids/1` (idempotence guard including in-flight worker meta vm_ids), `handle_info({:vm_primed, ...})` (just-claimed miss VM enters known_vm_ids before Assign lands), boot sweep `handle_continue(:boot_sweep, ...)`, `run_sweep/1`.
  - `projects/embervm/control/lib/embervm/node_registry.ex`: health states `:starting/:healthy/:unknown/:down`, `evaluate_ages` + `apply_health_transition`, `handle_node_down/2` (forget-before-kill, comment at ~line 718), `forget_streamer/3`, capacity publish/retract fail-closed.
  - `projects/embervm/proto/embervm/node/v1/node.proto`: 23 `rpc` verbs (R0: BuildBase, Prime, Assign, Destroy, WatchNode, GetNodeStatus; R2: SessionAssign, Bank, Relight, EvictSnapshot; R3: StartServing, StopServing; R4: StartStateful, StopStateful, ResolveStateful, DeleteVolume; R5: CreateGroupNetwork, DeleteGroupNetwork, StartGroupMember, StopGroupMember; R6: ExportArtifact, RestoreArtifact, EvictArtifact).
  - `projects/embervm/control/lib/embervm/op_log.ex`: closed enum `@kinds`, public accessor `OpLog.kinds/0` (line ~206).
- Vocabulary test pattern to copy: `projects/embervm/control/test/embervm/task_state_test.exs` (exhaustive enum walks asserting table == implementation).
- Conventional Commits enforced by hook. NEVER use em-dashes in any prose. Run `bazel/tools/format/fast-format.sh` before each commit (the bare `format` shim is not on PATH in worktrees).
- No chart bump needed: nothing in this PR deploys (specs + CI + test-only Elixir accessor).

---

## Task 1: Bazel TLA toolchain (`bazel/tla/`)

**Files:**
- Create: `bazel/tla/repositories.bzl`
- Create: `bazel/tla/BUILD`
- Create: `bazel/tla/tlc.sh` (executable)
- Modify: `MODULE.bazel` (register extension, near the `erlang` extension at ~line 756)

**Step 1: Pin the artifacts**

Fetch and hash both artifacts (host has network):

```bash
curl -sLo /tmp/tla2tools.jar https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar
shasum -a 256 /tmp/tla2tools.jar
```

For the JRE, use the latest Temurin 21 GA linux x64 **JRE** tarball from https://api.adoptium.net/v3/assets/latest/21/hotspot (pick `image_type == "jre"`, `os == "linux"`, `architecture == "x64"`); record the exact release name, download URL, and verify its published sha256 by hashing the download yourself. TLC only needs a JRE (tla2tools.jar also bundles the PlusCal translator `pcal.trans`, which runs on the same JRE).

**Step 2: Write `bazel/tla/repositories.bzl`**

Copy the shape of `bazel/erlang/repositories.bzl`: a module docstring explaining WHY prebuilt (RBE executor is ubuntu-22.04 x86_64, no network at exec time, no rules_java in this repo; TLC is a build-time-only checker so single-arch linux-amd64 is by design, same as `@protoc_linux_x86_64`), then:

```starlark
load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

_JRE_BUILD = """
filegroup(
    name = "jre",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

exports_files(["bin/java"], visibility = ["//visibility:public"])
"""

def _tla_impl(_ctx):
    http_file(
        name = "tla2tools",
        urls = ["https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"],
        sha256 = "<pinned in Step 1>",
        downloaded_file_path = "tla2tools.jar",
    )
    http_archive(
        name = "temurin21_jre_linux_amd64",
        urls = ["<exact adoptium URL from Step 1>"],
        sha256 = "<pinned in Step 1>",
        strip_prefix = "<top-level dir inside the tarball, e.g. jdk-21.0.x+y-jre>",
        build_file_content = _JRE_BUILD,
    )

tla = module_extension(
    implementation = _tla_impl,
    doc = "Prebuilt TLC model checker (tla2tools.jar) and the Temurin 21 JRE that runs it on the linux-amd64 RBE executor. Build-time only; nothing deployed uses Java.",
)
```

**Step 3: Wire into `MODULE.bazel`**

Directly after the `erlang` `use_repo` block, add a short comment (TLC for the ADR embervm/006 spec checks) plus:

```starlark
tla = use_extension("//bazel/tla:repositories.bzl", "tla")
use_repo(tla, "temurin21_jre_linux_amd64", "tla2tools")
```

**Step 4: Write `bazel/tla/tlc.sh`**

Driver invoked from a genrule (copy the arg-handling conventions of `bazel/erlang/mix_test.sh`: absolutize execroot-relative paths before `cd`, `mktemp -d` workdir with EXIT trap, everything staged writable). Interface:

```
$1 java anchor (bin/java), $2 tla2tools.jar, $3 spec .tla, $4 cfg, $5 output marker,
$6 expectation: "pass" or "fail"
```

Behavior:
1. Stage the spec + cfg into the workdir (TLC writes states/ droppings next to the spec).
2. **Translation freshness check:** copy the .tla, run `java -cp tla2tools.jar pcal.trans -nocfg <copy>`, then `diff` against the committed file. A diff means someone edited the PlusCal block without retranslating: fail with a message telling them to run pcal.trans and commit the result. (pcal.trans rewrites in place between the `\* BEGIN TRANSLATION` / `\* END TRANSLATION` markers; committed specs carry the translation.)
3. Run TLC: `java -XX:+UseParallelGC -Dtlc2.TLC.stopAfter=600 -cp tla2tools.jar tlc2.TLC -workers auto -config <cfg> <spec>`; capture output to the marker file.
4. If expectation is `pass`: exit nonzero when TLC failed, and echo the TLC output to stderr. If `fail`: exit nonzero when TLC SUCCEEDED (the negative modes exist to prove the model still detects the historical bugs; a passing bug-mode run means the model went blind) and require the output to contain `Invariant` or `Temporal properties were violated` so a crash is not mistaken for a detection.

**Step 5: Write `bazel/tla/BUILD`**

Header comment pointing at ADR embervm/006. `exports_files(["tlc.sh"], visibility = ["//projects/embervm:__subpackages__"])` plus a `bzl_library` for `repositories.bzl` (copy `bazel/erlang/BUILD`'s). No smoke genrule here; Task 2's spec checks are the smoke.

**Step 6: Format and commit**

```bash
bazel/tools/format/fast-format.sh
git add bazel/tla MODULE.bazel
git commit -m "build(tla): prebuilt TLC toolchain (tla2tools.jar + Temurin 21 JRE) for ADR embervm/006"
```

Note: buildifier may reorder/reformat the .bzl; let it. CI verifies the fetch shas and that the genrules (Task 2) run; nothing to verify locally beyond format.

---

## Task 2: The adoption spec (`projects/embervm/specs/`)

**Files:**
- Create: `projects/embervm/specs/adoption.tla`
- Create: `projects/embervm/specs/adoption.cfg`
- Create: `projects/embervm/specs/adoption_wedge.cfg`
- Create: `projects/embervm/specs/adoption_resurrection.cfg`
- Create: `projects/embervm/specs/README.md`
- Create: `projects/embervm/specs/BUILD`

This is the judgment-heavy task. The model contract below is authoritative for WHAT to model; the implementer owns the PlusCal realization and iterates against TLC in CI until the three genrules are green.

**Step 1: Write the spec header prose map**

ADR requirement (risk table, "write-only specs"): the spec starts with a comment block mapping every PlusCal action to the module/function it abstracts, e.g. `AdoptInventory ~ Embervm.Dispatcher.adopt_inventory/1`, `NodeDownEdge ~ Embervm.NodeRegistry.handle_node_down/2`. Keep it current with the actions you actually write.

**Step 2: Write the PlusCal model**

Module `adoption`. EXTENDS Naturals, Sequences, FiniteSets.

CONSTANTS: `Nodes`, `VMs`, `Tasks`, `Principals`, `NULL`, and two boolean protocol switches: `AdoptionEnabled` (models `adopt_inventory` existing at all) and `ForgetBeforeKill` (models the D-R2.7.2 ordering).

State, node side (survives CP crash, wiped by node crash):
- `vmState[v]` in {"free", "primed", "assigned", "destroyed"}, `vmNode[v]`, `vmPrincipal[v]`.

State, channel (per node): `statusCh[n]`, a bounded FIFO (depth 2) of NodeStatus records `[gen |-> g, primed |-> set of vms, assigned |-> set of vms]`. Messages carry the streamer generation; this is how stragglers exist. Node crash clears the node's VMs but NOT in-flight messages (that is the straggler).

State, CP side (volatile unless noted):
- `cpAlive`; `health[n]` in {"starting", "healthy", "unknown", "down"}; `streamerGen[n]` (NULL when forgotten); `inventory` subset of Nodes X VMs (the primed pool); `inflightMeta` subset of VMs (just-claimed miss VMs: in known_vm_ids but not yet in inventory); `taskState[t]` (durable: mirrors the op-log's submitted/assigned/succeeded), `taskVM[t]`, `taskPrincipal[t]`.
- Bounded crash budget counters (`cpCrashes <= 1`, `nodeCrashes <= 1`) to contain the state space.

Actions (each an atomic PlusCal `either` branch or labeled step; the adversarial scheduler is just TLC's nondeterminism plus the crash actions):
- `Prime(n, v, p)`: CP asks node to prime under principal p; deposit path adds to inventory via the `PutVmIfUnknown` guard (skip when v in inventory-vms union inflightMeta).
- `SendStatus(n)`: node appends its current report tagged with the CURRENT streamerGen the node was connected under.
- `RecvStatus(n)`: CP pops; **guard: accept only if msg.gen = streamerGen[n] and streamerGen[n] /= NULL**, else drop silently (that guard IS forget-before-kill's mechanism; when `ForgetBeforeKill = FALSE`, model the buggy order by allowing acceptance when streamerGen is NULL but a stale gen matches the last known one, i.e. the kill happened but the pid was not forgotten first). On accept: health[n] := "healthy", and if `AdoptionEnabled` run `AdoptInventory` (PutVmIfUnknown for each reported primed vm).
- `DispatchWarm(t)`: pick (n, v) from inventory with health[n] = "healthy", vmPrincipal[v] = taskPrincipal[t]; remove from inventory, taskVM[t] := v, vmState[v] := "assigned".
- `DispatchMiss(t)`: no warm VM: prime a fresh v (vmState "primed"), add v to inflightMeta (the `:vm_primed` message), then assign it (vmState "assigned", remove from inflightMeta). Two labeled steps so adoption can interleave between them: this is the exact window `known_vm_ids` protects.
- `AgeToUnknown(n)` / `AgeToDown(n)`: health ticks. `AgeToDown` runs `NodeDownEdge`: forget (streamerGen[n] := NULL) and kill ordered per `ForgetBeforeKill`; then `ReassignNode(n)`: every task assigned on n goes back to "queued" (taskVM := NULL) and n's entries leave inventory, BEFORE any later adoption can re-add them (reassign-before-adopt).
- `Reap(n, v)`: CP destroys a vm it believes orphaned. Guard must ensure v is not in the node's LAST ACCEPTED report's primed or assigned sets and not in knownVMs. (The reap-would-wipe-fleet reviewer catch.) Track `lastReport[n]` for this.
- `CrashCP` / `RestartCP`: wipe volatile CP state (inventory, inflightMeta, health := "starting", streamerGen fresh gens); durable taskState survives; boot sweep re-queues in-flight tasks.
- `CrashNode(n)`: all of n's VMs destroyed; statusCh[n] retains in-flight messages.

Invariants (INVARIANT in cfg):
- `TypeOK`.
- `NoDoubleAssign`: no two live tasks share a VM, and every assigned task's VM has `vmState = "assigned"` on the node.
- `AdoptIdempotent`: no VM appears in inventory more than once, and never in both inventory and inflightMeta.
- `NoResurrection`: `streamerGen[n] = NULL => health[n] /= "healthy"` (a forgotten node can only become healthy again via a fresh-generation status).
- `NoReapLive`: a destroyed-by-reap VM was never in the node's current primed/assigned sets at the moment of the reap (encode as an action guard violation flag or a history variable).
- `PrincipalIsolation`: `taskVM[t] = v => vmPrincipal[v] = taskPrincipal[t]` (ADR 001's no-cross-principal rule).

Temporal property (PROPERTY in cfg, with fairness on dispatch/status/recv actions):
- `EventuallyDispatched`: every submitted task eventually reaches "assigned" or a terminal state, ACROSS a CP crash-restart. This is the dispatch-restart-wedge property: with `AdoptionEnabled = FALSE` it must fail (the historical wedge), with TRUE it must hold.

**Step 3: Translate**

Run `pcal.trans` (locally if java is available, else lean on the Task 1 driver's freshness diff in CI to tell you the committed translation is stale; practically: install nothing, write the translation by running TLC in CI is miserable, so DO try `java -cp /tmp/tla2tools.jar pcal.trans specs/adoption.tla` with the Task 1 jar first; any JRE >= 11 works, macOS included).

**Step 4: Write the three cfgs**

- `adoption.cfg`: bounds per the ADR: 2 nodes, 3 VMs, 2 tasks, 2 principals; `AdoptionEnabled = TRUE`, `ForgetBeforeKill = TRUE`; all invariants + the temporal property. Expectation: pass.
- `adoption_wedge.cfg`: same but `AdoptionEnabled = FALSE`, ONLY the temporal property. Expectation: fail (re-finds the restart wedge).
- `adoption_resurrection.cfg`: same as adoption.cfg but `ForgetBeforeKill = FALSE`, ONLY `NoResurrection` (drop the temporal property to keep it fast). Expectation: fail (re-finds the straggler resurrection).

**Step 5: Write `projects/embervm/specs/BUILD`**

Three genrules (`tlc_adoption`, `tlc_adoption_wedge`, `tlc_adoption_resurrection`) invoking `//bazel/tla:tlc.sh` with `@temurin21_jre_linux_amd64//:bin/java`, `@tla2tools//file`, the spec, the cfg, the out marker, and the expectation string. NO `no-remote-cache` tag: the check is deterministic, so cache hits mean full TLC re-runs only when the spec or toolchain changes, which is exactly the ADR's cost model. Also a `filegroup(name = "spec_files", srcs = glob(["*.tla", "*.cfg", "vocabulary.exs"]))` with `//projects/embervm:__subpackages__` visibility (Task 3 consumes it; include `vocabulary.exs` in the glob now so Task 3 does not touch this file).

**Step 6: Write `projects/embervm/specs/README.md`**

Short: what lives here (per ADR embervm/006), the action->module prose map duplicated from the spec header or pointed at, how the three cfgs differ, how the negative modes work, and that PR2 (trace validation) is future work. No em-dashes.

**Step 7: Format, commit**

```bash
bazel/tools/format/fast-format.sh
git add projects/embervm/specs
git commit -m "feat(embervm): adoption-protocol TLA+ spec with TLC checks in CI (ADR embervm/006 pilot)"
```

---

## Task 3: Layer-1 vocabulary sync test

**Files:**
- Create: `projects/embervm/specs/vocabulary.exs`
- Create: `projects/embervm/control/test/embervm/spec_vocabulary_test.exs`
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex` (add enum accessor)
- Modify: `bazel/erlang/BUILD` (`mix_test_smoke` srcs) and `bazel/erlang/mix_test.sh` (stage specs + proto)
- Check: `projects/embervm/proto/embervm/node/v1/BUILD` exposes `node.proto` as a readable file to the test genrule (add an `exports_files`/`filegroup` if the existing codegen targets do not already)

**Step 1: Add the health-state enum accessor**

In `node_registry.ex`, alongside the existing module attributes:

```elixir
@health_states [:starting, :healthy, :unknown, :down]

@doc "Closed enum of node health states, exposed for the spec vocabulary sync test (ADR embervm/006 layer 1)."
@spec health_states() :: [atom()]
def health_states, do: @health_states
```

Grep the module and confirm no health literal exists outside this list; wire nothing else (behavior unchanged).

**Step 2: Write the manifest `projects/embervm/specs/vocabulary.exs`**

An `Code.eval_file`-able map declaring, per surface, what the adoption spec models vs deliberately excludes. Every current enum member must appear in exactly one bucket; the ADR's out-of-scope list (serving, stateful, groups, continuity, FaaS) drives the exclusions:

```elixir
%{
  proto_rpcs: %{
    modeled: ~w(Prime Assign Destroy WatchNode GetNodeStatus)a,
    excluded: ~w(BuildBase SessionAssign Bank Relight EvictSnapshot StartServing StopServing
                 StartStateful StopStateful ResolveStateful DeleteVolume CreateGroupNetwork
                 DeleteGroupNetwork StartGroupMember StopGroupMember ExportArtifact
                 RestoreArtifact EvictArtifact)a
  },
  health_states: %{modeled: ~w(starting healthy unknown down)a, excluded: []},
  op_kinds: %{
    modeled: ~w(submitted assigned primed vm_destroyed)a,
    excluded: [] # implementer: fill with ALL remaining OpLog.kinds/0 members, grouped by rung with a comment each
  }
}
```

(Adjust `modeled` to match what the Task 2 spec actually models; the test's cross-check against `adoption.tla` text will force honesty.)

**Step 3: Write the failing test**

`spec_vocabulary_test.exs`, `async: true`, module doc citing ADR embervm/006 layer 1. Locate inputs:

```elixir
specs_dir = System.get_env("EMBERVM_SPECS_DIR", Path.expand("../specs", File.cwd!()))
proto = System.get_env("EMBERVM_NODE_PROTO", Path.expand("../proto/embervm/node/v1/node.proto", File.cwd!()))
```

(`File.cwd!()` during `mix test` is `control/`; the defaults work in a plain repo checkout, the env vars override inside the Bazel sandbox.)

Assertions, one test per surface plus one freshness test:
1. `proto_rpcs`: parse verbs with `Regex.scan(~r/^\s*rpc\s+(\w+)\s*\(/m, File.read!(proto))`; assert `MapSet.new(modeled ++ excluded) == actual` AND `modeled -- excluded intersection empty` (disjoint buckets). A new rpc lands in neither bucket and fails with a message telling the author to classify it in `vocabulary.exs` (model it or exclude it), per the ADR.
2. `health_states` vs `Embervm.NodeRegistry.health_states()`.
3. `op_kinds` vs `Embervm.OpLog.kinds()`.
4. Freshness: every `modeled` atom's string appears in `File.read!(specs_dir <> "/adoption.tla")` (the prose map + actions must mention each). Case-sensitive contains is enough.

**Step 4: Run it red, mentally**

No local test loop; instead verify by inspection that with an empty `op_kinds.excluded` the test MUST fail (there are ~60 kinds). Fill the exclusions to green it. This ordering proves the test bites.

**Step 5: Stage spec + proto into the mix test sandbox**

`bazel/erlang/BUILD` `mix_test_smoke`: add `//projects/embervm/specs:spec_files` and the node.proto file to `srcs`, and pass them via env in `cmd` following the `MIX_REBAR3_SRC` optional-env pattern:

```
EMBERVM_SPECS_SRCS="$(locations //projects/embervm/specs:spec_files)" EMBERVM_NODE_PROTO_SRC="$(location //projects/embervm/proto/embervm/node/v1:node.proto)" ...
```

`mix_test.sh`: when `EMBERVM_SPECS_SRCS` is set, absolutize each path, `mkdir -p "$work/specs"`, copy them in, `export EMBERVM_SPECS_DIR="$work/specs"`; same for the proto to `$work/proto/node.proto` + `export EMBERVM_NODE_PROTO`. Keep both optional (absent leaves behavior unchanged), matching the rebar3 precedent.

**Step 6: Format, commit**

```bash
bazel/tools/format/fast-format.sh
git add projects/embervm/specs/vocabulary.exs projects/embervm/control bazel/erlang projects/embervm/proto
git commit -m "test(embervm): layer-1 spec vocabulary sync test (ADR embervm/006)"
```

---

## Task 4: Docs note, PR, CI, merge

**Step 1:** Add a short DECISIONS.md entry (repo root, follow existing entry style) recording the pilot's PR1 landing: adoption spec + negative-mode self-checks + layer-1 guard; trace validation deferred to PR2.

**Step 2:** `bazel/tools/format/fast-format.sh`, commit `docs(embervm): record TLA+ pilot PR1 in DECISIONS.md`.

**Step 3:** Push branch, open PR titled `feat(embervm): TLA+ adoption spec pilot, PR1 of ADR embervm/006` with a body summarizing the three genrules, the two negative modes, and the layer-1 test. End the body with the standard generated-with footer.

**Step 4:** One comprehensive Opus code review of the full diff (per repo review cadence), fix findings, then watch `gh pr checks <n> --watch`. Iterate on CI failures via `mcp__buildbuddy__get_invocation` (commitSha selector) + `get_log`; quote failures verbatim before hypothesizing. Expect iteration on the spec itself: TLC counterexamples come back through CI logs; read the error trace, fix the model or discover a real protocol gap (if the latter: STOP and surface it to Joe, that is a pilot success finding, not a spec bug to paper over).

**Step 5:** Merge with `gh pr merge --rebase` once green (update branch first if `BEHIND`). No rollout to poll: nothing deploys.
