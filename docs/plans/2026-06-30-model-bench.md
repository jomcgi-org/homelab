# model-bench Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `projects/model-bench/`, an internal Python harness that screens OpenRouter models against a curated pack of tasks distilled from this repo's real commits, and reports a coarse Pareto shortlist of a budget tier that clears our bar on the offloadable task classes.

**Architecture:** A Python CLI (`bench`) runs a `task x model` grid. Each cell is a 2-shot loop: the model emits full-file replacements, a deterministic effect-asserting verifier grades shot 1, and on failure the verifier's real stderr feeds one retry. Results are content-hash-cached JSON cells committed in-repo; a report renders per-class qualification against calibration anchors as a coarse Pareto frontier plus a retired-model tombstone table. Verifiers run in a credential-scrubbed sandbox.

**Tech Stack:** Python 3, `pydantic` (schemas), `httpx` (async OpenRouter client), `pyyaml` (task/registry parsing), vendored `helm`/`go`/`python`/`ruff`/`buildifier` (verifiers), Bazel `aspect_rules_py` + `py_test` (CI-run unit tests), OpenRouter chat-completions API.

**Design doc:** `docs/plans/2026-06-30-model-bench-design.md` (read first; it records the rationale and two deliberate overrides).

## Repo conventions this plan follows

- **No local test loop.** Do NOT run `bazel test` / `pytest` on the workstation. Each task writes its test alongside the code and self-reviews; **all test execution is deferred to Task 14 (end-of-plan CI)** on the pushed branch. This overrides the generic TDD "run the test" steps.
- **BUILD files are generated.** After adding/renaming Python files, run `format` (gazelle) to regenerate `BUILD` files and update BUILD deps; never hand-write `py_library`/`py_test` targets unless gazelle cannot infer them.
- **Deps.** `@pip//httpx` and `@pip//pydantic` already exist in `bazel/requirements/all.txt`. Task 1 confirms/adds `@pip//pyyaml`.
- **Tests** load `py_test` from `//bazel/tools/pytest:defs.bzl`; libraries use `py_library` from `@aspect_rules_py//py:defs.bzl`; the CLI uses `py_venv_binary`. gazelle wires these.
- **Commits:** Conventional Commits, no em-dashes, `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer. Frequent, one per task.
- **Vendored tools** are on PATH after `./bootstrap.sh` + `direnv allow` (already active in this worktree).

## Module map (target end state)

```
projects/model-bench/
  README.md
  models.yaml                       # registry (Task 12)
  bench/
    __init__.py
    schema.py                       # Task 2: pydantic models
    cache.py                        # Task 3: content-hash cell keys + skip/force + drift
    sandbox.py                      # Task 4: credential-scrubbed subprocess runner
    verifiers/
      __init__.py                   # Task 5: dispatch by verifier "kind"
      command.py                    # Task 5: raw command verifier
      helm.py                       # Task 5: helm-template render + field assert
      compile.py                    # Task 5: go build / python compile
      lint.py                       # Task 6: ruff / buildifier
      rbac.py                       # Task 6: parse handler calls, assert ClusterRole covers
      jsonmatch.py                  # Task 6: structured-answer match
    judge.py                        # Task 7: hardened free-text LLM judge
    openrouter.py                   # Task 8: async client, usage, pricing, latency
    runner.py                       # Task 9: 2-shot loop, envelope extraction, cell write
    pareto.py                       # Task 10: frontier + coarse tiers + per-class qualify
    report.py                       # Task 11: leaderboard.md + tombstone
    registry.py                     # Task 12: load/filter/drop/prune
    cli.py                          # Task 13: run/report/drop/prune/list
  tasks/<task-id>/ ...              # Task 13: seed pack
  results/<model>/<task>/<hash>.json
  reports/leaderboard.md
```

---

### Task 1: Scaffold the package and dependencies

**Files:**

- Create: `projects/model-bench/bench/__init__.py` (empty)
- Create: `projects/model-bench/bench/verifiers/__init__.py` (empty for now)
- Create: `projects/model-bench/README.md` (one paragraph: what this is, how to run `python -m bench.cli run`, link to the design doc)
- Modify (only if needed): `bazel/requirements/all.in`

**Step 1: Confirm deps.** Run `grep -iE '^(pyyaml|httpx|pydantic)\b' bazel/requirements/all.txt`. `httpx` and `pydantic` must be present. If `pyyaml` (or `PyYAML`) is absent, add `pyyaml` to `bazel/requirements/all.in` and regenerate the lock per `bazel/requirements/README.md` (the format hook "Update Python requirements" handles it on commit). If present, no requirements change.

**Step 2: Create the package files** (empty `__init__.py`s + README).

**Step 3: Regenerate BUILD files.** Run `format`. Expect new `projects/model-bench/bench/BUILD` etc. to appear.

**Step 4: Self-review** the generated BUILD for a `py_library` named `bench` (or per-file libs). No test to write yet.

**Step 5: Commit**

```bash
git add projects/model-bench bazel/requirements
git commit -m "feat(model-bench): scaffold package and confirm deps"
```

---

### Task 2: Core schemas (`schema.py`)

**Files:**

- Create: `projects/model-bench/bench/schema.py`
- Create: `projects/model-bench/bench/schema_test.py`

**Step 1: Write the failing test** (`schema_test.py`)

```python
from bench.schema import TaskSpec, VerifierSpec, ModelSpec, ResultCell, TaskClass, Attempt


