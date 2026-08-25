---
name: stpa
description: Generate or refresh the STPA safety model for one system and lens (logic/security/governance), written colocated as <system>/stpa/<lens>.json with a merged render as <system>/STPA.md. Takes `system` (directory) and optional `lens` (logic|security|governance, default logic). The analysis is deterministic, findings extracted as JSON, rendered by an embedded jq renderer, so runs produce small reviewable diffs. Use when updating a system's STPA analysis, auditing unsafe control actions, or refreshing on schedule.
---

Generate/update the STPA safety model for ONE system and ONE lens. Findings are
written to committed JSON fragments under `<system>/stpa/`, and a merged
`<system>/STPA.md` is rendered deterministically from all present fragments.

The `system` argument is the directory to analyze (e.g. `projects/monolith`).
The `lens` argument is `logic`, `security`, or `governance` (default `logic` if
not given; ask if ambiguous). The analysis scope is that subtree and the control
structure it grounds in.

You are a systems-safety analyst applying STPA (System-Theoretic Process Analysis)
per lens. EXTRACT findings as JSON (judgment), RENDER the document by running
BLOCK A verbatim (mechanism), and only if it changed, open/refresh a PR via BLOCK B.
Determinism depends on rendering ONLY via the renderer. Do NOT hand-write markdown.
(`**/STPA.md` and `**/stpa/*.json` are in `.prettierignore` so the renderer and
jq pretty-print are sole authorities.)

## Lens framing

All lenses share a control structure (structure.json, owned by the logic lens).
Per-lens analyses answer different safety questions:

- **logic**: mission-failure STPA. "What unsafe control action or feedback defect
  drives the system into a loss?" Honest-but-imperfect system, mission violation,
  guidewords `providing|not-providing|wrong-timing|wrong-duration` for actions and
  `missing|stale|corrupted|unauthorized-source` for feedback.
- **security**: deliberate attack. "Who can forge/withhold/replay a control action,
  and what does it get an adversary?" Same control structure, attacks on purpose.
  Status (`enforced-prod|enforced-dev|shipped-off|designed|none`) REQUIRED on every
  UCA and unsafe-feedback row (what safeguard exists now). Issues referenced via
  `issue` field (label: `security-finding`).
- **governance**: data-processing safety. "Does data cross visibility boundaries,
  stay retained too long, process without basis, lose provenance?" Feedback and
  data channels matter more than control arrows. Same rejection bar.

## Lens ownership and independence

The `logic` lens OWNS structure.json (nodes, control_actions, feedback); it may add,
update, or remove them (grounded in code), then re-validates every other committed
fragment. If a fragment references a removed action, that row is dropped with a
note in the PR body.

`security` and `governance` lenses are READ-ONLY consumers of structure.json. If
analysis needs an action/node the structure lacks, record it in the fragment's
`open_questions` (form: "structure gap: ..."), do NOT invent an ID.

Each lens reads ONLY: code + structure.json + its own prior fragment. Not the
other lens fragments. Independence prevents anchoring and context bloat.

## Two views: logical and physical

Every finding carries a `view`:

- **logical**: functional control structure. Request/command flow: routers, domain
  functions, schedulers, transactions, authorization boundaries in the code path.
  Losses about governed correctness and provenance.
- **physical**: deployment realization. Pods/binaries, namespaces, NetworkPolicy,
  database schemas, ingress, secret delivery. Losses about exposure and escalation.

A system may model only one view; omit ones you cannot ground; do not invent.

## Steps

1. **Determine system and lens.** If not given, ask (defaults: system =
   `projects/monolith`, lens = `logic`).
2. **Check for prior fragments.** If `<system>/stpa/` exists, read them (prior
   analysis, semantic keys to reuse). If not but `<system>/STPA.md` exists (legacy
   single-lens), extract structure.json + logic.json from its tables (preserve keys
   verbatim; minimizes diff).
3. **Ground in code.** List the system subtree; read READMEs, architecture docs,
   roadmap. Read code units that issue or receive commands. Get commit:
   `git rev-parse --short HEAD`.
