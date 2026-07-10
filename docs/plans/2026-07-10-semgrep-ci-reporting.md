# Route B: Self-Hosted Semgrep CI Reporting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run Semgrep PR scans on our own Firecracker VMs (fast, our rules) while reporting findings to the Semgrep AppSec Platform so the native PR check, triage dashboard, and inline comments work exactly as Managed Scans (SMS) do, then retire SMS.

**Architecture:** The monolith owns the whole Semgrep-App conversation. A GitHub `pull_request` webhook hits the monolith at `private.jomcgi.dev/webhooks/github/semgrep`; the monolith calls the existing fc-invoke `/invoke/semgrep` workload to scan (guest stays air-gapped from the App), then uses pysemgrep's own `ScanHandler` (`start_scan` -> map findings to `RuleMatch` -> `report_findings`/`complete`, or `report_failure` on error) to upload under a per-PR scan keyed to the PR metadata. The App applies policy/triage server-side and posts the native check. The full design (and every prior decision) is in the session scratchpad `route-b-semgrep-ci-design.md`; memory `project_semgrep_ci_app_reporting` and `reference_github_webhook_cloudflare_access` capture the load-bearing facts.

**Tech Stack:** Python 3.13 (FastAPI monolith), pysemgrep INTERNAL APIs (`semgrep.app.scans.ScanHandler`, `semgrep.core_output`, `semgrep.meta`) pinned exactly, Go (fc-invoke semgrep guest), Gateway API HTTPRoute (cloudflare-ingress), 1Password Operator, Helm charts.

**Prereqs (already satisfied):** Semgrep Code deployment exists (semgrep.dev org `jomcgi`, project `3263658`); token `op://k8s-homelab/semgrep-mcp/SEMGREP_API_TOKEN`; Semgrep GitHub App installed (SMS check proves it). SMS stays ON as the safety net until Phase 4.

**Cross-cutting constraints (read before any task):**
- No local test loop. Implement, push the branch, watch CI (`gh pr checks <n> --watch`); read failures via `mcp__buildbuddy__*`. New `*_test.py` needs a hand-added `py_test` (see [[reference_monolith_gazelle_pytest_targets]]); SQLite test fixtures use `create_all`, not migrations.
- pysemgrep APIs are INTERNAL (x_ flags, "internal use"): pin the exact version in the monolith pip deps and gate any bump on re-verifying the import surface. This is the project's real ongoing cost.
- Never write em-dashes anywhere.
- The webhook route reuses the ArgoCD-proven pattern: private.jomcgi.dev + Cloudflare Access IP-Bypass (already configured for `/webhooks/github/semgrep`) + HTTPRoute to `apiPort` + **NO** cf-access SecurityPolicy on the route + app-level HMAC as the real gate.

---

## Phase 1 - Prove the ScanHandler relay (de-risk the internal-API coupling FIRST)

Goal: a finding scanned on our VM appears on a scan in the Semgrep App, uploaded by the monolith via pysemgrep's own client. No webhook, no HTTPRoute yet - proven by an in-pod manual invocation on a throwaway scan. This resolves the single highest-risk unknown before any infrastructure is built.

### Task 1: Expose raw cli_output from the fc-invoke semgrep guest

**Why:** The guest currently flattens `semgrep --json` cli_output to a 6-field `Finding` (`scandriver/driver.go`), discarding `match_based_id` fingerprints, end positions, metadata, and dataflow that `report_findings` needs for faithful App dedup/triage. Add the raw cli_output alongside the existing flattened findings (backward-compatible: existing MCP/demo callers ignore the new field).

**Files:**
- Modify: `projects/firecracker/substrate/vsockproto/proto.go` (add `RawCliOutput json.RawMessage` to `ScanResult`, `omitempty`)
- Modify: `projects/firecracker/semgrep/guest-init/internal/scandriver/driver.go` (keep the raw cli_output line and set `res.RawCliOutput` before returning)
- Modify: `projects/firecracker/semgrep/guest-init/internal/handler/handler.go` if it re-marshals (pass the field through)
- Test: `projects/firecracker/semgrep/guest-init/internal/scandriver/driver_test.go` (assert RawCliOutput preserved)
- Bump: fc-invoke chart (`bazel/tools/git/bump-chart.sh projects/firecracker/substrate`) - the guest rootfs is in fc-invoke's dep closure

**Steps:** write a driver_test asserting the raw bytes survive -> implement pass-through -> `GOOS=linux` build-check the guest packages (the mount/scan code is behind `//go:build linux`) -> bump chart -> push, watch CI, let fc-invoke roll.

**Verify live:** existing MCP `semgrep_scan` still returns `findings`; a direct `curl`-equivalent (in-pod python, fc-invoke has no curl) shows `raw_cli_output` present.

### Task 2: The relay module in projects/monolith/semgrep/

**Files:**
- Create: `projects/monolith/semgrep/report.py` - the relay: `report_pr_scan(pr_meta, raw_cli_output) -> BlockDecision`
- Create: `projects/monolith/semgrep/report_test.py` (+ hand-add `py_test` to `projects/monolith/semgrep/BUILD`)
- Modify: monolith pip deps to add pinned `semgrep==<pin>` (match the fc-invoke guest engine version); regenerate lock per repo convention
- Modify: `projects/monolith/deploy/values.yaml` - wire `SEMGREP_APP_TOKEN` from `op://k8s-homelab/semgrep-mcp/SEMGREP_API_TOKEN` (OnePasswordItem -> secretKeyRef env, mirror the goosecracker/whatsapp secret pattern)