def test_taskspec_parses_minimal_yaml_shape():
    t = TaskSpec(
        id="helm-values-plumbing-01",
        version="v1",
        task_class=TaskClass.CONFIG_PLUMBING,
        prompt="Wire the image tag into values.yaml.",
        target_files=["values.yaml"],
        verifier=VerifierSpec(kind="helm-template", args={"release": "x", "assert_jsonpath": "$.spec"}),
    )
    assert t.task_class == "config-plumbing"
    assert t.verifier.kind == "helm-template"


def test_modelspec_defaults_active_and_temp_zero():
    m = ModelSpec(id="anthropic/claude-sonnet-4.6")
    assert m.status == "active"
    assert m.params.temperature == 0.0


def test_resultcell_records_both_attempts_and_provenance():
    cell = ResultCell(
        task_id="t", task_version="v1", model_id="m", content_hash="abc123",
        outcome="pass@2",
        attempts=[Attempt(passed=False, feedback="err", latency_ms=10, prompt_tokens=5, completion_tokens=7),
                  Attempt(passed=True, feedback="", latency_ms=20, prompt_tokens=9, completion_tokens=3)],
        cost_usd=0.0004, harness_version="0.1.0", prompt_template_hash="deadbeef",
    )
    assert cell.total_latency_ms == 30
    assert cell.total_tokens == 24
    assert cell.first_attempt_passed is False
```

**Step 2: Implement `schema.py`.** Provide:

- `class TaskClass(str, Enum)`: `MECHANICAL="mechanical"`, `CONFIG_PLUMBING="config-plumbing"`, `CODE_FIX="code-fix"`, `FREE_TEXT="free-text"`.
- `class VerifierSpec(BaseModel)`: `kind: str`, `args: dict[str, Any] = {}`. `kind` is the dispatch key.
- `class ModelParams(BaseModel)`: `temperature: float = 0.0`, `max_tokens: int = 8192`.
- `class ModelSpec(BaseModel)`: `id: str`, `status: Literal["active","experimental","retired"] = "active"`, `params: ModelParams = ModelParams()`, `role: Literal["candidate","anchor"] = "candidate"`, `retired_reason: str | None = None`, `retired_date: str | None = None`.
- `class TaskSpec(BaseModel)`: `id`, `version: str`, `task_class: TaskClass` (alias `class` via `Field(alias="class")`, `populate_by_name=True`), `prompt: str`, `target_files: list[str]`, `verifier: VerifierSpec`, `source_commit: str | None = None`.
- `class Attempt(BaseModel)`: `passed: bool`, `feedback: str`, `latency_ms: int`, `prompt_tokens: int`, `completion_tokens: int`.
- `class ResultCell(BaseModel)`: fields as in the test; `outcome: Literal["pass@1","pass@2","fail"]`; computed properties `total_latency_ms`, `total_tokens`, `first_attempt_passed` (via `@property` or `@computed_field`).

**Step 3: Self-review** field names against `schema_test.py`.

**Step 4: `format`** then **Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): task, model, and result schemas"
```

---

### Task 3: Content-hash cache (`cache.py`)

**Files:**

- Create: `projects/model-bench/bench/cache.py`
- Create: `projects/model-bench/bench/cache_test.py`

**Step 1: Write the failing test**

```python
from bench.cache import cell_key, cell_path, is_cached, HARNESS_VERSION


def test_cell_key_is_stable_and_order_independent(tmp_path):
    inputs = dict(prompt="p", fixture_hash="fh", verifier_repr="vr",
                  model_id="m", params_repr="pr")
    k1 = cell_key(**inputs)
    k2 = cell_key(**inputs)
    assert k1 == k2 and len(k1) == 12


def test_cell_key_changes_when_verifier_changes():
    base = dict(prompt="p", fixture_hash="fh", verifier_repr="v1",
                model_id="m", params_repr="pr")
    assert cell_key(**base) != cell_key(**{**base, "verifier_repr": "v2"})


def test_is_cached_true_only_when_file_exists(tmp_path):
    p = cell_path(tmp_path, "openai/gpt-x", "task-1", "abc123def456")
    assert not is_cached(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert is_cached(p)
```

**Step 2: Implement `cache.py`.**

