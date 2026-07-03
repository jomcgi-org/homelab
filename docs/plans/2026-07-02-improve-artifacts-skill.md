# improve-artifacts Skill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an on-demand `/improve-artifacts` skill: a UI/UX quality feedback loop that screenshots published goosecracker artifacts, scores them against a fixed rubric, and opens evidence-backed PRs editing the two artifact recipe YAMLs.

**Architecture:** Sister skill to `/improve-recipes`, mirroring its exact shape: a `SKILL.md` driving the operator plus one in-pod helper script piped over stdin into the monolith backend container. Its own sampling frame (all artifacts published in the recipe-change window, not worst sessions) and its own judge (local playwright screenshots at desktop and mobile widths). Evals persist as `design-eval.json` next to each artifact's `index.html` in S3 so PRs can carry before/after tables across recipe generations.

**Tech Stack:** Markdown skill + Python helper (runs in-pod against SQLAlchemy/boto3 that already exist there), playwright CLI via npx locally.

**Design doc:** `docs/plans/2026-07-02-improve-artifacts-design.md` (read it first).

**Reference implementation to mirror:** `.claude/skills/improve-recipes/SKILL.md` and `.claude/skills/improve-recipes/scripts/improve_recipes_tool.py`. Read both before Task 1; the new skill deliberately copies their conventions (pod invocation, put-eval-as-argv, window default, evidence rule, guardrails).

**Testing note:** This repo has NO local test loop (see CLAUDE.md). The helper script only runs in-pod (it imports `app.db` and `artifact.s3`, which exist only in the monolith image), so verification here is `python3 -m py_compile` plus code review. Skill scripts are not Bazel targets (improve-recipes precedent), so CI only runs format/semgrep over them.

---

### Task 1: In-pod helper script

**Files:**

- Create: `.claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py`

**Step 1: Read the reference helper**

Read `.claude/skills/improve-recipes/scripts/improve_recipes_tool.py` in full. The new helper reuses its bucket-index and put-eval patterns.

**Step 2: Write the helper**

Write this exact content to `.claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py`:

```python
"""improve-artifacts in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>          NDJSON artifact records to stdout
  fetch-artifact <artifact_id>      base64 index.html to stdout
  put-eval <artifact_id> <b64>      base64-encoded design-eval JSON as argv[3]
                                    -> s3://artifacts/<id>/design-eval.json

put-eval takes its payload as a base64-encoded argv, not stdin: the script
itself is piped in over stdin (there is no second stdin channel to carry the
payload once that pipe is consumed). Callers must base64-encode the eval JSON
document and pass it as the third argument.

Postgres access is read-only (SELECT only). S3 writes are limited to
design-eval.json.
"""

import base64
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import get_engine
from artifact import s3

# Recipe/thread context for the artifact ids found in the bucket. Artifact ids
# are random capability tokens minted per thread (chat.goosecracker_sessions.
# artifact_id), NOT session ids, so recover the Discord thread via
# goosecracker_sessions and join agent_threads on it. DISTINCT ON keeps the
# most recent thread row when a thread ran multiple turns.
_CONTEXT_SQL = """
SELECT DISTINCT ON (gs.artifact_id)
       gs.artifact_id,
       gs.discord_thread AS session_id,
       at.recipe, at.tier, at.state,
       LEFT(at.task, 500) AS task,
       at.created_at
FROM chat.goosecracker_sessions gs
LEFT JOIN claude_agent.agent_threads at ON at.session_id = gs.discord_thread
WHERE gs.artifact_id = ANY(:ids)
ORDER BY gs.artifact_id, at.created_at DESC
"""

_EVAL_LEAF = "design-eval.json"


def _bucket_index():
    """One paginated listing -> {artifact_id: {leaf: last_modified}}."""
    client = s3._client()
    have = {}
    token = None
    while True:
        kwargs = {"Bucket": s3._bucket()}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            aid, _, leaf = key.partition("/")
            if leaf:
                have.setdefault(aid, {})[leaf] = obj["LastModified"]
        if not resp.get("IsTruncated"):
            return have
        token = resp.get("NextContinuationToken")


def gather(since):
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    have = _bucket_index()
    client = s3._client()
    ids = [
        aid
        for aid, leaves in have.items()
        if "index.html" in leaves and leaves["index.html"] >= since_dt
    ]
    context = {}
    if ids:
        with get_engine().connect() as conn:
            rows = conn.execute(text(_CONTEXT_SQL), {"ids": ids}).mappings()
            context = {r["artifact_id"]: dict(r) for r in rows}
    for aid in sorted(ids, key=lambda a: have[a]["index.html"], reverse=True):
        leaves = have[aid]
        ctx = context.get(aid, {})
        rec = {
            "artifact_id": aid,
            "session_id": ctx.get("session_id"),
            "published_at": str(leaves["index.html"]),
            "recipe": ctx.get("recipe"),
            "tier": ctx.get("tier"),
            "state": ctx.get("state"),
            "task": ctx.get("task"),
            "thread_created_at": str(ctx["created_at"]) if ctx.get("created_at") else None,
            "has_design_eval": _EVAL_LEAF in leaves,
            "design_eval_rubric_version": None,
        }
        if rec["has_design_eval"]:
            body = client.get_object(
                Bucket=s3._bucket(), Key=f"{aid}/{_EVAL_LEAF}"
            )["Body"].read()
            rec["design_eval_rubric_version"] = json.loads(body).get("rubric_version")
        print(json.dumps(rec, default=str))


def fetch_artifact(artifact_id):
    got = s3.get_artifact(artifact_id)
    if got is None:
        print(json.dumps({"error": "no index.html", "artifact_id": artifact_id}))
        sys.exit(2)
    html, _etag = got
    sys.stdout.write(base64.b64encode(html).decode())


_EVAL_REQUIRED = {
    "artifact_id",
    "session_id",
    "recipe",
    "rubric_version",
    "scores",
    "slop_tells",
    "defects",
    "viewports",
    "judged_at",
    "recipes_ref",
}


def put_eval(artifact_id, payload_b64):
    doc = json.loads(base64.b64decode(payload_b64))
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if doc["artifact_id"] != artifact_id:
        print(json.dumps({"error": "artifact_id mismatch"}))
        sys.exit(2)
    s3._client().put_object(
        Bucket=s3._bucket(),
        Key=f"{artifact_id}/{_EVAL_LEAF}",
        Body=json.dumps(doc, indent=1).encode(),
        ContentType="application/json",
    )
    print(json.dumps({"ok": True, "key": f"{artifact_id}/{_EVAL_LEAF}"}))


def main():
    cmd = sys.argv[1]
    if cmd == "gather":
        gather(sys.argv[sys.argv.index("--since") + 1])
    elif cmd == "fetch-artifact":
        fetch_artifact(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
```

