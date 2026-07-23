---
name: stpa
description: Generate or refresh the STPA (System-Theoretic Process Analysis, STPA-Sec) safety model for one system in this repo, written colocated as <system>/STPA.md, landing any change as a PR against main that merges (rebase) on green CI. Use when asked to update/regenerate a system's STPA safety analysis, run the STPA routine, audit unsafe control actions or unsafe feedback (data staleness, corruption, provenance drift), or when invoked on a schedule. Takes a `system` argument (the directory to analyze, e.g. projects/monolith). The analysis is deterministic, findings are extracted as JSON (judgment) and the markdown is rendered by an embedded jq renderer (mechanism), so scheduled runs produce small reviewable diffs.
---

Generate/update the STPA safety model for ONE system in this repository, written
colocated as `<system>/STPA.md` (e.g. `projects/monolith/STPA.md`), and land any
change as a PR against `main` that you merge (rebase) once CI is green.

The `system` argument is the directory to analyze. If not given, ask which system
(default `projects/monolith`). The analysis scope is that subtree.

You are a systems-safety analyst applying STPA (System-Theoretic Process Analysis,
Leveson) in the STPA-Sec tradition: "losses" are security and data-integrity
violations, not just physical harm. You will EXTRACT findings as JSON (judgment),
RENDER the document by running BLOCK A verbatim (mechanism), and, only if it
changed, open/refresh a PR via BLOCK B. Determinism, and therefore small,
reviewable diffs on every scheduled run, depends on you producing the markdown
ONLY via the renderer. Do NOT hand-write or reformat the markdown. (`**/STPA.md`
is in `.prettierignore` so the renderer stays the sole authority.)

## Two views: logical and physical

Every finding carries a `view`:

- **logical**: the functional control structure. For an app, that is the
  request/command flow: routers, domain `public()` functions, schedulers,
  transactions, the capability/authorization boundaries enforced in the code
  path. Losses are about governed correctness of data access and provenance.
- **physical**: the deployment realization. Pods/binaries, namespaces and
  NetworkPolicy, database schemas and grants, ingress, secret delivery
  (1Password operator), GitOps sync. Losses are about secret exposure, privilege
  escalation across boundaries, and ingress/policy bypass.

A system may legitimately model only one view (e.g. a build system has a rich
physical/CI-execution view and a thin logical one). Omit the view you cannot
ground; do not invent one.

────────────────────────────────────────────────────────

## Target invariants per system

Derive the mission and reason-to-exist from the system itself: read its
`README`, `CLAUDE.md`, design docs, and code. Do not assume an architecture from
prior knowledge. The reason a system exists defines what "unsafe" means: an
unsafe state violates the mission, it is not merely a crash.

Seeded example (`projects/monolith`): a single-binary personal platform split
into domains over per-domain Postgres schemas, deployed as a secret-less
read-only public binary and a privileged private binary (see
`docs/decisions/services/010-fastmonolith-modular-framework.md`). Reason to exist
= governed correctness of data access plus the secret-less public boundary.
Unsafe states violate those.
────────────────────────────────────────────────────────

## Steps

1. **Read the prior, if any.** If `<system>/STPA.md` exists, read it: it is your
   prior analysis. Its tables carry the stable semantic keys (first column),
   conditions, and evidence. Reuse them to minimize drift.
2. **Ground yourself in the code.** List the system subtree; read its READMEs,
   architecture/design docs, AND any "future work / deferred / roadmap" doc
   (discover by listing, do not assume filenames; it is the authoritative ledger
   of designed-but-not-built). Read the code units that issue or receive commands
   (routers, domain functions, schedulers, queues, transactions, worker loops).
   Trust CODE over docs on conflict; note the conflict in `scope`. Get the
   commit: `git rev-parse --short HEAD`.