```python
import hashlib
from pathlib import Path

HARNESS_VERSION = "0.1.0"


def cell_key(*, prompt: str, fixture_hash: str, verifier_repr: str,
             model_id: str, params_repr: str) -> str:
    h = hashlib.sha256()
    for part in (prompt, fixture_hash, verifier_repr, HARNESS_VERSION, model_id, params_repr):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")  # domain separator so concatenation is unambiguous
    return h.hexdigest()[:12]


def fixture_hash(fixture_dir: Path) -> str:
    """Hash of the frozen fixture tree: sorted (relpath, bytes) so it is deterministic."""
    h = hashlib.sha256()
    for f in sorted(p for p in fixture_dir.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(fixture_dir)).encode())
        h.update(b"\x00")
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:12]


def _slug(model_id: str) -> str:
    return model_id.replace("/", "__")


def cell_path(results_root: Path, model_id: str, task_id: str, key: str) -> Path:
    return results_root / _slug(model_id) / task_id / f"{key}.json"


def is_cached(path: Path) -> bool:
    return path.exists()
```

Note the `\x00` domain separators (prevents `"a"+"b"` colliding with `"ab"+""`), and that `HARNESS_VERSION` and `verifier_repr` are both in the key so bumping either invalidates cells (design doc requirement).

**Step 3: Self-review** against tests. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): content-hash cell cache with verifier-versioned keys"
```

---

### Task 4: Credential-scrubbed sandbox (`sandbox.py`)

**Files:**

- Create: `projects/model-bench/bench/sandbox.py`
- Create: `projects/model-bench/bench/sandbox_test.py`

**Step 1: Write the failing test**

```python
import os
from bench.sandbox import run_sandboxed, SandboxResult


def test_scrubs_cluster_and_token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/secret/kubeconfig")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", os.environ["PATH"])  # keep PATH so tools resolve
    res = run_sandboxed(["/usr/bin/env"], cwd=tmp_path, timeout_s=10)
    assert "KUBECONFIG" not in res.stdout
    assert "sk-secret" not in res.stdout


def test_returns_rc_and_streams(tmp_path):
    res = run_sandboxed(["sh", "-c", "echo out; echo err 1>&2; exit 3"], cwd=tmp_path, timeout_s=10)
    assert res.rc == 3 and "out" in res.stdout and "err" in res.stderr


def test_timeout_is_nonzero_rc(tmp_path):
    res = run_sandboxed(["sh", "-c", "sleep 5"], cwd=tmp_path, timeout_s=1)
    assert res.rc != 0 and res.timed_out is True
```

**Step 2: Implement `sandbox.py`.**

```python
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Env keys that must never reach a verifier subprocess (untrusted model output runs here).
_DENY_PREFIXES = ("KUBE", "OPENROUTER", "OP_", "ONEPASSWORD", "AWS_", "GITHUB_TOKEN",
                  "BUILDBUDDY", "ANTHROPIC", "OPENAI")
# Minimal env the tools actually need.
_ALLOW_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")


@dataclass
class SandboxResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool


def _scrubbed_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ALLOW_KEYS if k in os.environ}
    # Belt and braces: drop anything sensitive even if it sneaks into the allow set.
    return {k: v for k, v in env.items() if not k.startswith(_DENY_PREFIXES)}


def run_sandboxed(cmd: list[str], *, cwd: Path, timeout_s: int) -> SandboxResult:
    """Run an untrusted command with a scrubbed env in cwd. No cluster creds, no tokens.

    macOS caveat: this does not network-isolate (that needs a sandbox profile / container);
    env-scrubbing + temp cwd + no-creds is the portable floor. Tighten in CI if needed.
    """
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=_scrubbed_env(),
            capture_output=True, text=True, timeout=timeout_s,
            start_new_session=True,  # own process group so we can reap children on timeout
        )
        return SandboxResult(proc.returncode, proc.stdout, proc.stderr, False)
    except subprocess.TimeoutExpired as e:
        return SandboxResult(124, e.stdout or "", (e.stderr or "") + "\n[sandbox] timed out", True)
```

**Step 3: Self-review** the deny/allow logic. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): credential-scrubbed verifier sandbox"
```

---

### Task 5: Verifier dispatch + command/helm/compile verifiers

**Files:**

- Create: `projects/model-bench/bench/verifiers/__init__.py` (dispatch)
- Create: `projects/model-bench/bench/verifiers/command.py`
- Create: `projects/model-bench/bench/verifiers/helm.py`
- Create: `projects/model-bench/bench/verifiers/compile.py`
- Create: `projects/model-bench/bench/verifiers/verifiers_test.py`

**Contract:** every verifier is `def verify(workdir: Path, args: dict) -> VerifyResult` where
`VerifyResult = namedtuple/dataclass(passed: bool, feedback: str)` and `feedback` is the
**real tool stderr/stdout** on failure (fed to the retry), never the golden answer.

**Step 1: Write the failing test** (covers dispatch + each verifier; use `tmp_path` as workdir)

```python
from pathlib import Path
from bench.verifiers import get_verifier, VerifyResult


def test_dispatch_unknown_kind_raises():
    import pytest
    with pytest.raises(KeyError):
        get_verifier("nope")


def test_command_verifier_passes_on_zero_exit(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "exit 0"]})
    assert r.passed


def test_command_verifier_reports_stderr_on_fail(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "echo boom 1>&2; exit 1"]})
    assert not r.passed and "boom" in r.feedback


def test_compile_python_detects_syntax_error(tmp_path):
    (tmp_path / "m.py").write_text("def f(:\n")
    v = get_verifier("py-compile")
    r = v(tmp_path, {"file": "m.py"})
    assert not r.passed and "SyntaxError" in r.feedback
```