**Step 3: Verify it compiles**

Run: `python3 -m py_compile .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py && echo OK`
Expected: `OK`

**Step 4: Sanity-check the SQL join column names against the schema**

Run: `grep -n "session_id\|recipe\|tier\|state\|task\|created_at" projects/monolith/claude_agent/*.py | grep -i "column\|mapped" | head -20`
Expected: columns `session_id`, `recipe`, `tier`, `state`, `task`, `created_at` all exist on the agent_threads model. Artifact ids are NOT session ids: they're random capability tokens minted per thread and stored on `chat.goosecracker_sessions.artifact_id`, whose PK `discord_thread` equals `agent_threads.session_id`. The join key for `_CONTEXT_SQL` is `goosecracker_sessions.artifact_id`, not `agent_threads.session_id` directly; recover the thread via `goosecracker_sessions` first, then join `agent_threads` on `discord_thread`. If any column name differs, fix `_CONTEXT_SQL` to match (improve-recipes' `_GATHER_SQL` is the authority for the agent_threads columns).

**Step 5: Commit**

```bash
git add .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py
git commit -m "feat(skills): improve-artifacts in-pod helper (gather/fetch-artifact/put-eval)"
```

---

### Task 2: SKILL.md

**Files:**

- Create: `.claude/skills/improve-artifacts/SKILL.md`

**Step 1: Read the reference SKILL.md**

Read `.claude/skills/improve-recipes/SKILL.md` in full: the new file mirrors its section order and voice.

**Step 2: Write the skill**

Write this exact content to `.claude/skills/improve-artifacts/SKILL.md`:

````markdown
---
name: improve-artifacts
description: >
  UI/UX quality feedback loop over published goosecracker artifacts: screenshot
  each artifact in the window, score it against a fixed design rubric, and open
  evidence-backed PRs editing the artifact recipe YAMLs. Use when asked to
  "improve the artifacts", "/improve-artifacts", "why do the artifacts look
  samey / like AI slop", or "design feedback loop". For mechanics failures
  (routing, turns, wall time) use improve-recipes instead.
---

# improve-artifacts

On-demand design-quality feedback loop for goosecracker artifact recipes.
Sister skill to improve-recipes, which selects on mechanics metrics (wall time,
owner turns) and is structurally blind to fast, no-follow-up sessions that
publish generic slop. This skill samples ALL artifacts published in the window,
judges them visually from screenshots, and edits only the two artifact recipe
YAMLs.

Design doc: `docs/plans/2026-07-02-improve-artifacts-design.md`.