3. **Analyze (STPA steps 1-3) per view.** Hazards = system states that, worst
   case, lead to a loss (map to loss keys). Control structure = controllers,
   controlled processes (nodes), control actions (commands, node to node),
   feedback (signals, node to node), each tagged `built` or `designed` and a
   `view`. UCAs = for each control action test four guidewords and keep ONLY
   genuinely-unsafe ones: `providing`, `not-providing`, `wrong-timing`,
   `wrong-duration`. A bounded nuisance recovered by a fallback is NOT a UCA, put
   it in `non_ucas`.
   Then analyze the FEEDBACK and data channels the same way. A controller acting
   on a wrong process model is an STPA cause, not a component failure: for each
   feedback signal, and for each data flow a controller's decisions depend on
   (caches, mirrors, replicas, fetched policy, queue state, lineage), test four
   guidewords and keep only genuinely-unsafe ones as `unsafe_feedback`:
   `missing` (never arrives; the controller acts on absence), `stale` (reflects
   an old state by the time it is acted on), `corrupted` (wrong content that
   passes silently), `unauthorized-source` (spoofable or unauthenticated
   origin). This is where data-integrity failures live: staleness windows, lost
   updates, cache or mirror drift, provenance divergence. Do NOT restate a data
   failure as a strained control-action UCA (e.g. a check-then-act staleness
   window is `<channel>.stale`, not `<action>.wrong-timing`). The same
   rejection bar applies: a bounded nuisance with a recovery path goes in
   `non_ucas`.
4. **Write your findings** as a single JSON object (schema below) to
   `/tmp/stpa.json`. Set `system_dir` to the system directory.
5. **Render** by running BLOCK A VERBATIM. It renders `/tmp/STPA.candidate.md`
   from your JSON deterministically, compares it against what `origin/main`
   actually has (this checkout may be stale), and prints either
   `STPA_RESULT=nochange` or `STPA_RESULT=changed`. It does NOT touch the
   working tree; BLOCK B lands the file from a disposable worktree.
6. **If `nochange`:** report "no change, STPA.md already current" and STOP. Do not
   open a PR.
7. **If `changed`:** prepare the PR text. Run
   `diff -u /tmp/STPA.prior.md /tmp/STPA.candidate.md` to see exactly what moved
   (no prior file means this is the first version). Then:
   - Write a Conventional-Commits title to `/tmp/stpa-title.txt`, ONE line, <=72
     chars, form `docs(stpa): <what changed>` (e.g.
     `docs(stpa): add queue.dequeue wrong-timing UCA for monolith`; first run:
     `docs(stpa): add monolith control-plane safety model`).
   - Write a concise markdown body to `/tmp/stpa-body.md`, 2-6 bullets naming the
     UCAs / hazards / control-actions added, removed, or materially changed (by
     their semantic keys), plus any scope/maturity shift. Summarize, do not paste
     the diff.
8. **Open/refresh the PR and merge on green** by running BLOCK B VERBATIM. It
   commits on a stable per-system branch, force-pushes, opens or updates a PR
   against `main`, watches CI, and rebase-merges once all checks pass. Report the
   PR URL and whether it merged or was left open for review.

## Remediation work goes to GitHub Issues (source of truth)

`STPA.md` is the safety *analysis*, not a work tracker. When the analysis surfaces
an unmitigated UCA or unsafe-feedback finding that needs code or config work to
close, file it as a **GitHub issue** (the repo's source of truth for outstanding
work), titled `<system>: <finding> — STPA <ucaKey>`, labelled `bug` (a broken/unsafe
control action) or `enhancement` (a missing safeguard), plus `critical` when the loss
is severe. Reference the issue from the finding's evidence rather than adding a
checklist to `STPA.md`. Do not open issues for `non_ucas` (bounded nuisances with a
recovery path) or for findings that already have a mitigation in the control
structure.

## Drift minimization (this is an UPDATE, not a rewrite)

The rendered doc is committed and reviewed as a diff, keep churn minimal:

- REUSE each prior finding's semantic key VERBATIM when the code it cites still
  exists and still means the same thing.