**Step 2: Implement.**

- `verifiers/__init__.py`:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

@dataclass
class VerifyResult:
    passed: bool
    feedback: str

_REGISTRY: dict[str, Callable[[Path, dict], VerifyResult]] = {}

def register(kind: str):
    def deco(fn):
        _REGISTRY[kind] = fn
        return fn
    return deco

def get_verifier(kind: str):
    if kind not in _REGISTRY:
        raise KeyError(f"unknown verifier kind: {kind}")
    return _REGISTRY[kind]

# import submodules so their @register runs
from . import command, helm, compile  # noqa: E402,F401
```

- `command.py`: `@register("command")` runs `run_sandboxed(args["cmd"], cwd=workdir, timeout_s=args.get("timeout_s", 120))`; `passed = rc == 0`; `feedback = stderr or stdout`.
- `compile.py`: `@register("py-compile")` runs `["python", "-c", "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)", args["file"]]` via sandbox; `@register("go-build")` runs `["go", "build", "./..."]` in `workdir`. Feedback = stderr.
- `helm.py`: `@register("helm-template")`. Render:
  `helm template <release> <chart_dir> -f <values>` via sandbox, capturing stdout.
  If rc != 0 -> `(False, stderr)`. Else, if `args.get("assert_contains")` present, check each
  substring is in the rendered stdout; if `args.get("refute_contains")` present, check none are.
  Feedback on assertion failure = a message naming the missing/forbidden string PLUS the rendered
  output slice (so the retry sees what it produced). This is the **effect assertion** the design
  requires: never just "rc==0".

**Step 3: Self-review** that helm asserts on rendered content, not just exit code. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): verifier dispatch with command, helm-template, and compile verifiers"
```

---

### Task 6: Effect-asserting verifiers: lint, RBAC coverage, json-match

**Files:**

- Create: `projects/model-bench/bench/verifiers/lint.py`
- Create: `projects/model-bench/bench/verifiers/rbac.py`
- Create: `projects/model-bench/bench/verifiers/jsonmatch.py`
- Create: `projects/model-bench/bench/verifiers/effect_verifiers_test.py`
- Modify: `projects/model-bench/bench/verifiers/__init__.py` (import the three new modules)

**Step 1: Write the failing test** (RBAC is the flagship effect verifier)

```python
from bench.verifiers import get_verifier

RBAC_YAML = """
rules:
- apiGroups: ["argoproj.io"]
  resources: ["applications"]
  verbs: ["get", "list"]
"""

def test_rbac_cover_passes_when_all_calls_covered(tmp_path):
    (tmp_path / "role.yaml").write_text(RBAC_YAML)
    v = get_verifier("rbac-cover")
    r = v(tmp_path, {"clusterrole": "role.yaml",
                     "required": [{"group": "argoproj.io", "resource": "applications", "verb": "list"}]})
    assert r.passed

def test_rbac_cover_fails_and_names_missing_verb(tmp_path):
    (tmp_path / "role.yaml").write_text(RBAC_YAML)
    v = get_verifier("rbac-cover")
    r = v(tmp_path, {"clusterrole": "role.yaml",
                     "required": [{"group": "argoproj.io", "resource": "applications", "verb": "watch"}]})
    assert not r.passed and "watch" in r.feedback

def test_jsonmatch_compares_structured_answer(tmp_path):
    (tmp_path / "answer.json").write_text('{"verbs": ["get","list"]}')
    v = get_verifier("json-match")
    r = v(tmp_path, {"file": "answer.json", "expect": {"verbs": ["get", "list"]}})
    assert r.passed
```

**Step 2: Implement.**

- `lint.py`: `@register("ruff")` runs `["ruff", "check", args.get("path", ".")]`; `@register("buildifier")` runs `["buildifier", "--mode=check", args["file"]]`. Feedback = stderr/stdout.
- `rbac.py`: `@register("rbac-cover")`. Parse the ClusterRole YAML (`yaml.safe_load`), build the covered set of `(group, resource, verb)` (expand `rules[].apiGroups x resources x verbs`, treat `"*"` as wildcard match). For each `required` triple, check coverage. `passed` iff all covered; feedback lists the uncovered triples by name. This is the effect assertion that mirrors the real CLAUDE.md RBAC gotcha.
- `jsonmatch.py`: `@register("json-match")`. Load `args["file"]` JSON, deep-compare to `args["expect"]` (order-insensitive for lists if `args.get("unordered")`). Feedback = a diff string on mismatch.
- Update `__init__.py` imports to include `lint, rbac, jsonmatch`.

