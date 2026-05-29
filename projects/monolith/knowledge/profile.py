"""Joe McGinley's relevance + privacy profile as typed Python constants.

Single source of truth for what's worth keeping in the vault (relevance)
and what's safe to publish (privacy). Read by classifier prompts,
visibility helpers, and ad-hoc classification subagents.

Replaces the prior joe-profile.md file (PR #2371) -- all rubric content is
Python to avoid markdown-vs-code drift and to enable importable, testable
constants. Edits go through PR review. Bump PROFILE_VERSION on substantive
changes so downstream consumers can pin or refuse stale rubrics.

## How to use this profile

For agents classifying atoms:
- Import the constants directly (RELEVANCE_KEEP, PRIVATE_CATEGORIES, etc.).
- For each atom, score against BOTH the relevance KEEP/SKIP tables AND
  the privacy categories.
- Privacy criteria are binding constraints; relevance is a heuristic.
- When uncertain on privacy -> 'private' (NOT 'public').

For classifier prompt code (gap_classifier.py, visibility.py):
- Reference these constants directly rather than hardcoding rubrics.
- Pin against PROFILE_VERSION so prompt regressions surface as test
  failures rather than silent classification drift.

For Joe (future-me):
- If a category here has not been updated in 6 months and your work
  has obviously shifted, something is stale. The two-question test
  is the constant; the seed lists evolve. Bump PROFILE_VERSION and
  PROFILE_UPDATED whenever you make a substantive change.
"""

from __future__ import annotations

PROFILE_VERSION = "2"
PROFILE_UPDATED = "2026-05-28"  # nosemgrep: test-hardcoded-past-timestamp (audit-trail constant; manually bumped on rubric edits)

IDENTITY = """\
Senior Platform Engineer @ Semgrep (Vancouver, BC). Career thesis:
remove complexity for other engineers; business value as a byproduct.
Former marine underwriter at Chubb. Public CV: https://jomcgi.dev.
"""

RELEVANCE_KEEP: list[dict[str, str]] = [
    {
        "domain": "eBPF / kernel observability",
        "signals": "cgroup_skb, BPF maps, Hubble, kfunc, BTF, bpftrace",
    },
    {
        "domain": "Kubernetes at scale",
        "signals": "multi-cluster, control-plane internals, operators (Go), CRDs, admission webhooks",
    },
    {
        "domain": "Service mesh",
        "signals": "Linkerd primarily, Envoy, Gateway API, Cloudflare ZeroTrust",
    },
    {
        "domain": "Observability / SLO",
        "signals": "OTel internals, SigNoz, Honeycomb, Prometheus, RCA, STAMP/STPA",
    },
    {
        "domain": "FinOps / cost attribution",
        "signals": "eBPF cost meters, AWS CUR, Orb, Iceberg/Snowflake cost, OpenCost (rejected; know why)",
    },
    {
        "domain": "Data platform",
        "signals": "SQLMesh, dbt, Postgres pgvector/HNSW, Neo4j, Iceberg, BigQuery, DuckDB",
    },
    {
        "domain": "Build systems",
        "signals": "Bazel/Starlark, BuildBuddy RBE, rules authoring, aspects",
    },
    {
        "domain": "AI infra",
        "signals": "vLLM, MCP, Claude/Anthropic, agent orchestration, prompt caching, transformer internals (Mamba, RoPE, SwiGLU, RMSNorm)",
    },
    {
        "domain": "Security / AppSec / Semgrep",
        "signals": "rules, AST/dataflow, taint mode, CWE, supply-chain (defensive)",
    },
    {
        "domain": "Knowledge graphs / PKM",
        "signals": "Obsidian core patterns, Andy Matuschak, spaced repetition, Tools for Thought",
    },
    {
        "domain": "Coffee",
        "signals": "origin/processing, brewing, cupping, roasters, lever espresso (Decent etc.)",
    },
    {
        "domain": "Philosophy - analytic/skeptical/Stoic only",
        "signals": "Sextus Empiricus, Pyrrho, Kahneman, Tversky, Kripke, Wittgenstein, Stoics, philosophy of science, decision theory",
    },
    {
        "domain": "Career / leadership / SRE methodology",
        "signals": "refactoring, TDD, organizational design, distributed systems theory",
    },
]

RELEVANCE_SKIP: list[dict[str, str]] = [
    {
        "category": "Frontend framework fads",
        "examples": "React/Vue/Vite/Remix deep dives NOT related to monolith Svelte 5 work",
    },
    {
        "category": "AI-IDE vendor profiles",
        "examples": "Cursor, Replit, Codecademy, Windsurf, Codespaces",
    },
    {
        "category": "Pop culture / gaming / consumer tech",
        "examples": "Valve, Dota, Stadia, watchOS, Half-Life, Counter-Strike, Roblox, Wordle",
    },
    {
        "category": "Vendor marketing / M&A trivia",
        "examples": "mergers, acquisitions, IPOs of non-strategic companies",
    },
    {
        "category": "Wikipedia-stub web fundamentals",
        "examples": "HTTP, DNS, HTML, SVG, WebRTC at primer level",
    },
    {
        "category": "Religious / theological / patristic content",
        "examples": "Christology, Gospels, Church Fathers, apologetics - NOT analytic philosophy",
    },
    {
        "category": "Pop history / political history",
        "examples": "FDR-era, US politics, EU institutions, Brexit, art/music/poetry/literature (Lorca/Blake/Ellington/Whitman), classical/patristic history, MBTI/Jung, pop-science Dawkins-tier",
    },
    {
        "category": "Obsidian PLUGIN-specific minutiae",
        "examples": "DataviewJS plugin tutorials, plugin security notes for plugins not in use",
    },
]