- KEEP prior `condition`/`statement`/`label`/`scope` wording UNCHANGED unless the
  underlying code changed. Do not reword for style.
- Update an `evidence` `path:line` only if the referenced code actually moved.
- But RE-VERIFY, never parrot: open each prior finding's cited code and confirm it
  still holds. Logic changed -> update it. Cited code deleted -> drop it. New
  unsafe control action -> add it with a new key.
  Net: unchanged code => identical finding; changed code => localized change;
  added/removed code => added/removed finding. Nothing else moves.

## Semantic keys (what makes diffs stable, the renderer sorts by them)

Derive each `key` from WHAT IT IS, never its position. Lowercase, no spaces.

- nodes: short component slug, NO dots (mermaid id): `acl`, `public-api`,
  `scheduler`, `postgres`, `public-binary`.
- losses: `L.unauthorized-access`, `L.integrity-loss`, `L.silent-incorrectness`,
  `L.provenance-loss`, `L.secret-exposure`, `L.liveness-loss`.
- control_actions: `<component>.<operation>`: `acl.check`, `public.resolve`,
  `scheduler.dispatch`, `tx.commit`.
- hazards: condition slug: `stale-policy`, `partial-atomic-unit`,
  `secret-in-public-binary`, `cross-schema-grant`.
- ucas: `<control_action-key>.<guideword>`: `scheduler.dispatch.wrong-timing`.
- unsafe_feedback: `<channel-slug>.<guideword>` where the channel slug names the
  signal or data flow itself, never its position: `policy-fetch.stale`,
  `queue-notify.missing`, `mirror-sync.corrupted`.
  `from`/`to`/`control_action`/`hazards`/`losses` reference other objects BY KEY.

## Grounding rules (hard)

- Every node, control_action, uca has `evidence`: `path:line` (or `path`); for a
  doc/comment, also a <=8-word verbatim quote. No evidence -> omit it.
- NEVER mark a designed-only element `built`.
- No invention. Uncertainty -> `open_questions`, not a guessed UCA.
- Prioritize, do not pad. Keep `condition`/`statement` to ONE line, no `|`, no
  newlines.

## JSON schema (write to /tmp/stpa.json; arrays in ANY order, the renderer sorts)

```
{
  "system": "string (human label, e.g. 'monolith')",
  "system_dir": "projects/monolith",
  "commit": "short-sha",
  "scope": {"summary":"1 sentence headline: the built-vs-designed framing.","built":"what is actually built (comma list)","designed":"what is designed-only (comma list)","note":"OPTIONAL: code-vs-docs conflict or key caveat, OMIT the field entirely if none"},
  "losses": [ {"key":"L.unauthorized-access","title":"..."}, ... ],
  "nodes": [ {"key":"acl","label":"Acl check","view":"logical","layer":"enforcement","maturity":"built"}, ... ],
  "control_actions": [ {"key":"acl.check","label":"...","view":"logical","from":"public-api","to":"acl","maturity":"built","evidence":"projects/monolith/.../acl.py:126"}, ... ],
  "feedback": [ {"view":"logical","from":"postgres","to":"scheduler","signal":"NOTIFY wakeup hint"}, ... ],
  "hazards": [ {"key":"stale-policy","view":"logical","statement":"...","losses":["L.unauthorized-access"],"maturity":"built"}, ... ],
  "ucas": [ {"key":"scheduler.dispatch.wrong-timing","view":"logical","control_action":"scheduler.dispatch","guideword":"wrong-timing","condition":"...","severity":"high","hazards":["premature-reclaim"],"evidence":"projects/monolith/.../scheduler.py:153"}, ... ],
  "unsafe_feedback": [ {"key":"policy-fetch.stale","view":"logical","from":"acl","to":"public-api","signal":"policy rows for subject+target","guideword":"stale","condition":"...","severity":"high","hazards":["stale-policy"],"evidence":"projects/monolith/.../acl.py:126"}, ... ],
  "non_ucas": [ {"item":"await missed NOTIFY","reason":"bounded by 5s poll fallback (path:line)"}, ... ],
  "open_questions": [ "string", ... ]
}
```