**Step 3: Self-review** RBAC wildcard handling and that feedback names the missing triple. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): effect-asserting lint, RBAC-coverage, and json-match verifiers"
```

---

### Task 7: Hardened free-text judge (`judge.py`)

**Files:**

- Create: `projects/model-bench/bench/judge.py`
- Create: `projects/model-bench/bench/judge_test.py`

**Step 1: Write the failing test** (judge calls are injected, so tests are deterministic)

```python
from bench.judge import judge_free_text, JudgeConfig

def fake_caller(prompt: str) -> str:
    # deterministic stub: returns PASS iff the candidate contains "feat("
    return "PASS" if "feat(" in prompt else "FAIL"

def test_judge_passes_conventional_commit():
    cfg = JudgeConfig(judge_model="anthropic/claude-sonnet-4.6", criteria=["conventional", "no-em-dash"])
    r = judge_free_text(candidate="feat(x): do a thing", task_prompt="write a commit",
                        cfg=cfg, caller=fake_caller)
    assert r.passed

def test_judge_refuses_to_grade_own_output():
    import pytest
    cfg = JudgeConfig(judge_model="anthropic/claude-sonnet-4.6", criteria=["x"])
    with pytest.raises(ValueError):
        judge_free_text(candidate="c", task_prompt="p", cfg=cfg, caller=fake_caller,
                        candidate_model="anthropic/claude-sonnet-4.6")  # same as judge -> self-preference
```

**Step 2: Implement `judge.py`.**

- `class JudgeConfig(BaseModel)`: `judge_model: str`, `criteria: list[str]`, `permutations: int = 2`.
- `def judge_free_text(*, candidate, task_prompt, cfg, caller, candidate_model=None) -> JudgeResult`:
  - **Self-preference guard:** if `candidate_model == cfg.judge_model`, raise `ValueError` (design: never self-judge).
  - **Cue-stripping:** strip any model-identifying metadata from `candidate` before templating (here, nothing to strip beyond trimming; document the hook).
  - **Order permutation:** build `cfg.permutations` prompts with the `criteria` list shuffled deterministically (rotate the list, not RNG, so it is reproducible), call `caller` for each, majority-vote PASS/FAIL. This debiases criterion-order bias.
  - Return `JudgeResult(passed: bool, votes: list[str])`.
- `caller` is `Callable[[str], str]`; production wiring passes a closure over `openrouter.complete` (Task 8). Keeping it injected makes the judge unit-testable without network.

**Step 3: Self-review** the self-preference guard and deterministic permutation. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): hardened free-text judge with self-preference guard and order permutation"
```

---

### Task 8: OpenRouter client (`openrouter.py`)

**Files:**

- Create: `projects/model-bench/bench/openrouter.py`
- Create: `projects/model-bench/bench/openrouter_test.py`

**Step 1: Write the failing test** (inject a fake transport; no real network)

```python
import httpx, asyncio
from bench.openrouter import OpenRouterClient, Completion

def test_complete_parses_usage_and_measures_latency():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        })
    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(api_key="test", transport=transport)
    c = asyncio.run(client.complete(model="x/y", messages=[{"role": "user", "content": "hi"}], temperature=0.0))
    assert c.text == "hello" and c.prompt_tokens == 11 and c.completion_tokens == 3 and c.latency_ms >= 0

def test_price_lookup_computes_usd():
    client = OpenRouterClient(api_key="test")
    client._prices = {"x/y": (1.0, 2.0)}  # $/1M prompt, $/1M completion
    assert abs(client.cost_usd("x/y", 1_000_000, 500_000) - (1.0 + 1.0)) < 1e-9
```

**Step 2: Implement `openrouter.py`.**

- `@dataclass Completion`: `text, prompt_tokens, completion_tokens, latency_ms`.
- `class OpenRouterClient`:
  - `__init__(self, *, api_key, transport=None, base_url="https://openrouter.ai/api/v1")` builds an `httpx.AsyncClient(transport=transport, ...)` with `Authorization: Bearer {api_key}`.
  - `async def complete(self, *, model, messages, temperature, max_tokens=8192) -> Completion`: POST `/chat/completions`, time it with a monotonic clock, parse `choices[0].message.content` and `usage`. Retry (tenacity-style manual loop) on 429/5xx with capped backoff; raise on 4xx-other.
  - `async def load_prices(self)`: GET `/models`, populate `self._prices[model_id] = (prompt_usd_per_mtok, completion_usd_per_mtok)` from the pricing fields (OpenRouter returns per-token prices; multiply to per-Mtok).
  - `def cost_usd(self, model, prompt_tokens, completion_tokens) -> float`: uses `self._prices`; returns `0.0` with a logged warning if the model is missing.
- Read `OPENROUTER_API_KEY` from env at the CLI layer, not here (keeps the client testable).

**Step 3: Self-review** the retry/latency logic. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): async OpenRouter client with usage, pricing, and latency"
```

---

### Task 9: 2-shot runner (`runner.py`)

**Files:**

- Create: `projects/model-bench/bench/runner.py`
- Create: `projects/model-bench/bench/runner_test.py`

**Step 1: Write the failing test** (inject a scripted model + a stub verifier)

```python
import asyncio
from pathlib import Path
from bench.runner import run_cell
from bench.verifiers import VerifyResult

