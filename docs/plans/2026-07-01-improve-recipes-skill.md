# /improve-recipes Skill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship an on-demand `/improve-recipes` Claude Code skill that classifies goosecracker sessions from prod data and opens evidence-backed PRs editing the recipe YAMLs.

**Architecture:** Two files. A single in-pod Python helper script (mechanism: gather metrics from Postgres, fetch sessions.db, write eval.json to S3) executed via `kubectl exec` piping the script to the monolith backend pod's venv python, and a SKILL.md (judgment: ranking, classification against a fixed taxonomy, recipe diagnosis, PR authoring). Design doc: `docs/plans/2026-07-01-improve-recipes-feedback-loop-design.md` (read it first).

**Tech Stack:** Python (runs only in-pod, reusing `app.db` and `artifact.s3`), Claude Code skill markdown, kubectl, gh.

**Not TDD:** the helper runs only inside the prod pod against live Postgres/S3; there is no local or CI test seam for it and the repo has no local test loop. Verification is `python3 -m py_compile` plus a live read-only smoke run (Task 3). The script must therefore stay small and boring: SQL SELECTs, S3 get/put, JSON on stdout.

---

### Task 1: In-pod helper script

**Files:**

- Create: `.claude/skills/improve-recipes/scripts/improve_recipes_tool.py`

This script is NEVER built by Bazel and never imported by monolith code. It lives under `.claude/skills/` (outside any Bazel package) and is piped over stdin into the backend pod, where `app.db`, `artifact.s3`, and `goosecracker.sessions` are importable. Pre-commit semgrep still scans it: keep all imports at module top level (the `no-inline-stdlib-import` rule) and do not construct a boto3 client directly (reuse `artifact.s3._client()`, which already carries the endpoint-scheme guard and its nosemgrep).

**Step 1: Write the script**

```python
"""improve-recipes in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>       NDJSON session records to stdout
  fetch-session <session_id>     base64 sessions.db blob to stdout
  put-eval <session_id>          eval JSON on stdin -> s3://artifacts/<sid>/eval.json

Postgres access is read-only (SELECT only). S3 writes are limited to eval.json.
"""

import base64
import json
import sys

from sqlalchemy import text

from app.db import get_engine
from artifact import s3
from goosecracker import sessions

_GATHER_SQL = """
SELECT at.thread_id, at.session_id, at.recipe, at.tier, at.state,
       LEFT(at.task, 500) AS task,
       LEFT(at.result, 400) AS result_head,
       at.result_error,
       at.created_at, at.completed_at,
       EXTRACT(EPOCH FROM (at.completed_at - at.created_at)) AS wall_seconds,
       gs.transcript
FROM claude_agent.agent_threads at
LEFT JOIN chat.goosecracker_sessions gs ON gs.discord_thread = at.session_id
WHERE at.created_at >= :since
ORDER BY at.created_at
"""


def _owner_turns(transcript):
    # _join_transcript in chat/goosecracker.py separates owner turns with a
    # blank line; a heuristic (turns containing blank lines overcount) but
    # consistent across sessions.
    if not transcript:
        return None
    return len([b for b in transcript.split("\n\n") if b.strip()])


def _bucket_index():
    """One paginated listing of the artifacts bucket -> per-session key sets."""
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
            sid, _, leaf = key.partition("/")
            if leaf:
                have.setdefault(sid, set()).add(leaf)
        if not resp.get("IsTruncated"):
            return have
        token = resp.get("NextContinuationToken")


def gather(since):
    have = _bucket_index()
    client = s3._client()
    with get_engine().connect() as conn:
        rows = conn.execute(text(_GATHER_SQL), {"since": since}).mappings()
        for row in rows:
            rec = dict(row)
            sid = rec["session_id"]
            rec["owner_turns"] = _owner_turns(rec.pop("transcript"))
            rec["created_at"] = str(rec["created_at"])
            rec["completed_at"] = str(rec["completed_at"]) if rec["completed_at"] else None
            rec["wall_seconds"] = float(rec["wall_seconds"]) if rec["wall_seconds"] is not None else None
            leaves = have.get(sid, set())
            rec["has_sessions_db"] = "sessions.db" in leaves
            rec["has_eval"] = "eval.json" in leaves
            rec["eval_taxonomy_version"] = None
            if rec["has_eval"]:
                body = client.get_object(Bucket=s3._bucket(), Key=f"{sid}/eval.json")["Body"].read()
                rec["eval_taxonomy_version"] = json.loads(body).get("taxonomy_version")
            print(json.dumps(rec, default=str))


def fetch_session(session_id):
    blob = sessions.load(session_id)
    if blob is None:
        print(json.dumps({"error": "no sessions.db", "session_id": session_id}))
        sys.exit(2)
    sys.stdout.write(base64.b64encode(blob).decode())


_EVAL_REQUIRED = {
    "session_id", "recipe", "taxonomy_version", "failure_modes",
    "metrics", "classified_at", "recipes_ref",
}


def put_eval(session_id):
    doc = json.load(sys.stdin)
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if doc["session_id"] != session_id:
        print(json.dumps({"error": "session_id mismatch"}))
        sys.exit(2)
    s3._client().put_object(
        Bucket=s3._bucket(),
        Key=f"{session_id}/eval.json",
        Body=json.dumps(doc, indent=1).encode(),
        ContentType="application/json",
    )
    print(json.dumps({"ok": True, "key": f"{session_id}/eval.json"}))


def main():
    cmd = sys.argv[1]
    if cmd == "gather":
        gather(sys.argv[sys.argv.index("--since") + 1])
    elif cmd == "fetch-session":
        fetch_session(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
```

