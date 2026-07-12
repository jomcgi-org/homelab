# Security Findings Remediation Plan — Semgrep + GitHub

**Date:** 2026-07-11
**Status:** Ready for execution
**Scope:** Every outstanding Semgrep and GitHub (Dependabot) security finding for `jomcgi/homelab`, triaged into actionable tasks.

---

## 1. Executive Summary

Two independent finding sources were fully enumerated:

| Source | Total open | Actionable surface | Notes |
| --- | --- | --- | --- |
| **GitHub Dependabot** | 100 alerts (28 high / 52 medium / 20 low) | **18 unique packages** across 3 lock manifests | No criticals. Alerts double-count because each package appears in both `all.in` and `test.in` (some also `tools.in`). |
| **Semgrep AppSec Platform** | 425 findings | **96 on `ref=main`** (6 critical / 33 high / 57 medium) | The other **329 are stale `refs/pull/*/merge` findings** from long-closed PRs and the pre-`projects/` layout that never auto-resolved. |
| **GitHub code scanning** | — | n/a | No SARIF is uploaded (`code-scanning/alerts` → `404 no analysis`); Semgrep results flow to the Semgrep App via webhook, not GitHub code scanning. |
| **GitHub secret scanning** | — | disabled | Out of scope; see Task E4 (optional). |

**The work splits into three parts:**
- **Part A — Dependencies (Dependabot):** almost entirely a single lock refresh; 4 packages need a manual constraint edit first (`starlette` is the only hard one).
- **Part B — Semgrep code findings (96 on main):** ~55 are false-positives/by-design to suppress or accept with justification; ~25 are genuine hardening worth doing; concentrated in a handful of rules.
- **Part C — Semgrep App hygiene:** bulk-resolve the 329 stale findings and fix the root cause (no completed full scan on `main` to auto-resolve fixed findings).

### How this was gathered (reproducible)

```bash
# Dependabot (GitHub side)
gh api -X GET repos/jomcgi/homelab/dependabot/alerts -f state=open -f per_page=100 --jq '...'

# Semgrep App findings live in semgrep.dev, NOT in local Postgres.
# The local semgrep.scan_perf table holds only scan *metadata* (timing/counts).
# Token lives in the monolith pod; the run-python sandbox is zero-egress so cannot reach it.
PY=/projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3.13
kubectl exec -i -n monolith deploy/monolith -c backend -- "$PY" - <<'EOF'
import os, httpx
h={"Authorization":f"Bearer {os.environ['SEMGREP_APP_TOKEN']}"}
slug=httpx.get("https://semgrep.dev/api/v1/deployments",headers=h).json()["deployments"][0]["slug"]
# GET /api/v1/deployments/{slug}/findings?status=open&page_size=300  (paginate)
EOF
```

---

## Part A — Dependency CVEs (GitHub Dependabot)

### A0. Mechanism (read first)

All 100 alerts are `pip` packages resolved by the layered uv/pip-tools pipeline in `bazel/requirements/` (see `bazel/requirements/README.md`). Direct runtime deps live in `pyproject.toml`; test/tool deps in `test.in`/`tools.in`; everything compiles to the committed `runtime.txt` / `all.txt` / `tools.txt` lock files consumed by `pip.parse` in `MODULE.bazel`.

**Fix vector:** `bazel run //bazel/requirements:update` recompiles the locks and pulls patched versions **for any package whose source constraint already permits them** (most are loose or transitive). This is a `run` target that rewrites committed files; it is **not** part of CI and **not** the workstation test loop. It must be run in an environment with `bazel` + `uv`, then the regenerated `*.txt` locks committed and pushed. There is **no code change** for the bulk of these — only the lock refresh.

> **Engineering-philosophy note (simplest approach):** the simplest remediation is one bulk `:update` refresh (Task A1) that sweeps ~14 of 18 packages at once, followed by 4 targeted constraint edits (A2). Do *not* hand-bump 18 packages individually.

### A1. Bulk lock refresh (sweeps most alerts) — **do first**

- [ ] Run `bazel run //bazel/requirements:update`, commit the regenerated `runtime.txt` / `all.txt` / `tools.txt`.
- [ ] Confirm patched versions landed for the loosely-/transitively-constrained packages below.
- [ ] **Do NOT bump `semgrep==1.168.0`** in `tools.in` — it is pinned exactly and version-coupled to the fc-invoke Pro engine (`projects/monolith/semgrep_scan/report.py` imports pysemgrep internals). It has no CVE. Leave it. If the re-lock tries to move it, hold it with `-c`.