## Goal metric

Rubric compliance, not vibes: median dimension scores and the slop-tell
histogram across a recipe generation, compared before/after in every PR. The
rubric deliberately derives from the DESIGN BAR in artifact-build.yaml, so
scores measure compliance with the bar we already ship.

## Invocation

Optional argument: a lookback window, or one artifact_id for a targeted "that
looks bad" analysis.

Find the monolith pod (the API lives in the `backend` CONTAINER of the
`monolith-*` pod; there is no pod named "backend"):

```bash
kubectl get pods -n monolith -o name | grep '^pod/monolith-' | grep -v pg | grep -v atlas | grep -v searxng | head -1
```
````

Run a subcommand by piping the helper script over stdin into the pod's venv
python (note the `-c backend` container selector):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  gather --since 2026-06-28T00:00:00Z \
  < .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py
```

`put-eval` takes the eval JSON base64-encoded as an argv argument (stdin
carries the script):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <artifact_id> <base64-encoded-eval-json> \
  < .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py
```

Default `--since` window: the merge date of the last commit touching
`projects/firecracker/goosecracker/guest/recipes/` on `origin/main`:

```bash
git log -1 --format=%cI origin/main -- projects/firecracker/goosecracker/guest/recipes/
```

The previous generation's window is between the prior two such commits; use it
to compute the before/after aggregates for the PR body.

## Worklist

Judge EVERY artifact in the window (this is the point: slop does not rank on
mechanics metrics, so there is no "worst" pre-filter). Cap at 15, newest
first, if a window is unusually busy, and say so in the report. Skip artifacts
whose `has_design_eval` is true at the current rubric_version.

## Screenshots

Per artifact, on the workstation:

1. `fetch-artifact <artifact_id>`, decode to `<scratchpad>/<artifact_id>/index.html`.
2. Serve and shoot at desktop and mobile widths:

```bash
(cd <scratchpad>/<artifact_id> && python3 -m http.server 8931 &) && sleep 1
npx playwright screenshot --viewport-size "1440,900" --full-page \
  http://localhost:8931/index.html <artifact_id>-desktop.png
npx playwright screenshot --viewport-size "390,844" --full-page \
  http://localhost:8931/index.html <artifact_id>-mobile.png
kill %1
```

One-time setup if chromium is missing: `npx playwright install chromium`.

Caveat: local rendering lacks the production sandbox CSP, so this is a design
judge, not a security or embed-behavior check. If a page fails to render for
reasons outside the recipe's control (CDN outage, https-blocked resource),
record it as env-failure and never diff for it.

## Rubric v1

Fixed vocabulary so runs are comparable over time. `rubric_version: 1`.
Changing a dimension means bumping the version in this file by PR.

Six dimensions, each scored 1-4 with a one-line rationale:

1. **typography**: distinctive pairing, clear hierarchy, no default stacks
2. **color**: cohesive committed palette, tinted neutrals, readable contrast
3. **layout-rhythm**: varied spacing, asymmetry where it helps, no card-in-card
4. **component-discipline**: one token system (:root variables, single radius),
   not ad-hoc styling per element
5. **interaction-affordance**: visible hover/active/focus on every control
6. **mobile-usability**: judged from the 390px shot; no overflow, tap targets
   usable, text wraps

Two flag lists:

- **slop_tells**: purple-blue gradients, gradient text on headings or numbers,
  cyan-on-dark, neon-on-dark, everything centered, cards nested in cards, pure
  #000 or #fff, default-stack fonts (Inter/Roboto/Arial/system), monospace as
  lazy "techie" shorthand
- **defects**: visible breakage (overflow, dead-looking or unstyled sections,
  missing icons, half-rendered components)

Score from BOTH screenshots plus the HTML itself (token discipline, type
stack, palette live in the code).

## design-eval.json contract

Written per artifact via `put-eval <artifact_id> <base64-payload>`, alongside
the artifact's index.html in `s3://artifacts/{artifact_id}/`. Keys:

- `artifact_id`
- `session_id` (the Discord thread id, from gather; nullable if no session row matched)
- `recipe` (from gather; nullable)
- `rubric_version`
- `scores`: dimension -> `{score, rationale}`
- `slop_tells`: list of strings from the fixed vocabulary
- `defects`: list of strings
- `viewports`: widths screenshotted, e.g. `[1440, 390]`
- `judged_at`: ISO timestamp, from `date -u`
- `recipes_ref`: git SHA of `origin/main` for the recipes dir at run time

## Judging

Judge each artifact yourself, or when the window is busy fan out per-artifact
subagents that each Read the two PNGs plus the HTML and return the eval JSON;
synthesis stays in the main loop. Judgment quality over wall time.