def make_model(script):  # script: list of outputs per attempt
    calls = {"i": 0}
    async def complete(**kwargs):
        from bench.openrouter import Completion
        out = script[calls["i"]]; calls["i"] += 1
        return Completion(text=out, prompt_tokens=10, completion_tokens=5, latency_ms=7)
    return complete

def test_pass_at_1_when_first_attempt_verifies(tmp_path):
    verifier = lambda workdir, args: VerifyResult(True, "")
    cell = asyncio.run(run_cell(
        task_id="t", task_version="v1", model_id="m", content_hash="h",
        fixture_dir=tmp_path, target_files=["out.txt"],
        prompt="p", complete=make_model(["FILE out.txt\nok"]),
        verify=verifier, cost_fn=lambda p, c: 0.001))
    assert cell.outcome == "pass@1" and len(cell.attempts) == 1

def test_pass_at_2_feeds_stderr_not_golden(tmp_path):
    seen = {}
    def verifier(workdir, args):
        content = (Path(workdir) / "out.txt").read_text()
        return VerifyResult(content == "good", "" if content == "good" else "boom-stderr")
    async def complete(**kwargs):
        from bench.openrouter import Completion
        # second call must contain the verifier feedback, never a golden answer
        seen["msgs"] = kwargs["messages"]
        text = "FILE out.txt\ngood" if any("boom-stderr" in m["content"] for m in kwargs["messages"]) else "FILE out.txt\nbad"
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, latency_ms=1)
    cell = asyncio.run(run_cell(
        task_id="t", task_version="v1", model_id="m", content_hash="h",
        fixture_dir=tmp_path, target_files=["out.txt"], prompt="p",
        complete=complete, verify=verifier, cost_fn=lambda p, c: 0.0))
    assert cell.outcome == "pass@2" and len(cell.attempts) == 2
```

**Step 2: Implement `runner.py`.**

- `def extract_files(text, target_files) -> dict[str,str]`: lenient parser. Recognize blocks of the form `FILE <path>\n<content>` and fenced code blocks labeled with a path; fall back to "if a single target file, treat whole response (minus surrounding prose/backticks) as its content". Goal: measure capability, not envelope compliance (design requirement). Unit-test this separately if time permits.
- `async def run_cell(...) -> ResultCell`:
  1. Copy `fixture_dir` into a temp workdir (so the frozen fixture is never mutated).
  2. Shot 1: `c1 = await complete(model=model_id, messages=[{"role":"user","content": prompt}], temperature=0.0)`. Write extracted files into workdir. `r1 = verify(workdir, verifier_args)`.
  3. If `r1.passed`: outcome `pass@1`, attempts `[Attempt(...c1..., passed=True)]`.
  4. Else shot 2: messages append the assistant turn `c1.text` and a user turn containing **`r1.feedback` verbatim** with an instruction to fix (NEVER the golden file). `c2 = await complete(...)`. Re-copy the clean fixture, write shot-2 files, `r2 = verify(...)`. Outcome `pass@2` if `r2.passed` else `fail`.
  5. Build `ResultCell` with both `Attempt`s, `cost_usd = cost_fn(total_prompt, total_completion)`, `harness_version`, `prompt_template_hash`.
- Keep `complete`, `verify`, `cost_fn` injected so the loop is unit-testable offline.

**Step 3: Self-review** that shot 2 receives only `r1.feedback`, and that the fixture tree is re-copied clean between attempts. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): 2-shot cell runner with stderr-only feedback and lenient file extraction"
```

---

### Task 10: Pareto + per-class qualification (`pareto.py`)

**Files:**

- Create: `projects/model-bench/bench/pareto.py`
- Create: `projects/model-bench/bench/pareto_test.py`

**Step 1: Write the failing test**

```python
from bench.pareto import pareto_frontier, qualifies, ClassScore

def test_frontier_flags_dominated():
    # (model, pass1, cost) - B dominated by A (worse quality, higher cost)
    pts = {"A": (0.9, 1.0), "B": (0.8, 2.0), "C": (0.95, 5.0)}
    front = pareto_frontier(pts)  # higher pass1 better, lower cost better
    assert "A" in front and "C" in front and "B" not in front

def test_qualifies_relative_to_anchor():
    anchor = ClassScore(pass1=0.8, cost=10.0)
    cand = ClassScore(pass1=0.85, cost=2.0)
    assert qualifies(cand, anchor)  # >= anchor pass1 and cheaper
    assert not qualifies(ClassScore(pass1=0.7, cost=1.0), anchor)  # below bar
```

**Step 2: Implement `pareto.py`.**