Packages that A1 alone should fix (current → target patched):

| Package | Current pin | Target | Sev | Constraint source |
| --- | --- | --- | --- | --- |
| `urllib3` | 2.6.3 | ≥2.7.0 | high | transitive |
| `dulwich` | 1.1.0 | ≥1.2.5 | high | direct (`pyproject`, unbounded) |
| `cryptography` | 46.0.6 | ≥48.0.1 | high | transitive |
| `python-multipart` | 0.0.22 | ≥0.0.31 | high | direct `>=0.0.20` |
| `Mako` | 1.3.10 | ≥1.3.12 | high | transitive |
| `soupsieve` | 2.8.3 | ≥2.8.4 | high | transitive (via bs4) |
| `aiohttp` | 3.13.5 | ≥3.14.1 | medium | direct (unbounded) |
| `authlib` | 1.6.9 | ≥1.6.12 | medium | transitive |
| `zeroconf` | 0.148.0 | ≥0.149.16 | medium | transitive |
| `idna` | 3.11 | ≥3.15 | medium | transitive |
| `pydantic-settings` | 2.13.1 | ≥2.14.2 | medium | direct `~=2.1` (permits it) |
| `pip` | 26.0.1 | ≥26.1.2 | medium | pinned in lock |
| `pytest` | (test) | ≥9.0.3 | medium | `test.in` (unbounded) |
| `Pygments` | 2.19.2 | ≥2.20.0 | low | transitive + tools |

### A2. Packages needing a constraint edit before the re-lock takes

- [ ] **`lxml` → 6.1.0 (high, XXE — CVE-2026-41066).** Currently pinned by `bazel/requirements/overrides.txt` at `lxml==6.0.2` (an override that strips the `[html-clean]` extra to break a Bazel dep cycle). Bump the override to `lxml==6.1.0`, keep the explicit `lxml-html-clean` runtime dep, and re-lock. Verify the cycle workaround still holds after the bump.
- [ ] **`copier` → 9.14.1 (medium, template traversal — CVE-2026-34726/34730).** `tools.in` has `copier>=9.11.2`, resolving below 9.14.1. Raise to `copier>=9.14.1` and re-lock.
- [ ] **`pydantic-ai-slim` → 1.102.0 (medium, SSRF blocklist bypass — CVE-2026-48782).** `pyproject.toml` has `pydantic-ai-slim[openai]>=0.2`; the re-lock would jump 0.x → 1.x (major). Pin an explicit floor `>=1.102.0` and **verify our pydantic-ai usage still compiles** (search for `pydantic_ai` imports; the 1.0 API changed agent/tool construction). Run affected tests in CI.
- [ ] **`starlette` → 1.3.1 (HIGH ×several — SSRF/DoS/host-poisoning). HARDEST TASK.** Current 0.52.1, floor `starlette>=0.47.2`, and it is **capped by `fastapi==0.135.1`** (FastAPI pins a narrow Starlette range). Starlette 1.x is a major release with breaking API changes. This needs: bump FastAPI to a release that supports Starlette 1.x, raise the Starlette floor, then a full CI pass because middleware/`request.form()`/`TestClient` semantics shifted. **Own PR, highest risk — do not bundle with A1.** If FastAPI has no 1.x-Starlette release yet, mark these alerts "acknowledged, blocked on upstream" and revisit.

### A3. Verify + close out

- [ ] Push the branch, watch CI, confirm `bazel test //...` is green (dependency bumps can break at import/runtime).
- [ ] After merge, re-query Dependabot; confirm alerts auto-close. Any residual = still-vulnerable transitive pin needing an explicit floor.

---

## Part B — Semgrep code findings on `main` (96)

Grouped by disposition. Each group is one reviewable unit. **Preferred suppression mechanism:** triage-to-ignore in the Semgrep App with a written reason (keeps code clean), or an inline `# nosemgrep: <rule-id> — <reason>` where a durable in-repo record is better. Note: `main_semgrep_test` ignores `# nosemgrep`, so for rules enforced by that test, prefer App triage or a path exclusion.

### B1. Accept / suppress — false positives & by-design (no vuln) — ~55 findings