4. **Analyze per lens.** STPA steps 1-3: hazards (map to loss keys), control
   structure (nodes, control_actions, feedback, tagged `built` or `designed`),
   UCAs (four guidewords, keep ONLY genuinely unsafe; bounded nuisances go to
   `non_ucas`), unsafe feedback (missing, stale, corrupted, unauthorized; this is
   where data-integrity failures live).
5. **Pick scratch dir.** `STPA_TMP="$(mktemp -d /tmp/stpa-<system>-<lens>.XXXXXX)"`,
   export it. Prevents collisions between concurrent lens runs.
6. **For logic lens only**: write structure.json to `$STPA_TMP/structure.json`.
   For all lenses: write lens fragment to `$STPA_TMP/<lens>.json`. Schemas below.
7. **Render** by running BLOCK A VERBATIM. It assembles all present fragments,
   validates cross-references, renders `$STPA_TMP/STPA.candidate.md`, compares
   against origin/main (this checkout may be stale), and prints `STPA_RESULT=`.
8. **If `nochange`:** report no change, stop, do not open PR.
9. **If `changed`:** prepare PR text. Use `diff -u` to see what moved. Write
   Conventional title to `$STPA_TMP/stpa-title.txt`, form `docs(stpa): <lens>:
   <what changed>` (<=72 chars, e.g. `docs(stpa): security: add login
   forge-token UCA`). Write concise body to `$STPA_TMP/stpa-body.md` (2-6
   bullets naming changed findings by semantic key, plus scope/maturity shifts).
10. **Open/refresh PR** by running BLOCK B VERBATIM. Commits both the fragment
    and merged STPA.md, force-pushes, opens or updates PR, watches CI, rebase-merges
    on green. Report PR URL and result.

## Migration: existing single-analysis STPA.md

Existing `<system>/STPA.md` files (no `stpa/` dir) are reverse-extracted: parse the
tables, emit structure.json + logic.json preserving every semantic key verbatim.
The logic lens's first run on such a system extracts once, then owns structure
normally. Minimizes diff churn.

## Remediation work goes to GitHub Issues

File unmitigated UCAs / unsafe-feedback findings as issues (repo's source of truth
for outstanding work), titled `<system>: <finding> (STPA <key>)`, labeled `bug`
(broken safeguard) or `enhancement` (missing safeguard), plus `critical` for
severe losses. Security lens findings add label `security-finding`. Reference
the issue from the finding's evidence, not a STPA.md checklist.

## Drift minimization (this is an UPDATE, not a rewrite)

Rendered docs are committed and reviewed as diffs. Minimize churn:

- REUSE each prior finding's semantic key VERBATIM when the code it cites still
  exists and still means the same thing.
- KEEP prior `condition`/`statement`/`label`/`scope` wording UNCHANGED unless code
  changed. Do not reword for style.
- Update an `evidence` `path:line` only if the referenced code actually moved.
- But RE-VERIFY, never parrot: open each prior finding's cited code and confirm it
  still holds. Logic changed, update it. Code deleted, drop it. New unsafe action,
  add it with a new key. Net: unchanged code => identical finding; changed code =>
  localized change; added/removed code => added/removed finding.

## Semantic keys

Derive each key from WHAT IT IS, never position. Lowercase, no spaces, no dots
(mermaid ids): nodes: `acl`, `public-api`, `scheduler`; losses: `L.unauthorized-access`,
`L.secret-exposure`; control_actions: `<node>.<op>`: `acl.check`, `scheduler.dispatch`;
hazards: condition slug: `stale-policy`, `cross-schema-grant`; ucas:
`<control_action>.<guideword>`: `scheduler.dispatch.wrong-timing`; unsafe_feedback:
`<channel>.<guideword>`: `policy-fetch.stale`, `queue-notify.missing`.
`from`/`to`/`control_action`/`hazards`/`losses` reference other objects BY KEY.

## Grounding rules (hard)