- `@dataclass ClassScore`: `pass1: float`, `cost: float` (and optionally `pass2`, `latency_ms`).
- `def pareto_frontier(points: dict[str, tuple[float, float]]) -> set[str]`: model is on the frontier if no other model has `pass1 >= p1 and cost <= c` with at least one strict. Return the non-dominated set.
- `def qualifies(cand: ClassScore, anchor: ClassScore) -> bool`: `cand.pass1 >= anchor.pass1 and cand.cost < anchor.cost`.
- `def coarse_tier(pass1_first, pass_any) -> str`: `"one-shots"` if `pass1_first >= 0.8`, `"needs-repair"` if `pass_any >= 0.8`, else `"can't"` (thresholds are constants, documented as coarse and tunable).
- `def aggregate_by_class(cells, tasks, models) -> dict[model][class] -> ClassScore`: group cells by `(model, task_class)`, compute pass@1 rate, pass-any rate, mean cost, median latency.

**Step 3: Self-review** the dominance predicate for the strict-inequality edge case. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): Pareto frontier, coarse tiers, and per-class qualification"
```

---

### Task 11: Report renderer (`report.py`)

**Files:**

- Create: `projects/model-bench/bench/report.py`
- Create: `projects/model-bench/bench/report_test.py`

**Step 1: Write the failing test** (assert on rendered markdown structure, an effect assertion)

```python
from bench.report import render_leaderboard

def test_report_has_per_class_qualification_and_tombstone():
    md = render_leaderboard(
        per_class={"cheap/x": {"config-plumbing": {"pass1": 0.9, "cost": 1.0, "tier": "one-shots", "qualifies": True}}},
        anchors={"anthropic/claude-sonnet-4.6": {"config-plumbing": {"pass1": 0.9, "cost": 12.0}}},
        frontier={"config-plumbing": ["cheap/x"]},
        retired=[{"id": "old/y", "reason": "flunked config", "date": "2026-06-01", "pass1": 0.3, "cost": 0.5}],
    )
    assert "## Budget tier" in md
    assert "cheap/x" in md and "config-plumbing" in md
    assert "## Retired" in md and "old/y" in md and "flunked config" in md
```

**Step 2: Implement `report.py`.** `render_leaderboard(...) -> str` produces markdown with:

- `## Budget tier` - per offloadable class, a table of qualifying candidates sorted by cost ascending, with `pass@1`, `pass@2`, `$`, latency, tier.
- The anchors shown as the ceiling/baseline row per class.
- A `## Pareto frontier` section listing non-dominated models per class (dominated flagged).
- `## Retired` tombstone table (id, final pass@1, cost, reason, date).
- No em-dashes in the template (repo style rule).

**Step 3: Self-review** headings match the test. **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench/bench && git commit -m "feat(model-bench): leaderboard renderer with budget tier and retired tombstone"
```

---

### Task 12: Registry load/filter/drop/prune (`registry.py`) + `models.yaml`

**Files:**

- Create: `projects/model-bench/bench/registry.py`
- Create: `projects/model-bench/bench/registry_test.py`
- Create: `projects/model-bench/models.yaml`

**Step 1: Write the failing test**

```python
from bench.registry import load_registry, active_models, drop_model

YAML = """
models:
  - id: anthropic/claude-sonnet-4.6
    role: anchor
  - id: cheap/fast
    status: active
  - id: old/dead
    status: retired
    retired_reason: flunked
"""

def test_active_excludes_retired(tmp_path):
    p = tmp_path / "models.yaml"; p.write_text(YAML)
    reg = load_registry(p)
    ids = [m.id for m in active_models(reg)]
    assert "cheap/fast" in ids and "old/dead" not in ids

def test_drop_sets_retired_with_reason(tmp_path):
    p = tmp_path / "models.yaml"; p.write_text(YAML)
    drop_model(p, "cheap/fast", reason="too weak", date="2026-06-30")
    reg = load_registry(p)
    m = next(m for m in reg if m.id == "cheap/fast")
    assert m.status == "retired" and m.retired_reason == "too weak"
```

**Step 2: Implement `registry.py`.**

- `load_registry(path) -> list[ModelSpec]` (parse yaml, validate each via `ModelSpec`).
- `active_models(reg, include_experimental=False)` filters by status.
- `anchors(reg)` returns `role == "anchor"`.
- `drop_model(path, model_id, *, reason, date)`: load raw yaml, set the entry's `status/retired_reason/retired_date`, write back preserving key order/comments as best as possible (use `yaml.safe_dump`; comment preservation is out of scope).
- `prune_retired(results_root, reg)`: delete result cells for retired models, leaving the tombstone in `models.yaml`.
- Author `models.yaml` seed: anchors `anthropic/claude-opus-4.8`, `anthropic/claude-sonnet-4.6` (role: anchor), plus a handful of budget candidates (e.g. a cheap open model id, a cheap closed model id) as `active`. Use real OpenRouter model ids; leave a comment that ids must match OpenRouter's `/models`.

**Step 3: Self-review.** **Step 4: `format`.** **Step 5: Commit**

```bash
git add projects/model-bench && git commit -m "feat(model-bench): model registry with retire and prune"
```

---

### Task 13: CLI wiring + seed task pack

**Files:**

- Create: `projects/model-bench/bench/cli.py`
- Create: `projects/model-bench/bench/cli_test.py`
- Create: `projects/model-bench/tasks/helm-values-plumbing-01/{task.yaml,fixture/...,expected/...}`
- Create: `projects/model-bench/tasks/rbac-endpoint-verbs-01/{task.yaml,fixture/...}`
- Create: `projects/model-bench/tasks/commit-message-01/{task.yaml,expected/...}`

**Step 1: Write the failing test** (CLI arg parsing + task loading, no network)

```python
from bench.cli import build_parser, load_tasks