## Diagnose and ship

Recipe files live in `projects/firecracker/goosecracker/guest/recipes/`. The
editable surface is exactly two files: `artifact-build.yaml` and
`artifact-review.yaml`, prose AND static retry gates.

Evidence rule: every diff hunk must cite at least one artifact_id and what its
screenshot showed. An edit that cannot cite a specific artifact is dropped.

1. Aggregate the evals: recurring low dimensions or repeated tells map to the
   DESIGN BAR prose (artifact-build.yaml), the review checklist
   (artifact-review.yaml), or, when the defect is mechanically detectable, a
   new static check in BOTH files' `retry` gates (the slate-705 path: a
   recurring visual defect promoted to a mechanical gate is this loop's
   highest-leverage output).
2. Gate edits must be tested locally before the PR: run the node check script
   against a fixture HTML that should pass and one that should fail, and show
   both results.
3. Create a worktree and branch, following the repo's normal workflow.
4. Edit the two recipe YAMLs only, nothing else. Sanity-check every edited
   file parses:

   ```bash
   python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>
   ```

5. Open a PR. Body template, in order:
   - Before/after rubric table: this generation vs the previous one (median
     score per dimension, slop-tell histogram), computed from the
     design-eval.json files in each window.
   - Per-edit evidence rows: artifact_id, scores, tells/defects, what the
     screenshot showed, and which diff line addresses it.
   - "Out of scope, observed" section for anything the evidence points at
     outside the two recipe files.

## Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to creating design-eval.json; never touch index.html,
  sessions.db, eval.json, or any other object.
- Fewer than about 5 artifacts in the window: report-only, no PR ("not enough
  evidence yet").
- env-failure (render failure not attributable to the recipe) is reported,
  never diffed.
- Edits limited to artifact-build.yaml and artifact-review.yaml.

## After merge

Recipes reach production through the existing pipeline: the merged PR's CI
rebuilds the goosecracker guest apko image and bumps the substrate chart. The
next `/improve-artifacts` run's before/after table is the verdict on whether
the change worked.

````

**Step 3: Verify the frontmatter parses**

Run: `python3 -c "import yaml; print(yaml.safe_load(open('.claude/skills/improve-artifacts/SKILL.md').read().split('---')[1])['name'])"`
Expected: `improve-artifacts`

**Step 4: Commit**

```bash
git add .claude/skills/improve-artifacts/SKILL.md
git commit -m "feat(skills): improve-artifacts UI/UX feedback loop skill"
````

Note: the format pre-commit hook may regenerate the repo docs manifests when a
new markdown file lands; if the commit fails with "files were modified", `git
add -A` and re-commit.

---

### Task 3: Push, PR, CI, merge

**Step 1: Push and open the PR**

```bash
git push -u origin feat/improve-artifacts-skill
gh pr create --title "feat(skills): improve-artifacts UI/UX quality feedback loop" --body "$(cat <<'EOF'
Adds /improve-artifacts: an on-demand design-quality feedback loop over
published goosecracker artifacts, sister to /improve-recipes.

- improve-recipes selects worst sessions on mechanics metrics (wall time,
  owner turns) and structurally never sees fast, no-follow-up sessions that
  publish generic slop. This skill samples ALL artifacts in the recipe-change
  window instead.
- Judge input is local playwright screenshots (1440px + 390px) plus the HTML.
- Scores persist as design-eval.json next to each artifact's index.html
  (rubric v1, six dimensions + slop-tell/defect flags), so PRs carry
  before/after tables across recipe generations.
- PRs from the loop edit only artifact-build.yaml and artifact-review.yaml
  (prose and static retry gates), every hunk citing an artifact id.

Design doc: docs/plans/2026-07-02-improve-artifacts-design.md (included).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 2: Watch CI**

Run: `gh pr checks <number> --watch`
Expected: Format check and Test and push both green (this PR adds no Bazel
targets, so the test wall is the format/semgrep surface).

If red: fetch the log via `mcp__buildbuddy__get_invocation` (commitSha
selector) then `get_target`/`get_log`, quote the failing assertion verbatim,
fix, push.

**Step 3: Merge (rebase) and clean up**

```bash
gh pr merge <number> --auto --rebase
```

Poll `gh pr view <number> --json state,mergeStateStatus` until merged, then:

```bash
git -C ~/repos/homelab worktree remove /tmp/claude-worktrees/improve-artifacts
```

**Step 4: First real run**

After merge, invoke `/improve-artifacts` once against the current window to
shake out the helper in-pod (schema mismatches in `_CONTEXT_SQL` would surface
here, not in CI). Expect report-only if fewer than ~5 artifacts exist in the
window.

```

```
