# Goose Research Sub-Recipe Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give goosecracker's small-model (Qwen) agents a web-research capability: a `research` sub-recipe that searches via the in-cluster SearXNG JSON API and reads bounded page excerpts, reachable both as a router route and as a mid-task delegate from the plan/implement workers.

**Architecture:** The recipe is deployment-generic: it consumes a `SEARXNG_URL` env var injected per tier (monolith deploy values) and degrades gracefully when unset, so nothing monolith-specific is baked into recipe text. Search traffic rides the existing egress funnel; the only new network permission is one internal allowlist entry for the SearXNG service. Workers pass their research question via a file (`/tmp/goose/research-question.md`), never as a recipe parameter, because multi-line values substituted into recipe YAML break parsing (see the comment block in `agent.yaml`).

**Tech Stack:** goose recipes (YAML), apko/Wolfi guest image, Helm deploy values (monolith tiers, substrate egress allowlist).

**Verification model:** No local test loop in this repo. Each task ends in a commit; verification is `format` cleanliness locally, then end-of-plan CI on the pushed PR, then a post-deploy live check through a Discord agent thread.

**Deploy path notes:**

- Guest image/recipe changes auto-bump the fc-invoke chart in CI (PR #3042, `chart-version --keep_going`). No manual chart bump for guest content.
- Monolith and substrate `deploy/values.yaml` changes are `$values` git refs: ArgoCD picks them up on merge, no chart bump needed.

---

### Task 1: Create `research.yaml` sub-recipe

**Files:**

- Create: `projects/firecracker/goosecracker/guest/recipes/research.yaml`

**Step 1: Write the recipe**

Model it on `query.yaml` (same parameter block, same file-based task passing). Full content:

```yaml
version: "1.0.0"
title: "Research"
description: "Web research sub-recipe: search and read public sources to answer a question, confirm an assumption, or gather current external facts. Read-only."
instructions: |
  You are a web researcher working inside an isolated Firecracker microVM.
  Answer the research question below from public web sources and return a
  short, cited answer. You change nothing: no edits, no commits, no PRs.

  Capabilities:
  - Web search via SearXNG IF the SEARXNG_URL env var is set. Check once
    with `printenv SEARXNG_URL`. If it is empty or unset, skip searching
    and work from sources you can name directly: official docs URLs,
    GitHub repos (`gh api`, raw.githubusercontent.com), package registries.
  - `curl` for fetching, `jq` for JSON, `gh` for GitHub.

  To search, run EXACTLY this pipeline (do not invent flags):
    curl -s "${SEARXNG_URL}/search?q=YOUR+QUERY&format=json" | jq -r '.results[:5][] | "\(.title)\n\(.url)\n\(.content)\n"'
  Replace spaces in the query with +. Run ONE search, then read sources.
  Search again only if the results were clearly off-target, three searches
  maximum.

  Read at most 3 sources, each with a bounded fetch so a large page cannot
  overflow your context window:
    curl -sL "URL" | head -c 4000
  For markup-heavy pages strip tags first:
    curl -sL "URL" | sed -e 's/<[^>]*>//g' | tr -s '[:space:]' ' ' | head -c 4000
  For GitHub projects prefer `gh api` or the raw README/CHANGELOG URL over
  the HTML page.

  Rules:
  - READ ONLY, and stay small: never pull a whole page into context.
  - Prefer primary sources (official docs, the project's own repository,
    changelogs, release notes) over blogs and forums.
  - The caller uses you to CHECK things, so a confident wrong answer is
    worse than "unconfirmed". If sources disagree or you cannot confirm,
    say so plainly. Never present a guess as a confirmed fact.
  - Note publication dates when recency matters (versions, APIs, prices).

  Finish with a short, self-contained answer: the finding first, then the
  source URLs it rests on, then anything you could not confirm. Your final
  message is returned verbatim to the caller, so lead with the answer.
parameters:
  - key: task_file
    description: "Path to a file containing the research question"
    input_type: string
    requirement: required
  - key: context_file
    description: "Path to an optional file with caller-gathered context (what is already known, why the question matters); may be empty"
    input_type: string
    requirement: optional
    default: ""
prompt: |
  The research question is in the file {{ task_file }}. Read it in full
  first, for example `cat {{ task_file }}`.

  Caller context may be in the file {{ context_file }} (the path may be
  empty, meaning none): if it is a non-empty path, read it too, for example
  `cat {{ context_file }}`.
extensions:
  - type: builtin
    name: developer
settings:
  max_turns: 15
  max_tool_repetitions: 5
```

Note `max_turns: 15`, tighter than query's 25: research is a bounded side-quest (search, read up to 3 sources, answer), and a delegating worker is burning its own wall-clock while it waits.

**Step 2: Verify formatting**

Run: `cd /tmp/claude-worktrees/goose-research-recipe && format`
Expected: no diff beyond the new file (prettier may normalize it; keep whatever it produces).

**Step 3: Commit**

```bash
git add projects/firecracker/goosecracker/guest/recipes/research.yaml
git commit -m "feat(goosecracker): add research sub-recipe for web research via SearXNG"
```

---

### Task 2: Wire `research` into the router (`agent.yaml`)

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml`

**Step 1: Add the route description**

In the `Route the task to exactly one of:` list (after the `query` entry, before `plan`), add:

```
  - research: the task needs facts from OUTSIDE the repo and cluster:
    upstream docs, current versions, API details, best practices, product
    comparisons, news. ("what's the latest X", "how do I use library Y",
    "compare A vs B"). If the answer lives in this repo or this cluster,
    that is query, not research.
```

**Step 2: Register the sub-recipe**

In `sub_recipes:`, after the `query` entry, add:

```yaml
- name: research
  path: /home/goose-agent/recipes/research.yaml
  values:
    task_file: "{{ task_file }}"
    context_file: /tmp/goose/context.md
```

**Step 3: Extend the response schema**

In `response.json_schema.properties.mode.enum`, add `"research"`:

```yaml
enum: ["query", "plan", "implement", "artifact", "research"]
```

(`type` enum needs no change: research results are `answer`.)

**Step 4: Bump the recipe version**

`version: "1.6.0"` becomes `version: "1.7.0"`.

**Step 5: Format and commit**

```bash
format
git add projects/firecracker/goosecracker/guest/recipes/agent.yaml
git commit -m "feat(goosecracker): route research tasks to the research sub-recipe"
```

---

### Task 3: Make `research` delegable from the plan and implement workers

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/plan.yaml`
- Modify: `projects/firecracker/goosecracker/guest/recipes/implement.yaml`

**Step 1: Add the delegation guidance to both recipes' `instructions`**

Append this paragraph (adjust indentation to match each file's block scalar):

```
  When you are unsure about an EXTERNAL fact (an upstream API or flag, a
  library version, current best practice) do not guess and do not silently
  assume. Write ONE specific question to /tmp/goose/research-question.md
  (for example `printf '%s' 'what flags does X v2 accept for Y?' >
  /tmp/goose/research-question.md`), then dispatch the research sub-recipe
  and wait for its result. It searches the web and returns a short cited
  answer. Use it for external facts only: questions about THIS repo you
  answer yourself by reading the code. At most 2 research calls per task;
  if research cannot confirm something, state the uncertainty in your
  output rather than blocking.
```

**Step 2: Declare the sub-recipe in both files**

Add to each (creating the `sub_recipes:` block; neither file has one today). Note `task_file` is pinned to the fixed question file, NOT `{{ task_file }}`, because the worker's question differs from the thread task and multi-line values cannot pass through recipe parameters:

```yaml
sub_recipes:
  - name: research
    path: /home/goose-agent/recipes/research.yaml
    values:
      task_file: /tmp/goose/research-question.md
      context_file: /tmp/goose/context.md
```

**Step 3: Bump both recipe versions (minor)**

**Step 4: Format and commit**

```bash
format
git add projects/firecracker/goosecracker/guest/recipes/plan.yaml projects/firecracker/goosecracker/guest/recipes/implement.yaml
git commit -m "feat(goosecracker): let plan and implement workers delegate to research"
```

---

### Task 4: Add `curl` and `jq` to the guest image

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/apko.yaml`
- Regenerated: `projects/firecracker/goosecracker/guest/apko.lock.json`

**Step 1: Add packages**

In the alphabetized `packages:` list add `curl` (after `coreutils`) and `jq` (after `go`).

**Step 2: Regenerate the lock**

Run: `format`
Expected: `apko.lock.json` diff adds curl, jq, and their deps (libcurl etc.) for both arches. Commit only this lock.

**Step 3: Commit**

```bash
git add projects/firecracker/goosecracker/guest/apko.yaml projects/firecracker/goosecracker/guest/apko.lock.json
git commit -m "feat(goosecracker): add curl and jq to the guest image for the research recipe"
```

---

### Task 5: Fix the stale context-forge comment in guest `config.yaml`

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/config.yaml`

**Step 1: Correct the header comment**

The comment claims the context-forge extension provides "SigNoz observability, ArgoCD, Kubernetes via MCP gateway" to sessions. That is dead wiring: goose recipes' `extensions:` lists REPLACE the config set ("only the explicitly listed extensions will be utilized"), and every goosecracker session runs through a recipe that lists only `developer`. Rewrite the comment to state that the extensions here apply only to bare (non-recipe) sessions, that recipe extension lists override this file, and that guest MCP access is pending the per-tier MCP ACL ADR. Do not remove the extension entry itself (harmless, and bare-session debugging still uses it).

**Step 2: Commit**

```bash
git add projects/firecracker/goosecracker/guest/config.yaml
git commit -m "docs(goosecracker): correct stale context-forge extension comment"
```

---

### Task 6: Allow SearXNG in the guest egress allowlist

**Files:**

- Modify: `projects/firecracker/substrate/deploy/values.yaml` (the `egress.internal.allowlist` block, currently lines 53-58)

**Step 1: Add the entry**

```yaml
- monolith-searxng.monolith.svc.cluster.local:8080 # SearXNG JSON API (research sub-recipe web search)
```

Internal egress is deny-by-default; without this the recipe's search call is blocked at the sidecar. SearXNG is read-only search, so it is a low-risk pivot target, which is why it earns an allowlist slot.

**Step 2: Format and commit**

```bash
format
git add projects/firecracker/substrate/deploy/values.yaml
git commit -m "feat(substrate): allow guest egress to SearXNG for the research recipe"
```

---

### Task 7: Inject `SEARXNG_URL` into the goosecracker tiers

**Files:**

- Modify: `projects/monolith/deploy/values.yaml` (`goosecracker.tiers`, currently lines 173+)

**Step 1: Add the env var to BOTH tiers (`default` and `artifact`)**

Any thread's tier can route/delegate research, and the URL is not a secret, so both tiers get it. Use the FQDN (guest funnel + sidecar resolve it; matches the other tier URLs):

```yaml
# SearXNG JSON API for the research sub-recipe. Not a secret; the
# egress sidecar must also allowlist this host:port (substrate deploy
# values). Leave unset in deployments without SearXNG: the recipe
# skips web search gracefully.
SEARXNG_URL: http://monolith-searxng.monolith.svc.cluster.local:8080
```

**Step 2: Format and commit**

```bash
format
git add projects/monolith/deploy/values.yaml
git commit -m "feat(monolith): inject SEARXNG_URL into goosecracker guest tiers"
```

---

### Task 8: Push, PR, CI, merge

**Step 1: Push and open the PR**

```bash
git push -u origin feat/goose-research-recipe
gh pr create --title "feat(goosecracker): research sub-recipe with SearXNG web search" --body "..."
```

PR body should cover: the recipe design (SEARXNG_URL-generic, file-based question passing, bounded fetches for a 32k-window model), the three wiring points (router route + plan/implement delegates), the guest image additions, the two values changes, and the config.yaml comment fix. End with the standard generation footer.

**Step 2: Watch CI**

Run: `gh pr checks <number> --watch`
On failure, read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) before hypothesizing. Likely failure spots: apko lock drift (`{name}_lock_test`), recipe YAML formatting.

**Step 3: Merge**

`gh pr merge --auto --rebase`, then poll `gh pr view <number> --json state,mergeStateStatus` until merged.

---

### Task 9: Post-deploy verification (live cluster)

**Step 1: Confirm the rollout**

- fc-invoke chart auto-bumped by CI on main and synced: `kubectl get applications -n argocd` (fc-invoke app Healthy/Synced at the new revision).
- Monolith picked up the tier env: goosecracker threads created after sync carry `SEARXNG_URL`.

**Step 2: Live smoke test through Discord**

Open an agent thread with a research-shaped task, e.g. "research: what is the latest stable release of Firecracker and what changed in it?" Expected: router mode `research`, a cited answer with source URLs.

**Step 3: Check the SearXNG leg actually ran**

`kubectl logs -n monolith deploy/monolith-searxng --since=15m | grep search` (or check the egress-proxy sidecar logs in the fc-invoke pod for the allowed `monolith-searxng` connection). If the sidecar denied it, the allowlist entry host string does not match what the guest sent: compare against the funnel preamble log line.

**Step 4: Delegation smoke test (best-effort)**

A plan-shaped task that needs an external fact, e.g. "plan: how should we upgrade our SearXNG deployment to the latest image?" Watch whether the worker writes `/tmp/goose/research-question.md` and dispatches research (visible in the goose session transcript via /improve-recipes tooling). If the worker never delegates, that is recipe-prompt tuning for the /improve-recipes loop, not a rollback.