RELEVANCE_EMPLOYER_CARVE_OUTS = (
    "BenchSci is allowed in depth (3yr prior employer); the "
    "Sky/AXA/Hometree/Ensono atoms about Joe's own platform work there are "
    "KEEP (CV-relevant); generic market commentary about those firms is SKIP."
)

VISIBILITY_CRITERIA = """\
## Visibility (REQUIRED frontmatter field)

Every note MUST set `visibility: public` or `visibility: private`.
This controls whether the note appears on Joe's public website.

Default to `private` whenever you are uncertain.

Mark `public` when the note is about:
- General engineering concepts, principles, heuristics (DORA, Conway's Law,
  blameless postmortems, etc.) -- anything you'd find in a textbook, blog,
  or conference talk.
- Skills, technologies, or methods covered in Joe's public CV / GitHub /
  conference talks.
- Verifiable facts about external systems, libraries, protocols, or tools.
- Book / paper / talk summaries when the source is publicly available.

Mark `private` when the note involves any of:
- Names of current or former colleagues, managers, reports, or interviewers.
- Specific employers in non-public ways: project codenames, internal
  architecture, compensation, performance reviews, hiring decisions.
- Job-search activity: interview prep, comp negotiation, target companies,
  reasons-for-leaving, offer comparisons.
- Personal life: family, finances, health, relationships, legal matters,
  living situation.
- Critiques or hot takes about identifiable people or companies that
  aren't already in Joe's public writing.
- Active tasks, daily/weekly journals, blockers -- anything operational
  about Joe's current work.

Edge cases:
- An atom about a generally-applicable pattern that includes a
  workplace-specific example: rewrite the example out and mark public,
  OR keep the example and mark private. Do not mark public with the
  example intact.
- A fact about an external library mentioned during a private incident:
  the fact is public, the incident framing is private -- split into two
  notes if needed.

When in doubt: `private`.
"""

PRIVATE_CATEGORIES: list[dict[str, list[str] | str]] = [
    {
        "name": "Job search / career mobility",
        "seeds": [
            "job-search",
            "interview",
            "recruiter",
            "offer",
            "resignation",
            "looking for",
            "application",
            "FAANG",
            "LeetCode-prep",
            "take-home",
            "reference-request",
        ],
    },
    {
        "name": "Compensation",
        "seeds": [
            "salary",
            "comp",
            "equity",
            "RSU",
            "vesting",
            "bonus",
            "raise",
            "total-comp",
            "levels.fyi",
            "negotiation",
        ],
    },
    {
        "name": "Performance / 1-on-1 / manager dynamics",
        "seeds": [
            "performance-review",
            "1-on-1",
            "PIP",
            "manager-critique",
            "frustrated with",
            "concerns about",
            "peer-feedback",
            "calibration",
        ],
    },
    {
        "name": "Current-employer internals",
        "seeds": [
            "Semgrep customer specifics beyond what's in the CV",
            "internal Semgrep roadmaps not yet public",
            "internal headcount/financials",
            "internal disputes",
        ],
    },
    {
        "name": "Friends, family, peers",
        "seeds": [
            "Brian",
            "Bjeu",
            "bjeambusher",
            "rossdd",
            "ochinchin",
            "scotscottmca",
            "family members",
            "Discord channel snapshots/profiles",
            "person:peer",
            "anyone whose primary reference is a Discord channel",
        ],
    },
    {
        "name": "Health (mental or physical)",
        "seeds": [
            "therapy",
            "therapist",
            "diagnosis",
            "medication",
            "anxiety",
            "depression",
            "sleep issues",
            "sobriety",
            "ADHD",
        ],
    },
    {
        "name": "Personal finance",
        "seeds": [
            "net worth",
            "mortgage",
            "debt",
            "savings rate",
            "FIRE plans",
            "specific account balances",
            "tax strategy",
        ],
    },
    {
        "name": "Relationships / dating",
        "seeds": [
            "partner",
            "dating",
            "relationship-issues",
            "breakup",
            "frank-dating-*",
        ],
    },
    {
        "name": "Glasgow / Vancouver civic personal",
        "seeds": [
            "local political views",
            "neighbour disputes",
            "anything tying Joe to a specific physical address or routine",
        ],
    },
]

ASYMMETRIC_ERROR_PREFERENCE = "private"

__all__ = [
    "PROFILE_VERSION",
    "PROFILE_UPDATED",
    "IDENTITY",
    "RELEVANCE_KEEP",
    "RELEVANCE_SKIP",
    "RELEVANCE_EMPLOYER_CARVE_OUTS",
    "VISIBILITY_CRITERIA",
    "PRIVATE_CATEGORIES",
    "ASYMMETRIC_ERROR_PREFERENCE",
]