- [ ] **`mcp-auth-passthrough-taint` ×5 (critical)** — `semgrep_scan/client.py:62`, `sandbox/client.py:48`, `semgrep_scan/full_scan.py:67/88/122`. These forward the pod's **own ServiceAccount token** (`auth_headers()`) or our GitHub PAT (`_github_headers()`) to **fixed internal/known hosts** (`FC_INVOKE_URL`, `api.github.com`), and the receiver gates on TokenReview. Not an attacker-controlled passthrough. **Triage: false-positive / by-design.**
- [ ] **Rule self-test fixtures ×4 (medium)** — `bazel/semgrep/rules/python/no-eval-exec.py` (`eval-detected`, `exec-detected`) and `bazel/semgrep/rules/python/duckdb-bound-param-in-array-distance.py` (`sqlalchemy-execute-raw-query`, `formatted-sql-query`, `avoid-sqlalchemy-text`). These files **are the intentional match targets for our own rules.** Add `bazel/semgrep/rules/**` to `.semgrepignore` (or the App path-ignore) so the scanner stops flagging its own fixtures.
- [ ] **`python37-compatibility-importlib2` ×1 (high, FALSE)** — `campsites/client.py:96`. We run Python 3.13; the py3.7 back-compat rule is irrelevant. Triage away (and consider disabling the `python37` ruleset repo-wide).
- [ ] **`non-literal-import` ×6 (medium)** — `app/architecture_test.py`, `app/bdd_completeness_test.py`, `agent/mcp_description_compliance_test.py`, `app/jobs_main.py:183`. Dynamic import by design (architecture/BDD introspection + job registry). Accept.
- [ ] **`len-all-count` ×13 (medium, perf lint)** — 12 are in `*_test.py`; only `grimoire/explore.py:275` is non-test. Fix that one (`len(x.all())` → `.count()`); path-ignore `*_test.py` for this perf rule for the rest.
- [ ] **Firecracker control-plane exec/syscall (by design)** — `dangerous-exec-command` ×5, `dangerous-syscall-exec` ×1, `bad-tmp-file-creation` ×3, `no-direct-write-to-responsewriter(-taint)` on `projects/firecracker/**` and `services/agent-orchestrator/**`. These drivers exec/syscall inside the microVM as their entire purpose; inputs are trusted control-plane values. Accept with a one-line justification per finding.
- [ ] **`dangerous-subprocess-use-tainted-env-args` ×1 (high)** — `model-bench/bench/claude_code.py:80`. Runs the `claude` CLI with config-derived args, not attacker input. Accept.
- [ ] **Low-value by-design** — `import-text-template` (sextant codegen generates Go, not HTML), `math-random-used` (grimoire dice roll, non-security), `ocaml crlf-support` ×3 (vendored `semgrep_src` overlay + hello example). Accept / path-ignore vendored OCaml.

### B2. Genuine hardening — worth fixing — ~25 findings

- [ ] **`tainted-fastapi-http-request-httpx` ×16 (high) — SSRF taint. Biggest review.** Files: `chat_public/{inference,turnstile}.py`, `grimoire_chat/{inference,turnstile}.py`, `chat/whatsapp_calendar.py`, `home/observability/clickhouse.py`. Audit each call: the dominant pattern is a **fixed host** (Cloudflare Turnstile siteverify, the vLLM inference URL from env, the internal ClickHouse endpoint) → those are false-positives to suppress. **Confirm no user-controlled host/path reaches an httpx request**; fix (allow-list host) any that do. This is the one cluster where a real SSRF could hide.
- [ ] **`generic-sql-fastapi` ×1 (critical) + `avoid-sqlalchemy-text` ×4 (high)** — `home/observability/clickhouse.py:45` posts a raw SQL string to ClickHouse; `chat/store.py:456/478/490/498` use SQLAlchemy `text()`. Audit callers for user-tainted SQL. `chat/store.py` is user-facing → highest priority to confirm parameterization. `clickhouse.py` queries are dashboard/observability — likely constant SQL; confirm no user input concatenated, then suppress or parameterize.
- [ ] **CI supply-chain hardening in `.github/workflows/update-semgrep-pro.yaml`** — `gha-curl-pipe-shell` ×1 (line 69: replace `curl | sh` with pinned download + checksum verify) and `github-actions-mutable-action-tag` ×3 (lines 28/31/34: pin `uses:` to full commit SHA). Real, low-effort, do it.
- [ ] **JS/npm supply-chain config** — `.npmrc:1` (`npm-missing-minimum-release-age`) and `pnpm-workspace.yaml:2` (`pnpm-missing-minimum-release-age`, `pnpm-trust-policy`, `pnpm-block-exotic-sub-dependencies`) ×4. Add a package cooldown (`minimumReleaseAge`) and trust policy. Cheap dependency-confusion hardening.
- [ ] **`tainted-path-traversal-pillow-fastapi` ×1 (high)** — `trips/ingest.py:42`. Image path reaches a Pillow open from a request. Sanitize/normalize the path and confine to the expected directory.
- [ ] **`avoid-sqlalchemy-text` / `formatted-sql-query` in `hikes/tools/generate_seed.py:67`** — offline seed generator (lower risk) but still parameterize or `# nosemgrep` with reason.
- [ ] **`detect-non-literal-regexp` ×1 (medium)** — `frontend/.../mention-highlight.js:52` builds a regex from a username → possible ReDoS. Escape the interpolated value.
- [ ] **`invalid-usage-of-modified-variable` ×1 (medium)** — `firecracker/semgrep/guest-init/internal/handler/handler.go:40`. Quick check: harmless under Go ≥1.22 loopvar semantics, but verify it is not a captured-loop-variable bug.