- Every node, control_action, UCA: `evidence` `path:line` or `path` plus <=8-word
  verbatim quote for docs/comments. No evidence, omit it.
- NEVER mark a designed-only element `built`.
- No invention. Uncertainty goes to `open_questions`.
- Prioritize. Keep `condition`/`statement` to ONE line, no `|`, no newlines.

## JSON schemas

**structure.json** (logic lens writes, others read):
```
{
  "system": "monolith", "system_dir": "projects/monolith", "commit": "abc1234",
  "nodes": [ {"key":"acl","label":"Acl check","view":"logical","layer":"enforcement","maturity":"built"}, ... ],
  "control_actions": [ {"key":"acl.check","label":"...","view":"logical","from":"public-api","to":"acl","maturity":"built","evidence":"path:line"}, ... ],
  "feedback": [ {"view":"logical","from":"postgres","to":"scheduler","signal":"NOTIFY wakeup hint"}, ... ]
}
```

**<lens>.json** (logic, security, or governance):
```
{
  "lens": "security", "system_dir": "projects/monolith", "commit": "abc1234",
  "scope": {"summary":"1-line headline","built":"built things","designed":"designed things","note":"OPTIONAL caveat"},
  "losses": [ {"key":"L.unauthorized-access","title":"..."}, ... ],
  "hazards": [ {"key":"stale-policy","view":"logical","statement":"...","losses":["L.unauthorized-access"],"maturity":"built","status":"none","issue":"#5277"}, ... ],
  "ucas": [ {"key":"scheduler.dispatch.wrong-timing","view":"logical","control_action":"scheduler.dispatch","guideword":"wrong-timing","condition":"...","severity":"high","hazards":["stale-policy"],"evidence":"path:line","status":"enforced-prod","issue":"#1234"}, ... ],
  "unsafe_feedback": [ {"key":"policy-fetch.stale","view":"logical","from":"acl","to":"public-api","signal":"policy rows","guideword":"stale","condition":"...","severity":"high","hazards":["stale-policy"],"evidence":"path:line","status":"enforced-dev"}, ... ],
  "non_ucas": [ {"item":"missed NOTIFY","reason":"bounded by 5s fallback (path:line)"}, ... ],
  "open_questions": [ "string", ... ]
}
```

Every fragment carries `system_dir` so a lens-only run can locate the committed
structure. `status` is REQUIRED on security-lens UCA/unsafe-feedback rows and
optional elsewhere; any `status` must be one of
`enforced-prod|enforced-dev|shipped-off|designed|none`, and a security-lens row
or hazard with `status: none` MUST carry a tracking `issue` (GitHub Issues are
the source of truth for unmitigated findings). The validator enforces all
three. Hazards MAY carry `status`/`issue` too: a tracked unsafe STATE with no
single attacking control action (a missing egress policy, an absent jailer)
lives on the hazard row rather than being forced onto an unrelated action.
A UCA `key` must be exactly `<control_action>.<guideword>` (validated). `view`:
`logical|physical`. `layer`: free-form subgraph (e.g. `enforcement`, `ingress`).
`severity`: `high|medium|low`. UCA guidewords:
`providing|not-providing|wrong-timing|wrong-duration`. Unsafe_feedback guidewords:
`missing|stale|corrupted|unauthorized-source`. `unsafe_feedback` array optional.
The rendered commit-stamp line lists every lens, so two lens PRs in flight touch
the same line and the second gets ejected from the merge queue on rebase: land
concurrent lens runs serially.

## BLOCK A: render + detect change (run verbatim; never hand-write .md)

````bash
set -euo pipefail
SYSTEM_DIR="$(jq -r '.system_dir' $STPA_TMP/structure.json 2>/dev/null || echo '')"
LENS="$(jq -r '.lens // empty' $STPA_TMP/*.json 2>/dev/null | head -1)"
[ -z "$LENS" ] && LENS="logic"
printf '%s' "$LENS" > "$STPA_TMP/lens.txt"
[ -z "$SYSTEM_DIR" -o "$SYSTEM_DIR" = "null" ] && SYSTEM_DIR="$(jq -r '.system_dir // empty' $STPA_TMP/*.json 2>/dev/null | head -1)"
test -n "$SYSTEM_DIR" -a "$SYSTEM_DIR" != "null"