`view` is `logical` or `physical`. `layer` is a free-form subgraph group WITHIN a
view (e.g. `enforcement`, `control-plane`, `store` for logical; `ingress`,
`compute`, `data`, `secrets` for physical). `severity` is `high|medium|low`.
UCA guidewords: `providing|not-providing|wrong-timing|wrong-duration`.
unsafe_feedback guidewords: `missing|stale|corrupted|unauthorized-source`.
`unsafe_feedback` is optional: omit the array entirely when no feedback channel
survives the rejection bar.

## BLOCK A — render + detect change (run verbatim; never hand-write the .md)

````bash
set -euo pipefail
SYSTEM_DIR="$(jq -r '.system_dir' /tmp/stpa.json)"
test -n "$SYSTEM_DIR" -a "$SYSTEM_DIR" != "null"
cat > /tmp/stpa-render.jq <<'JQ'
# Deterministic STPA.md renderer.
#   input : stpa.json  (semantic-keyed findings, ANY array order)
#   output: STPA.md    (sorted + templated; a pure function of content)
# Identical findings render to identical bytes regardless of input order, so a
# regenerated doc diffs only where the findings actually changed.

def esc: (. // "") | gsub("\\|"; "\\|") | gsub("\n"; " ");
# Sanitize a string for a (quoted) mermaid label: drop chars that break the
# parser or get read as HTML. Parens are fine once the label is quoted.
def mlabel: (. // "") | gsub("\n"; " ") | gsub("\""; "'") | gsub("<"; "(") | gsub(">"; ")");
def mid: gsub("[^a-zA-Z0-9_]"; "_");

# Mermaid flowchart for all nodes/actions/feedback in one view.
def diagram($v):
  ([.nodes[]? | select(.view == $v)]) as $ns
  | ([.control_actions[]? | select(.view == $v)]) as $cas
  | ([(.feedback // [])[]? | select((.view // $v) == $v)]) as $fbs
  | if ($ns | length) == 0
    then "_No \($v) control structure modeled._\n"
    else "```mermaid\nflowchart TD\n"
      + ( $ns | sort_by(.layer // "zzz", .key)
          | group_by(.layer // "zzz")
          | map( (.[0].layer // "other") as $L
              | "  subgraph \($L|mid)[\"\($L)\"]\n"
              + ( map("    \(.key)[\"\(.label|mlabel)\(if .maturity=="designed" then " (designed)" else "" end)\"]") | join("\n") )
              + "\n  end\n" )
          | join("") )
      + ( $cas | sort_by(.key) | map("  \(.from) -- \"\(.key)\" --> \(.to)") | join("\n") )
      + "\n"
      + ( $fbs | sort_by("\(.from)|\(.to)|\(.signal)") | map("  \(.from) -. \"\(.signal|mlabel)\" .-> \(.to)") | join("\n") )
      + "\n```\n"
    end;

"# STPA Control Analysis: \(.system) @ \(.commit)\n\n"

+ "_Auto-generated STPA safety model: the unsafe states this system can reach and the control actions that get it there. Two views: logical (functional control flow) and physical (deployment)._\n\n"

+ "<details>\n<summary><b>How to read this</b>: STPA primer and diagram legend</summary>\n\n"
+ "**STPA** (System-Theoretic Process Analysis) treats the system as *controllers* issuing *control actions* to *controlled processes*, with *feedback* flowing back up. Instead of \"what component can fail,\" it asks \"what control action, given or withheld at the wrong time, drives the system into an unsafe state?\" \"Unsafe\" means a violation of this system's reason to exist, not merely a crash.\n\n"
+ "Read top-down: **Losses** are outcomes we must never cause; **Hazards** are system states that lead to a loss; the **control-structure diagrams** (one per view) show who commands whom (solid arrows = control actions, dashed = feedback, a node tagged `(designed)` is in the architecture but **not yet built**); the **Unsafe Control Actions** table is the core, and **Unsafe Feedback** covers the dashed arrows: data channels whose absence, staleness, corruption, or spoofing drives a controller into a hazard. Every claim cites `path:line`; unbuilt elements are marked. Semantic, stable IDs mean regenerating changes only the findings that changed.\n</details>\n\n"

+ "**Scope.** \(.scope.summary|esc)\n\n"
+ "<details>\n<summary>Maturity detail</summary>\n\n"
+ "- **Built:** \(.scope.built|esc)\n"
+ "- **Designed-only:** \(.scope.designed|esc)\n"
+ (if (.scope.note // null) then "- **Note:** \(.scope.note|esc)\n" else "" end)
+ "</details>\n\n"

+ "## Control structure\n\n"
+ "### Logical view\n\n" + diagram("logical") + "\n"
+ "### Physical view\n\n" + diagram("physical") + "\n"

+ "## Losses\n\n| ID | Loss |\n|----|------|\n"
+ ( .losses | sort_by(.key) | map("| `\(.key)` | \(.title|esc) |") | join("\n") ) + "\n\n"

+ "## Hazards\n\n| ID | View | Hazard (unsafe state) | → Losses | Maturity |\n|----|----|----|----|----|\n"
+ ( .hazards | sort_by(.key) | map("| `\(.key)` | \(.view|esc) | \(.statement|esc) | \((.losses // [])|join(", ")) | \(.maturity|esc) |") | join("\n") ) + "\n\n"

+ "## Control actions\n\n| ID | View | Control action | Controller → Process | Maturity | Evidence |\n|----|----|----|----|----|----|\n"
+ ( .control_actions | sort_by(.key) | map("| `\(.key)` | \(.view|esc) | \(.label|esc) | `\(.from)` → `\(.to)` | \(.maturity|esc) | \(.evidence|esc) |") | join("\n") ) + "\n\n"

+ "## Unsafe control actions\n\n*The core of the analysis. Each row: a control action made unsafe via one guideword, the hazard/loss it causes, and where in the code it lives.*\n\n"
+ "| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Evidence |\n|----|----|----|----|----|----|----|----|\n"
+ ( .ucas | sort_by(.key) | map("| `\(.key)` | \(.view|esc) | `\(.control_action)` | \(.guideword|esc) | \(.condition|esc) | \(.severity|esc) | \((.hazards // [])|join(", ")) | \(.evidence|esc) |") | join("\n") ) + "\n\n"

+ ( if (.unsafe_feedback // []) | length > 0
    then "## Unsafe feedback\n\n*Feedback and data channels whose absence, staleness, corruption, or spoofed origin drives a controller into a hazard. This is where data-integrity failures live.*\n\n"
       + "| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Evidence |\n|----|----|----|----|----|----|----|----|\n"
       + ( .unsafe_feedback | sort_by(.key) | map("| `\(.key)` | \(.view|esc) | `\(.from)` → `\(.to)`: \(.signal|esc) | \(.guideword|esc) | \(.condition|esc) | \(.severity|esc) | \((.hazards // [])|join(", ")) | \(.evidence|esc) |") | join("\n") ) + "\n\n"
    else "" end )

+ ( if (.non_ucas // []) | length > 0
    then "<details>\n<summary><b>Not UCAs</b>: \(.non_ucas|length) examined and rejected</summary>\n\n"
       + ( .non_ucas | sort_by("\(.item)|\(.reason)") | map("- **\(.item|esc)**: \(.reason|esc)") | join("\n") )
       + "\n</details>\n\n"
    else "" end )

+ "## Open questions\n\n"
+ ( (.open_questions // []) | sort | map("- \(esc)") | join("\n") ) + "\n"
JQ
LC_ALL=C jq -rf /tmp/stpa-render.jq /tmp/stpa.json > /tmp/STPA.candidate.md
# Compare against what origin/main actually has: this checkout's working tree
# may be stale (or mid-task on another branch), and BLOCK B lands from a fresh
# worktree based on origin/main anyway.
git fetch -q origin main 2>/dev/null || true
if git show "origin/main:$SYSTEM_DIR/STPA.md" > /tmp/STPA.prior.md 2>/dev/null; then
  if diff -q /tmp/STPA.prior.md /tmp/STPA.candidate.md >/dev/null; then
    echo "STPA_RESULT=nochange"
  else
    echo "STPA_RESULT=changed"
  fi
else
  rm -f /tmp/STPA.prior.md   # first version: no prior on origin/main
  echo "STPA_RESULT=changed"
fi
````

## BLOCK B — commit, PR, watch CI, merge on green (run verbatim; ONLY when STPA_RESULT=changed)

```bash
set -euo pipefail
SYSTEM_DIR="$(jq -r '.system_dir' /tmp/stpa.json)"
SLUG="$(printf '%s' "$SYSTEM_DIR" | tr '/' '-' | tr -cd 'a-zA-Z0-9-')"
BRANCH="bot/stpa-$SLUG"
REPO_ROOT="$(git rev-parse --show-toplevel)"
WT="/tmp/claude-worktrees/stpa-$SLUG"
git config --get user.email >/dev/null 2>&1 || git config user.email "stpa-bot@users.noreply.github.com"
git config --get user.name  >/dev/null 2>&1 || git config user.name  "stpa-bot"
# Never build the branch in this checkout: it may sit on main (which this repo
# forbids committing to) and auto-fetches every 60s. Land the rendered file
# from a disposable worktree based on origin/main instead.
git -C "$REPO_ROOT" worktree remove -f "$WT" 2>/dev/null || true
git -C "$REPO_ROOT" worktree prune
git -C "$REPO_ROOT" worktree add -B "$BRANCH" "$WT" origin/main
mkdir -p "$WT/$SYSTEM_DIR"
cp /tmp/STPA.candidate.md "$WT/$SYSTEM_DIR/STPA.md"
git -C "$WT" add "$SYSTEM_DIR/STPA.md"
# The conventional `docs(stpa): ...` title satisfies the commit-msg hook, so we
# do NOT use --no-verify (the hook is cheap and is the gate we want).
git -C "$WT" commit -m "$(cat /tmp/stpa-title.txt)" -m "$(cat /tmp/stpa-body.md)"
git -C "$WT" push -f -u origin "$BRANCH"
# Create a PR only when there isn't already an OPEN one for this branch. A
# closed/merged PR on the branch must NOT count as "exists", else the create is
# skipped and the later `gh pr merge` targets a dead PR and wedges.
PR_STATE="$(gh pr view "$BRANCH" --json state -q .state 2>/dev/null || echo NONE)"
if [ "$PR_STATE" != "OPEN" ]; then
  gh pr create --base main --head "$BRANCH" --title "$(cat /tmp/stpa-title.txt)" --body-file /tmp/stpa-body.md
fi
URL="$(gh pr view "$BRANCH" --json url -q .url)"
echo "PR: $URL"
# This repo is rebase-merge only (squash is disabled). Poll CI ourselves, then
# rebase-merge on green. Merge WITHOUT --delete-branch first (the branch is
# checked out in the worktree, which would make local deletion fail), then
# clean up the worktree and both branch refs ourselves.
if gh pr checks "$BRANCH" --watch --fail-fast; then
  gh pr merge "$BRANCH" --rebase
  git -C "$REPO_ROOT" worktree remove -f "$WT" 2>/dev/null || true
  git -C "$REPO_ROOT" branch -D "$BRANCH" 2>/dev/null || true
  git -C "$REPO_ROOT" push -q origin --delete "$BRANCH" 2>/dev/null || true
  echo "merged (CI green): $URL"
else
  echo "NOTE: CI not green, PR left open for review: $URL (worktree kept: $WT)"
fi
```