def test_parser_has_subcommands():
    p = build_parser()
    for sub in ("run", "report", "drop", "prune", "list"):
        assert sub in p._subparsers._group_actions[0].choices  # argparse introspection

def test_load_tasks_reads_pack(tmp_path):
    d = tmp_path / "tasks" / "t1"; d.mkdir(parents=True)
    (d / "task.yaml").write_text(
        'id: t1\nversion: v1\nclass: config-plumbing\nprompt: p\n'
        'target_files: [values.yaml]\nverifier: {kind: command, args: {cmd: ["true"]}}\n')
    tasks = load_tasks(tmp_path / "tasks")
    assert tasks[0].id == "t1" and tasks[0].task_class == "config-plumbing"
```

**Step 2: Implement `cli.py`.**

- `build_parser()` (argparse) with subcommands:
  - `run [--tasks DIR] [--models models.yaml] [--results DIR] [--force] [--include-experimental]`: loads tasks + active models, builds the client (`OPENROUTER_API_KEY` from env), `await client.load_prices()`, then for each `(task, model)` computes the cell key, skips if cached (unless `--force`), else `run_cell(...)` and writes the JSON cell. Uses `asyncio.gather` with a semaphore to bound concurrency. Free-text tasks route to `judge.py` instead of a deterministic verifier.
  - `report [--results DIR] [--out reports/leaderboard.md]`: aggregate cells, compute qualification/frontier, `render_leaderboard`, write the file.
  - `drop MODEL --reason R`: `drop_model(...)` with today's date passed in (CLI reads the date; keeps functions pure).
  - `prune --retired`: `prune_retired(...)`.
  - `list`: print the registry with status.
- `load_tasks(dir) -> list[TaskSpec]`.
- `main()` -> `py_venv_binary` entrypoint; also runnable as `python -m bench.cli`.

**Step 3: Author 3 seed tasks** (proof the harness works end to end; the rest of the pack follows later):

- `helm-values-plumbing-01`: fixture = a minimal chart + values with a missing image tag; prompt asks to wire it; verifier `helm-template` with `assert_contains` on the rendered image ref. Distill from a real chart-values commit; **rewrite the prompt in your own words** (do not paste the commit message) and keep the fixture minimal.
- `rbac-endpoint-verbs-01`: fixture = a Go/Python handler that calls `list` + `get` on `argoproj.io/applications` and a ClusterRole missing `list`; prompt asks to fix the ClusterRole; verifier `rbac-cover` with the required triples.
- `commit-message-01`: prompt = a diff summary; free-text; judge criteria `["conventional-commits", "no-em-dash", "explains-why"]`.

**Step 4: Self-review** each `task.yaml` validates against `TaskSpec` and each verifier asserts on effects. **Step 5: `format`.** **Step 6: Commit**

```bash
git add projects/model-bench && git commit -m "feat(model-bench): CLI and seed task pack (helm plumbing, RBAC coverage, commit message)"
```

---

### Task 14: End-of-plan CI + one real smoke run

**Files:** none (verification task).

**Step 1: Push the branch and open the PR.**

```bash
git push -u origin feat/model-bench
gh pr create --fill --base main
```

**Step 2: Watch CI.** `gh pr checks <number> --watch`. On failure, read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`; quote the assertion before hypothesizing (repo rule); fix; push; repeat. This is where all the `py_test` targets actually run.

**Step 3: One real smoke run (manual, local, billed).** With `OPENROUTER_API_KEY` set, run the harness against the 3 seed tasks and the anchors + one budget candidate:

```bash
cd projects/model-bench && python -m bench.cli run --models models.yaml --tasks tasks --results results
python -m bench.cli report --out reports/leaderboard.md
```

Confirm: cells are written under `results/`, a rerun is a no-op (cache hit), editing a task re-runs only its cells, and `reports/leaderboard.md` shows per-class qualification + a Pareto section. Commit the seed `results/` + `reports/leaderboard.md` as the first cached baseline.

**Step 4: Commit the baseline**

```bash
git add projects/model-bench/results projects/model-bench/reports
git commit -m "chore(model-bench): commit first cached result baseline and leaderboard"
git push
```

**Step 5: Merge** after CI passes: `gh pr merge --rebase` (repo allows rebase only).

---

## Review

Per CLAUDE.md, do **one comprehensive code review** against the full diff at the end (not per task). Implementer subagents self-review before each commit; the end-of-PR Opus review is the boundary. Focus the review on: the sandbox deny-list completeness (no credential path reaches a verifier), the runner never leaking golden answers into shot 2, verifier effect-assertions (not exit-code-only), and cache-key coverage (verifier + harness version both included).