git fetch -q origin main 2>/dev/null || true
STRUCTURE_TMP="$STPA_TMP/structure-assembled.json"
if [ -f "$STPA_TMP/structure.json" ]; then
  cp "$STPA_TMP/structure.json" "$STRUCTURE_TMP"
else
  git show "origin/main:$SYSTEM_DIR/stpa/structure.json" > "$STRUCTURE_TMP" 2>/dev/null || {
    echo "STPA_ERROR: no structure.json in this run or origin/main"
    exit 1
  }
fi

FRAG_PATHS=""
for LENS_NAME in logic security governance; do
  LENS_FILE="$STPA_TMP/$LENS_NAME.json"
  FRAG_PATH="$SYSTEM_DIR/stpa/$LENS_NAME.json"
  FRAG_TMP="$STPA_TMP/$LENS_NAME-src.json"
  if [ -f "$LENS_FILE" ]; then
    cp "$LENS_FILE" "$FRAG_TMP"
    FRAG_PATHS="$FRAG_PATHS $FRAG_TMP"
  elif git show "origin/main:$FRAG_PATH" > "$FRAG_TMP" 2>/dev/null; then
    FRAG_PATHS="$FRAG_PATHS $FRAG_TMP"
  fi
done

MERGED="$STPA_TMP/merged.json"
jq -n --slurpfile s "$STRUCTURE_TMP" '{structure: $s[0], lenses: [inputs]}' $FRAG_PATHS > "$MERGED"