Implementation notes for the engineer:

- Verify the module seams before trusting this code: `goosecracker.sessions.load(session_id)` (projects/monolith/goosecracker/sessions.py:30), `artifact.s3._client()` / `s3._bucket()` (projects/monolith/artifact/s3.py:39-66), `app.db.get_engine`. Adjust if signatures moved.
- `put-eval` cannot read both stdin-script and stdin-payload. The SKILL.md invocation for put-eval therefore ships the script via `kubectl cp` style exec instead: write the eval JSON to a temp file in the pod first, or simpler, pass the payload base64-encoded as `sys.argv[3]` and decode it. Pick the argv approach: replace `json.load(sys.stdin)` with `json.loads(base64.b64decode(sys.argv[3]))` and document the calling convention in SKILL.md. Payloads are small (a few KB), well under argv limits.

**Step 2: Syntax check**

Run: `python3 -m py_compile .claude/skills/improve-recipes/scripts/improve_recipes_tool.py`
Expected: exit 0, no output. (Import errors will only surface in-pod; that is Task 3.)

**Step 3: Commit**

```bash
git add .claude/skills/improve-recipes/scripts/improve_recipes_tool.py
git commit -m "feat(skills): in-pod gather/fetch/put-eval helper for improve-recipes"
```

---

### Task 2: SKILL.md

**Files:**

- Create: `.claude/skills/improve-recipes/SKILL.md`

Follow the format of `.claude/skills/scheduler/SKILL.md` (frontmatter with `name` and a trigger-rich `description`, then operational sections). Content requirements, in order:

1. **Frontmatter.** `name: improve-recipes`. Description triggers: "improve the goose recipes", "/improve-recipes", "why did that agent session go badly", "classify agent sessions", "recipe feedback loop".
2. **Goal metrics** (verbatim from the design): time to outcome (wall time) and owner turns per interaction.
3. **The invocation recipe.** Exact commands:
   - Find the backend pod: `kubectl get pods -n monolith -o name | grep backend | head -1` (verify the namespace/label against the live cluster in Task 3 and hard-code what works).
   - Run a subcommand by piping the script:

     ```bash
     kubectl exec -i -n monolith <pod> -- env \
       PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
       /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
       gather --since 2026-06-28T00:00:00Z \
       < .claude/skills/improve-recipes/scripts/improve_recipes_tool.py
     ```

   - Window default: `--since` = merge date of the last commit touching `projects/firecracker/goosecracker/guest/recipes/` on origin/main (`git log -1 --format=%cI origin/main -- projects/firecracker/goosecracker/guest/recipes/`). Previous generation window = between the prior two such commits.

