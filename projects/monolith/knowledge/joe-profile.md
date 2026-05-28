---
title: Joe McGinley — Relevance & Privacy Profile
version: 1
updated: 2026-05-28
---

# Joe McGinley — Profile

Single source of truth for what's worth keeping in the vault (relevance) and what's safe to publish (privacy). Edited freely; agents and classifier prompts read this at runtime rather than hardcoding rubrics.

## Identity

Senior Platform Engineer @ Semgrep (Vancouver, BC). Career thesis: _remove complexity for other engineers; business value as a byproduct._ Former marine underwriter at Chubb. Public CV: <https://jomcgi.dev>.

---

## Relevance — KEEP

| Domain                                     | Lights up                                                                                                              |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| eBPF / kernel observability                | cgroup_skb, BPF maps, Hubble, kfunc, BTF, bpftrace                                                                     |
| Kubernetes at scale                        | multi-cluster, control-plane internals, operators (Go), CRDs, admission webhooks                                       |
| Service mesh                               | **Linkerd primarily**, Envoy, Gateway API, Cloudflare ZeroTrust                                                        |
| Observability / SLO                        | OTel internals, SigNoz, Honeycomb, Prometheus, RCA, STAMP/STPA                                                         |
| FinOps / cost attribution                  | eBPF cost meters, AWS CUR, Orb, Iceberg/Snowflake cost, OpenCost (rejected; know why)                                  |
| Data platform                              | SQLMesh, dbt, Postgres pgvector/HNSW, Neo4j, Iceberg, BigQuery, DuckDB                                                 |
| Build systems                              | Bazel/Starlark, BuildBuddy RBE, rules authoring, aspects                                                               |
| AI infra                                   | vLLM, MCP, Claude/Anthropic, agent orchestration, prompt caching, transformer internals (Mamba, RoPE, SwiGLU, RMSNorm) |
| Security / AppSec / Semgrep                | rules, AST/dataflow, taint mode, CWE, supply-chain (defensive)                                                         |
| Knowledge graphs / PKM                     | Obsidian _core_ patterns, Andy Matuschak, spaced repetition, Tools for Thought                                         |
| Coffee                                     | origin/processing, brewing, cupping, roasters, lever espresso (Decent etc.)                                            |
| Philosophy — analytic/skeptical/Stoic only | Sextus Empiricus, Pyrrho, Kahneman, Tversky, Kripke, Wittgenstein, Stoics, philosophy of science, decision theory      |
| Career / leadership / SRE methodology      | refactoring, TDD, organizational design, distributed systems theory                                                    |

## Relevance — SKIP

| Category                                    | Examples                                                                                                                                                                     |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Frontend framework fads                     | React/Vue/Vite/Remix deep dives NOT related to monolith Svelte 5 work                                                                                                        |
| AI-IDE vendor profiles                      | Cursor, Replit, Codecademy, Windsurf, Codespaces                                                                                                                             |
| Pop culture / gaming / consumer tech        | Valve, Dota, Stadia, watchOS, Half-Life, Counter-Strike, Roblox, Wordle                                                                                                      |
| Vendor marketing / M&A trivia               | mergers, acquisitions, IPOs of non-strategic companies                                                                                                                       |
| Wikipedia-stub web fundamentals             | HTTP, DNS, HTML, SVG, WebRTC at primer level                                                                                                                                 |
| Religious / theological / patristic content | Christology, Gospels, Church Fathers, apologetics — NOT analytic philosophy                                                                                                  |
| Pop history / political history             | FDR-era, US politics, EU institutions, Brexit, art/music/poetry/literature (Lorca/Blake/Ellington/Whitman), classical/patristic history, MBTI/Jung, pop-science Dawkins-tier |
| Obsidian PLUGIN-specific minutiae           | DataviewJS plugin tutorials, plugin security notes for plugins not in use                                                                                                    |

**BenchSci is allowed in depth** (3yr prior employer); the Sky/AXA/Hometree/Ensono atoms about Joe's own platform work there are KEEP (CV-relevant); generic market commentary about those firms is SKIP.