### B3. Optional / low-value (hobby & in-cluster)

- [ ] `cookie-missing-httponly`/`cookie-missing-secure` ×6 — `advent_of_code/pkg/aoc/client.go`. Hobby AoC client; trivial to add flags if desired, else accept.
- [ ] `use-tls` ×1 — signoz dashboard-sidecar in-cluster HTTP. Accept (mesh-encrypted) or note.
- [ ] `writable-filesystem-container` ×1 — `platform/longhorn/configure-node-4-disks.yaml:20`. Disk-config job likely needs a writable FS; confirm and accept.
- [ ] `ifs-tampering` ×1 — `bazel/tools/hooks/check-allowed-tools-prompt-sync.sh:61`. Review the `IFS=` usage; low risk in a local hook.

---

## Part C — Semgrep App hygiene

- [ ] **C1. Bulk-resolve the 329 stale `refs/pull/*/merge` findings.** They belong to long-closed PRs and the pre-`projects/` layout (paths like `charts/claude/src/...`, `sextant/pkg/...`, `overlays/...`). Bulk-triage them to "fixed"/"ignored" in the App so the dashboard reflects reality.
- [ ] **C2. Fix the root cause — no completed full scan on `main` to auto-resolve fixed findings.** Per the semgrep-perf work, a full scan on `main` was not yet running; without it the App cannot move fixed findings to resolved, which is why merged-PR findings pile up. Ensure the scheduled whole-repo full scan on `main` runs (see `POST /internal/semgrep/full-scan`, `semgrep_scan/full_scan.py`) and the baseline seeds so findings auto-resolve on merge going forward.
- [ ] **C3 (optional).** Consider uploading Semgrep SARIF to GitHub code scanning for a unified alert view (currently `code-scanning/alerts` returns "no analysis"). Decide whether the Semgrep App remains the single source of truth.

---

## Part D — Suggested execution order & PR slicing

1. **PR 1 (fast, high value):** Part A1 bulk lock refresh + A2 `lxml`/`copier`/`pydantic-ai-slim` edits. Sweeps ~17 of 18 dependency packages. Watch CI hard (import/runtime breakage).
2. **PR 2 (isolated, risky):** Part A2 `starlette`/`fastapi` major bump. Own PR, own CI cycle.
3. **PR 3 (App only, no code):** Part C1 stale-finding cleanup + C2 full-scan-on-main.
4. **PR 4 (code hardening):** Part B2 real fixes — SSRF audit, SQL audit, GH Actions + npm/pnpm supply-chain, path traversal.
5. **PR 5 (config only):** Part B1 suppressions — `.semgrepignore` for rule fixtures + vendored OCaml + test-path perf-rule ignores, and App triage of the by-design/false-positive set.

Each PR needs its chart bump only if it touches deploying code (Part A/B code changes to the monolith do; A2 dependency bumps that change the monolith image do — use `bazel/tools/git/bump-chart.sh projects/monolith` and the public tier if manifests regenerate). Part C and pure-suppression PRs may not deploy.

---

## Appendix — Full inventory

- **Dependabot:** 18 unique packages (table in A1/A2). Raw list: `gh api repos/jomcgi/homelab/dependabot/alerts -f state=open`.
- **Semgrep main findings:** 96, grouped by rule in Part B. Raw JSON snapshot captured during research (425 total, filter `ref=="main"`).
- **Key files:** findings live in the Semgrep App (`semgrep.dev/api/v1/deployments/jomcgi/findings`); local `semgrep.scan_perf` holds only metadata; harvest logic in `projects/monolith/semgrep_scan/perf_harvest.py`.