4. **Rank rules.** Score completed sessions on wall_seconds and owner_turns; any FAILED state or non-empty result_error is automatically in the worst set. Deep-read the worst 5 (or the single session the user named). Skip sessions where `has_eval` is true with the current taxonomy_version.
5. **Deep-read.** `fetch-session <sid>` -> base64 -> decode to a scratchpad file -> `sqlite3 <file> .schema` first, then extract the message/tool-call sequence (goose session schema is not documented here on purpose; inspect it).
6. **Taxonomy v1** (copy the seven modes and definitions verbatim from the design doc section "Classification taxonomy"). State: `taxonomy_version: 1`. Adding a mode means bumping the version in this file by PR.
7. **eval.json contract.** Keys: session_id, recipe, taxonomy_version, failure_modes (list of {mode, confidence, rationale}), metrics {wall_seconds, owner_turns, tool_calls, retries}, classified_at (ISO, from `date -u`), recipes_ref (git SHA of origin/main). Written via `put-eval <sid> <base64-payload>`. Low-confidence flag when sessions.db was missing and classification came from result_head alone.
8. **Diagnose and ship.** Recipe files live in `projects/firecracker/goosecracker/guest/recipes/`. Evidence rule: every diff hunk must cite at least one session_id. Worktree + branch + PR per repo workflow; recipe edits only. Sanity-check edited YAML parses (`python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`). PR body template: before/after aggregates table (this generation vs previous, from eval.json files), then per-edit evidence rows (session, metrics, mode, excerpt, diff line), then "Out of scope, observed".
9. **Guardrails** (verbatim from design): Postgres SELECT-only; S3 writes eval.json only; fewer than 5 completed sessions -> report, no PR; env-failure is never diffed.
10. **After merge.** Remind that recipes reach prod via guest image rebuild + substrate chart bump on the merged PR's CI; the NEXT /improve-recipes run measures whether this change worked.

**Step 2: Commit**

```bash
git add .claude/skills/improve-recipes/SKILL.md
git commit -m "feat(skills): /improve-recipes goosecracker feedback-loop skill"
```

The format hook will regenerate the two doc manifests for the new markdown file; include them in the commit when it does.

---

### Task 3: Live smoke test (read-only first)

No files. Verifies the helper against prod from the worktree, before the PR.

**Step 1:** Resolve the backend pod name; fix the SKILL.md command if the namespace or selector was wrong.

**Step 2:** Run `gather --since <7 days ago>`. Expected: NDJSON lines with wall_seconds, owner_turns, has_sessions_db populated for recent goosecracker sessions (there are recent /agent and /artifact threads, so this must not be empty). Debug import/SQL errors here; they will not show up anywhere else.

**Step 3:** Pick one completed session from the output, run `fetch-session`, decode, and confirm `sqlite3 file .tables` shows goose session tables.

**Step 4:** Classify that one session by hand (per the SKILL.md flow) and `put-eval` it. Re-run `gather` and confirm `has_eval: true` with `eval_taxonomy_version: 1` for that session. This is the intended production write, not a test artifact; leave it.

**Step 5:** Fold any command corrections back into SKILL.md and commit:

```bash
git add .claude/skills/improve-recipes/SKILL.md
git commit -m "fix(skills): correct improve-recipes pod exec invocation from live smoke test"
```

(Skip the commit if nothing changed.)

---

### Task 4: PR

**Step 1:** `format` in the worktree; commit any fallout.

**Step 2:** Push and open the PR:

```bash
git push -u origin feat/improve-recipes-skill
gh pr create --title "feat(skills): /improve-recipes goosecracker feedback loop" \
  --body "<summary + link to design doc + smoke-test evidence from Task 3>"
```

PR body must end with the standard Claude Code attribution line.

**Step 3:** `gh pr merge --auto --rebase`, then poll `gh pr view --json state,mergeStateStatus` until merged (CI here is format/semgrep/manifest checks only; no service code changed).