---

## Privacy — defaults & criteria

**Default**: `visibility: public`. Privacy is the minority case. This profile, the CV, the homelab repo, and most professional reflection are all public-default.

### The two-question test for `visibility: private`

1. Would Joe be upset if an employer or colleague saw this?
2. Is it personal about Joe's friends or family?

If either answer is yes → private. Otherwise → public.

### Private categories with seed terms

Any of these signals in an atom's title, body, or tags should bias toward `visibility: private`.

| Category                                    | Seed terms / signals                                                                                                                                                                                             |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Job search / career mobility**            | job-search, interview, recruiter, offer, resignation, "looking for", application, FAANG, LeetCode-prep, take-home, reference-request                                                                             |
| **Compensation**                            | salary, comp, equity, RSU, vesting, bonus, raise, total-comp, levels.fyi, negotiation                                                                                                                            |
| **Performance / 1-on-1 / manager dynamics** | performance-review, 1-on-1, PIP, manager-critique, "frustrated with", "concerns about", peer-feedback, calibration                                                                                               |
| **Current-employer internals**              | Semgrep customer specifics beyond what's in the CV, internal Semgrep roadmaps not yet public, internal headcount/financials, internal disputes                                                                   |
| **Friends, family, peers**                  | named friends (Brian, Bjeu, bjeambusher, rossdd, ochinchin, scotscottmca, etc.), family members, Discord channel snapshots/profiles, anyone tagged `person:peer` or whose primary reference is a Discord channel |
| **Health (mental or physical)**             | therapy, therapist, diagnosis, medication, anxiety, depression, sleep issues, sobriety, ADHD                                                                                                                     |
| **Personal finance**                        | net worth, mortgage, debt, savings rate, FIRE plans, specific account balances, tax strategy                                                                                                                     |
| **Relationships / dating**                  | partner, dating, relationship-issues, breakup, frank-dating-\* (existing convention)                                                                                                                             |
| **Glasgow / Vancouver civic personal**      | local political views, neighbour disputes, anything tying Joe to a specific physical address or routine                                                                                                          |

### Public-by-default if NONE of the above match

Generic professional knowledge, third-party concepts, opinion atoms about technology/methodology/leadership in the abstract, book notes, coffee notes, philosophy notes, project retrospectives that don't name people or expose employer internals.

### Asymmetric error preference

- Wrong-private = a few unnecessary clicks later (you can always upgrade to public).
- Wrong-public = an employer / colleague / family member sees something they shouldn't, possibly indexed at `public.jomcgi.dev`.

**When in doubt → private.** Privacy-conservative is the right error direction.

---

## How to use this profile

For **agents classifying atoms**:

- Read this file via `(Path(__file__).parent / "joe-profile.md").read_text()` or equivalent.
- For each atom, score against BOTH the relevance KEEP/SKIP tables AND the privacy criteria.
- Treat the privacy criteria as **binding constraints**; the relevance tables are heuristics.
- When uncertain on privacy → `visibility: private` (NOT `public`); when uncertain on relevance → leave the atom alone (no auto-delete).

For **classifier prompts** (`gap_classifier.py`, future `visibility_classifier.py`):

- Reference this profile by path rather than hardcoding categories in the prompt string.
- Pin against this file's `version` value so prompt regressions surface as test failures rather than silent classification drift.

For **Joe (future-me)**:

- Drift signal: if a category here hasn't been updated in 6 months and your work has obviously shifted, something is stale.
- Add new privacy categories as they surface (e.g., a new sensitive topic emerges). The two-question test is the constant; the seed lists evolve.
- Bump `version` and `updated` whenever you make a substantive change so consumers know to re-pin.

---

## Changelog

- **2026-05-28 v1**: Initial draft. Relevance tables from the gating-PR session; privacy criteria + categories from the visibility-classification calibration that surfaced the job-search risk. Public-default + asymmetric-error-preference established.