cat > $STPA_TMP/validate.jq << 'VJQEOF'
. as $doc
| ($doc.structure.control_actions // [] | map(.key)) as $cakeys
| ($doc.structure.nodes // [] | map(.key)) as $nodekeys
| [ $doc.lenses[]
    | .lens as $L
    | (.hazards // [] | map(.key)) as $hkeys
    | (.losses // [] | map(.key)) as $lkeys
    | (
        (.ucas // [] | .[]
          | select(.control_action as $ca | ($cakeys | index($ca)) == null)
          | "\($L): UCA \(.key) references unknown control_action \(.control_action)"),
        (.unsafe_feedback // [] | .[]
          | select((.from as $f | ($nodekeys | index($f)) == null)
                or (.to as $t | ($nodekeys | index($t)) == null))
          | "\($L): unsafe_feedback \(.key) references unknown node \(.from) or \(.to)"),
        ((.ucas // []) + (.unsafe_feedback // []) | .[] | .key as $k
          | (.hazards // [])[]
          | select(. as $h | ($hkeys | index($h)) == null)
          | "\($L): \($k) references unknown hazard \(.)"),
        (.hazards // [] | .[] | .key as $k
          | (.losses // [])[]
          | select(. as $l | ($lkeys | index($l)) == null)
          | "\($L): hazard \($k) references unknown loss \(.)"),
        (select($L == "security")
          | (.ucas // []) + (.unsafe_feedback // []) | .[]
          | select(.status == null)
          | "security: \(.key) missing required status"),
        (select($L == "security")
          | (.ucas // []) + (.unsafe_feedback // []) + (.hazards // []) | .[]
          | select(.status == "none" and .issue == null)
          | "security: \(.key) is unmitigated (status none) with no tracking issue"),
        ((.ucas // []) + (.unsafe_feedback // []) + (.hazards // []) | .[]
          | select(.status != null and
              (.status as $s | (["enforced-prod","enforced-dev","shipped-off","designed","none"] | index($s)) == null))
          | "\($L): \(.key) status \(.status) not in enforced-prod|enforced-dev|shipped-off|designed|none"),
        (.ucas // [] | .[]
          | select(.key != "\(.control_action).\(.guideword)")
          | "\($L): UCA key \(.key) must be \(.control_action).\(.guideword)")
      )
  ]
| if length == 0 then "VALIDATION_OK" else "VALIDATION_ERROR: " + join("; ") end
VJQEOF

VALIDATION="$(jq -r -f $STPA_TMP/validate.jq "$MERGED")"
if [ "$VALIDATION" != "VALIDATION_OK" ]; then
  echo "$VALIDATION"
  exit 1
fi

cat > $STPA_TMP/stpa-render.jq << 'JQEOF'
def esc: (. // "") | gsub("\\|"; "\\|") | gsub("\n"; " ");
def mlabel: (. // "") | gsub("\n"; " ") | gsub("\""; "'") | gsub("<"; "(") | gsub(">"; ")");
def mid: gsub("[^a-zA-Z0-9_]"; "_");

def diagram($v):
  ([.structure.nodes[]? | select(.view == $v)]) as $ns
  | ([.structure.control_actions[]? | select(.view == $v)]) as $cas
  | ([(.structure.feedback // [])[]? | select((.view // $v) == $v)]) as $fbs
  | (if ($ns | length) == 0 then "_No \($v) control structure modeled._\n"
      else ("```mermaid\nflowchart TD\n"
        + (($ns | sort_by(.layer // "zzz", .key) | group_by(.layer // "zzz")
           | map((.[0].layer // "other") as $L |
               "  subgraph \($L|mid)[\"\($L)\"]\n"
               + (map("    \(.key)[\"\(.label|mlabel)\(if .maturity=="designed" then " (designed)" else "" end)\"]") | join("\n"))
               + "\n  end\n") | join("")))
        + (($cas | sort_by(.key) | map("  \(.from) -- \"\(.key)\" --> \(.to)") | join("\n")))
        + "\n"
        + (($fbs | sort_by("\(.from)|\(.to)|\(.signal)") | map("  \(.from) -. \"\(.signal|mlabel)\" .-> \(.to)") | join("\n")))
        + "\n```\n")
      end);

"# STPA Control Analysis: \(.structure.system)\n\n"
+ (if (.lenses | length) > 0 then "_\(([.lenses[] | "\(.lens) @ \(.commit)"] | join(" · ")))_\n\n" else "" end)
+ "_Auto-generated STPA safety model: unsafe states this system can reach and control actions that get it there. Single or multiple lenses: logic (mission failure), security (deliberate attack), governance (data safety)._\n\n"
+ "<details>\n<summary><b>How to read this</b>: STPA primer and diagram legend</summary>\n\n"
+ "**STPA** (System-Theoretic Process Analysis, Leveson) treats the system as controllers issuing control actions to controlled processes, with feedback flowing back. Instead of \"what component fails,\" it asks \"what control action, given or withheld at the wrong time, drives the system into an unsafe state?\" Unsafe means a violation of this system's reason to exist. Multiple lenses (logic, security, governance) analyze the same control structure for different safety questions: mission failure, deliberate attack, data-processing violations. A finding appearing in multiple lenses is expected and correct.\n\n"
+ "Read top-down: Losses are outcomes we must prevent; Hazards are states leading to loss; diagrams show control structure (solid = control action, dashed = feedback); Unsafe Control Actions table is the core; Unsafe Feedback covers data channels (absent, stale, corrupted, spoofed). Every claim cites path:line; unbuilt elements are marked. Semantic stable IDs mean regenerating changes only findings that changed.\n</details>\n\n"
+ "## Control structure\n\n### Logical view\n\n" + diagram("logical") + "\n### Physical view\n\n" + diagram("physical") + "\n"
+ "## Control actions\n\n| ID | View | Control action | Controller → Process | Maturity | Evidence |\n|----|----|----|----|----|----|\n"
+ (.structure.control_actions | sort_by(.key) | map("| `\(.key)` | \(.view|esc) | \(.label|esc) | `\(.from)` → `\(.to)` | \(.maturity|esc) | \(.evidence|esc) |") | join("\n"))
+ "\n\n"
+ (.lenses | sort_by(.lens) | map(
    "## Lens: \(.lens)\n\n"
    + "**Scope.** \(.scope.summary|esc)\n\n"
    + "<details>\n<summary>Maturity detail</summary>\n\n"
    + "- **Built:** \(.scope.built|esc)\n"
    + "- **Designed-only:** \(.scope.designed|esc)\n"
    + (if (.scope.note // null) then "- **Note:** \(.scope.note|esc)\n" else "" end)
    + "</details>\n\n"
    + "### Losses\n\n| ID | Loss |\n|----|------|\n"
    + ((.losses // []) | sort_by(.key) | map("| `\(.key)` | \(.title|esc) |") | join("\n"))
    + "\n\n"
    + (((.hazards // []) | any(.status != null or .issue != null)) as $hz |
       "### Hazards\n\n| ID | View | Hazard (unsafe state) | → Losses | Maturity"
       + (if $hz then " | Status | Issue" else "" end)
       + " |\n"
       + "|" + (([range(if $hz then 7 else 5 end)] | map("----") | join("|"))) + "|\n"
       + ((.hazards // []) | sort_by(.key) | map(
           "| `\(.key)` | \(.view|esc) | \(.statement|esc) | \((.losses // [])|join(", ")) | \(.maturity|esc)"
           + (if $hz then " | \(.status // "" | esc) | \(.issue // "" | esc)" else "" end)
           + " |") | join("\n"))
       + "\n\n")
    + (if ((.ucas // []) | length > 0) then
        (((.ucas // []) | any(.status != null)) as $has_status |
         "### Unsafe control actions\n\n| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards"
         + (if $has_status then " | Status | Issue" else "" end)
         + " | Evidence |\n"
         + "|" + (([range(if $has_status then 10 else 8 end)] | map("----") | join("|"))) + "|\n"
         + ((.ucas // []) | sort_by(.key) | map(
             "| `\(.key)` | \(.view|esc) | `\(.control_action)` | \(.guideword|esc) | \(.condition|esc) | \(.severity|esc) | \((.hazards // [])|join(", "))"
             + (if $has_status then " | \(.status // "" | esc) | \(.issue // "" | esc)" else "" end)
             + " | \(.evidence|esc) |") | join("\n"))
         + "\n\n")
      else "" end)
    + (if ((.unsafe_feedback // []) | length > 0) then
        (((.unsafe_feedback // []) | any(.status != null)) as $has_status |
         "### Unsafe feedback\n\n*Data channels (absent, stale, corrupted, spoofed) whose failure drives a controller into a hazard.*\n\n"
         + "| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards"
         + (if $has_status then " | Status | Issue" else "" end)
         + " | Evidence |\n"
         + "|" + (([range(if $has_status then 10 else 8 end)] | map("----") | join("|"))) + "|\n"
         + ((.unsafe_feedback // []) | sort_by(.key) | map(
             "| `\(.key)` | \(.view|esc) | `\(.from)` → `\(.to)`: \(.signal|esc) | \(.guideword|esc) | \(.condition|esc) | \(.severity|esc) | \((.hazards // [])|join(", "))"
             + (if $has_status then " | \(.status // "" | esc) | \(.issue // "" | esc)" else "" end)
             + " | \(.evidence|esc) |") | join("\n"))
         + "\n\n")
      else "" end)
    + (if ((.non_ucas // []) | length > 0) then
        "<details>\n<summary><b>Not UCAs</b>: \(.non_ucas|length) examined and rejected</summary>\n\n"
        + ((.non_ucas // []) | sort_by("\(.item)|\(.reason)") | map("- **\(.item|esc)**: \(.reason|esc)") | join("\n"))
        + "\n</details>\n\n"
      else "" end)
    + (if ((.open_questions // []) | length > 0) then
        "### Open questions\n\n" + ((.open_questions // []) | sort | map("- \(esc)") | join("\n")) + "\n\n"
      else "" end)
  ) | join(""))
| sub("\n+$"; "")
JQEOF

jq -rf $STPA_TMP/stpa-render.jq "$MERGED" > $STPA_TMP/STPA.candidate.md

if git show "origin/main:$SYSTEM_DIR/STPA.md" > $STPA_TMP/STPA.prior.md 2>/dev/null; then
  if diff -q $STPA_TMP/STPA.prior.md $STPA_TMP/STPA.candidate.md >/dev/null 2>&1; then
    echo "STPA_RESULT=nochange"
  else
    echo "STPA_RESULT=changed"
  fi
else
  rm -f $STPA_TMP/STPA.prior.md
  echo "STPA_RESULT=changed"
fi
````

## BLOCK B: commit, PR, watch CI, merge on green (run verbatim; ONLY when STPA_RESULT=changed)

```bash
set -euo pipefail
SYSTEM_DIR="$(jq -r '.system_dir' $STPA_TMP/structure.json 2>/dev/null || jq -r '.system_dir // empty' $STPA_TMP/*.json 2>/dev/null | head -1)"
LENS="$(cat "$STPA_TMP/lens.txt" 2>/dev/null || echo 'logic')"
SLUG="$(printf '%s' "$SYSTEM_DIR" | tr '/' '-' | tr -cd 'a-zA-Z0-9-')"
BRANCH="bot/stpa-$SLUG-$LENS"
REPO_ROOT="$(git rev-parse --show-toplevel)"
WT="/tmp/claude-worktrees/stpa-$SLUG-$LENS"

git config --get user.email >/dev/null 2>&1 || git config user.email "stpa-bot@users.noreply.github.com"
git config --get user.name  >/dev/null 2>&1 || git config user.name  "stpa-bot"

git -C "$REPO_ROOT" worktree remove -f "$WT" 2>/dev/null || true
git -C "$REPO_ROOT" worktree prune
git -C "$REPO_ROOT" worktree add -B "$BRANCH" "$WT" origin/main

mkdir -p "$WT/$SYSTEM_DIR/stpa"
[ -f "$STPA_TMP/structure.json" ] && jq '.' "$STPA_TMP/structure.json" > "$WT/$SYSTEM_DIR/stpa/structure.json"
[ -f "$STPA_TMP/$LENS.json" ] && jq '.' "$STPA_TMP/$LENS.json" > "$WT/$SYSTEM_DIR/stpa/$LENS.json"
cp $STPA_TMP/STPA.candidate.md "$WT/$SYSTEM_DIR/STPA.md"

git -C "$WT" add "$SYSTEM_DIR/stpa/" "$SYSTEM_DIR/STPA.md"
git -C "$WT" commit -m "$(cat $STPA_TMP/stpa-title.txt)" -m "$(cat $STPA_TMP/stpa-body.md)"
git -C "$WT" push -f -u origin "$BRANCH"

PR_STATE="$(gh pr view "$BRANCH" --json state -q .state 2>/dev/null || echo NONE)"
if [ "$PR_STATE" != "OPEN" ]; then
  gh pr create --base main --head "$BRANCH" --title "$(cat $STPA_TMP/stpa-title.txt)" --body-file $STPA_TMP/stpa-body.md
fi

URL="$(gh pr view "$BRANCH" --json url -q .url)"
echo "PR: $URL"

gh pr merge "$BRANCH" --auto --rebase || true
for _ in $(seq 1 60); do
  STATE="$(gh pr view "$BRANCH" --json state -q .state)"
  [ "$STATE" = "MERGED" ] && break
  [ "$STATE" = "CLOSED" ] && { echo "NOTE: PR closed unmerged: $URL"; exit 1; }
  sleep 60
done

if [ "$STATE" = "MERGED" ]; then
  git -C "$REPO_ROOT" worktree remove -f "$WT" 2>/dev/null || true
  git -C "$REPO_ROOT" branch -D "$BRANCH" 2>/dev/null || true
  echo "merged: $URL"
else
  echo "NOTE: not merged after 60 min, PR left queued: $URL (worktree kept: $WT)"
fi
rm -rf "$STPA_TMP"
```