**The relay (resolve the exact mapping - THE risk):**
1. Build `out.ProjectMetadata` from PR fields via `semgrep.meta` (branch, commit, pull_request_id, repo, `is_full_scan=False`).
2. `ScanHandler(...).start_scan(project_metadata, project_config)` -> per-PR `scan_id`.
3. Turn `raw_cli_output` into `List[RuleMatch]`. **Determine the exact function**: `core_output.core_matches_to_rule_matches` consumes semgrep-CORE matches, not CLI json; the CLI cli_output is post-RuleMatch. Spike both directions in this task: (a) parse cli_output results into `RuleMatch` directly, or (b) if lossy, have the guest emit core-match form. Whichever preserves `match_based_id` wins. If neither is clean, FALLBACK: guest returns findings AND their `match_based_id` explicitly and construct minimal `RuleMatch`.
4. `report_findings(matches_by_rule=..., rules=<ours>, ...)` then it POSTs `/results` + `/complete`.
5. On any exception after `start_scan`: `report_failure(exit_code)` in a `finally` (open scans MUST be closed, else the App wedges the PR check).

**Steps:** unit-test the mapping with a captured raw_cli_output fixture (assert RuleMatch count + one match_based_id) -> implement -> push/CI. Keep upload paths behind `dry_run` in the unit test (no live App in CI).

### Task 3: Prove it end-to-end against the live App (throwaway scan)

Run the relay in-pod (`kubectl exec` monolith backend, blocking python) against project `3263658`: scan one file via fc-invoke, report. **Verify:** a new scan appears at `semgrep.dev/orgs/jomcgi/projects/3263658/scans`; findings match; if run against a scratch PR, the native check posts. Document the observed App baseline behavior (does a diff scan of whole-file findings get deduped against main server-side?) - this decides Phase 3's approach. If reporting fails, iterate here before building any infra.

---

## Phase 2 - Webhook automation

### Task 4: HTTPRoute passthrough for /webhooks/github/semgrep
- Modify: `projects/monolith/chart/templates/httproute-private.yaml` - add a `PathPrefix /webhooks/github/semgrep` rule to `apiPort` (backend), placed BEFORE the catch-all `/` (else it hits SvelteKit and 404s, per the Grimoire-rule comment). NO SecurityPolicy (GitHub bypassed Access at the edge). Bump monolith chart.
- Cloudflare Access destination + IP-Bypass already set by Joe.

### Task 5: The webhook handler
- Create: `projects/monolith/semgrep/router.py` - `APIRouter`, `@router.post("/webhooks/github/semgrep")`, HMAC-verify `X-Hub-Signature-256` (copy `chat/whatsapp_inbound.py`'s `hmac.compare_digest` pattern), filter to `pull_request` opened/synchronize, dispatch the scan+relay in a background task.
- Create/modify: `projects/monolith/semgrep/__init__.py` - `app.include_router(router)` (mirror `chat/__init__.py`).
- New secret: GitHub webhook HMAC secret (new 1Password field -> monolith env), SEPARATE from SEMGREP_APP_TOKEN.
- Test: `router_test.py` (+ `py_test`) - signature accept/reject, event filter.

### Task 6: Register the GitHub hook + verify
- `gh api repos/jomcgi/homelab/hooks -X POST` (events pull_request, url `https://private.jomcgi.dev/webhooks/github/semgrep`, content_type json, secret=<hmac>).
- Open a scratch PR, confirm delivery 200 (redeliver like the ArgoCD verification), a scan appears in the App, and the check posts alongside SMS.

---

## Phase 3 - Correct baseline (only NEW findings gate)

### Task 7: Diff-aware scanning
Decide by Phase 1 Task 3's observed App behavior:
- If the App suppresses pre-existing findings server-side for a diff scan: keep whole-file scanning, just ensure `is_full_scan=False` + correct baseline metadata.
- Else: guest-side git-mirror hydration (head + main deepened to merge-base, per [[project_git_mirror_generic_egress]] WS2/WS3, but NOT the agent's `--depth=1 --single-branch` which breaks `git merge-base`), run `--baseline-commit`, return net-new findings.
### Task 8: Scheduled full scan on main (establishes/refreshes the baseline)
Cron -> same relay + guest, `is_full_scan=True`, no baseline, whole tree. Near-free once Phase 1-3 exist.

---

## Phase 4 - Cutover

### Task 9: Make Route B the required check, disable SMS
Only after Phase 2-3 are proven equivalent alongside SMS: set the Route B check required in branch protection, then Joe disables Managed Scans for the repo in the App (one action = SMS off). No coverage gap because Route B is already reporting.

---

## Open risks tracked
- pysemgrep internal-API stability across version bumps (pin + re-verify gate).
- cli_output -> RuleMatch fidelity (Task 2/3; fingerprints for dedup).
- App server-side baseline behavior for whole-file diff scans (Task 3 decides Phase 3).
- ReplacePrefixMatch/passthrough trailing-slash edge (verify like ArgoCD).
