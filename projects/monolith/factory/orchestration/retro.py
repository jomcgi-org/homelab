"""Evidence digest for the factory retro drain job.

The ``factory-retro-daily`` routine job runs on the drain lane (Sol) and files
GitHub issues for recurring factory inefficiencies. The guest has no database
access, and the public task pages only cover the board's live and most recent
tasks, so the evidence is gathered here, server side, and appended to the
job's prompt. The model judges evidence rather than searching for it.

``load_retro_data`` is a thin Postgres reader that returns plain dicts and
``build_retro_digest`` is the pure shaping, unit tested with dicts. The window
is 72 hours although the job runs daily: most patterns need several days of
runs to rise above noise, and the dedupe section stops a daily run refiling
what an earlier run already reported.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

RETRO_DIGEST = "factory-retro"
WINDOW_HOURS = 72
# The body marker every retro issue and comment carries, so later runs can
# find what earlier runs filed.
RETRO_MARKER = "<!-- factory-retro -->"
PUBLIC_BASE = "https://jomcgi.dev/slop/factory/activity"
REPO = "jomcgi-org/homelab"
DIGEST_MAX_CHARS = 48000
EXAMPLES = 4
TOP = 10
ISSUE_LIST_LIMIT = 150

# Outcome reasons that mean the infrastructure lost the attempt, not that the
# model failed at the work.
INFRA_DEATH = re.compile(
    r"^(guest_cessation_confirmed|lost_before_guest|not_invoked|"
    r"supervised_cessation|interrupted_continuation_retired|"
    r"provider_error_before_work)"
)
_REPAIR_HINT = re.compile(
    r"guest_cessation|lost_before_guest|not_invoked|supervised_cessation|"
    r"cessation|interrupted_continuation"
)
# Tool calls that install or fetch something the guest image lacks.
SETUP_PATTERNS = (
    (
        "pip install (pytest, requirements)",
        r"pip3? install|-m pip install|uv pip install",
    ),
    ("npm or pnpm install", r"\bnpm (?:i|install)\b|\bpnpm install\b|corepack"),
    ("helm download", r"get\.helm\.sh"),
    ("go toolchain download", r"go\.dev/dl|golang\.org/dl"),
    ("go install", r"\bgo install\b"),
    ("JDK download", r"temurin|adoptium"),
    ("BuildBuddy RPC by curl", r"BuildBuddyService"),
)
_SETUP = tuple((label, re.compile(pattern)) for label, pattern in SETUP_PATTERNS)
_CORRECTION_NODE = re.compile(r"^correct_[0-9]+$")
_PR_URL = re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)")
_NOISE = re.compile(r"[0-9a-f]{7,}|t-[0-9a-f-]{8,}|\d+")


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _one_line(text, limit: int = 160) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _iso(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(value or "")[:16].replace("T", " ")


def _outcome(run: dict) -> dict:
    return _json(run.get("outcome_json"), {})


def _reason(run: dict) -> str:
    return str(_outcome(run).get("reason") or "")


def _artifact_value(run: dict) -> dict:
    artifact = _outcome(run).get("artifact")
    if not isinstance(artifact, dict):
        return {}
    value = artifact.get("value")
    return value if isinstance(value, dict) else {}


def _death_kind(run: dict) -> str | None:
    if run.get("status") not in ("failed", "uncertain", "escalated"):
        return None
    match = INFRA_DEATH.match(_reason(run))
    return match.group(1) if match else None


class Citer:
    """Cite a node attempt by public link when its page exists."""

    def __init__(self, published: dict[str, int | None], issues: dict[str, int]):
        self.published = published
        self.issues = issues
        # The public task page shows the issue's current task only, so a task
        # links only while one of its own sessions is published.
        self.published_tasks = {
            key.split(":")[1] for key in published if key.count(":") == 3
        }

    def __call__(self, task_id: str, node_key: str, attempt) -> str:
        issue = self.issues.get(task_id)
        key = f"factory:{task_id}:{node_key}:{attempt}"
        if issue is not None and self.published.get(key) == issue:
            return f"{PUBLIC_BASE}/{issue}/{node_key}/{attempt}"
        label = f"#{issue}" if issue is not None else "no issue"
        return f"{label} {node_key}/{attempt} (task {task_id[:10]})"

    def task(self, task_id: str) -> str:
        issue = self.issues.get(task_id)
        if issue is None:
            return f"task {task_id[:10]}"
        if task_id in self.published_tasks:
            return f"{PUBLIC_BASE}/{issue}"
        return f"#{issue} (task {task_id[:10]})"


def _activities(turn: dict) -> list[dict]:
    usage = _json(turn.get("usage_json"), {})
    items = usage.get("activities")
    return (
        [item for item in items if isinstance(item, dict)]
        if isinstance(items, list)
        else []
    )


def _session_parts(local_session_id: str) -> tuple[str, str, str] | None:
    parts = str(local_session_id or "").split(":")
    if len(parts) != 4 or parts[0] != "factory":
        return None
    return parts[1], parts[2], parts[3]


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "- none in the window"
    return f"### {title}\n{body}\n"


def _refusals(data: dict, cite: Citer, run_cost: dict) -> list[str]:
    by_code: dict[str, list[dict]] = defaultdict(list)
    for row in data.get("refusals", []):
        detail = _json(row.get("detail_json"), {})
        by_code[str(detail.get("refusal_code") or "unknown")].append(
            {**detail, "task_id": row.get("task_id")}
        )
    lines = []
    ordered = sorted(by_code.items(), key=lambda item: -len(item[1]))
    for code, rows in ordered[:TOP]:
        tasks = {row["task_id"] for row in rows if row.get("task_id")}
        cost = 0.0
        examples = []
        for row in rows:
            match = re.match(
                r"factory-decision:(.+):(\d+)$", str(row.get("cause") or "")
            )
            if not match or not row.get("task_id"):
                continue
            node, attempt = match.group(1), int(match.group(2))
            cost += run_cost.get((row["task_id"], node, attempt), 0.0)
            if len(examples) < EXAMPLES:
                examples.append(
                    f"{cite(row['task_id'], node, attempt)}: "
                    f"{_one_line(row.get('reason'), 140)}"
                )
        lines.append(
            f"- {code}: {len(rows)} refusals on {len(tasks)} tasks, "
            f"${cost:.2f} list in the refused planner runs"
        )
        lines.extend(f"  - {example}" for example in examples)
    return lines


def _deaths(data: dict, cite: Citer, run_cost: dict) -> list[str]:
    runs = data.get("runs", [])
    kinds: Counter = Counter()
    evicted = 0
    for run in runs:
        kind = _death_kind(run)
        if kind:
            kinds[kind] += 1
            evicted += '"state": "evicted"' in json.dumps(
                _outcome(run).get("cessation") or {}
            )
    total = sum(kinds.values())
    lines = [
        f"- {total} of {len(runs)} node runs ended in an infrastructure death: "
        + ", ".join(f"{kind} {count}" for kind, count in kinds.most_common())
        + f" (guest state evicted: {evicted})"
    ]
    repairs = [
        run
        for run in runs
        if str(run.get("node_key") or "").startswith("conductor")
        and run.get("status") == "succeeded"
        and _REPAIR_HINT.search(str(_artifact_value(run).get("reason") or "")[:400])
    ]
    cost = sum(
        run_cost.get((r["task_id"], r["node_key"], r["attempt"]), 0.0) for r in repairs
    )
    per_task = Counter(run["task_id"] for run in repairs)
    lines.append(
        f"- {len(repairs)} successful planner runs cite an infra death in their "
        f"reason (repair re-plans), ${cost:.2f} list"
    )
    for task_id, count in per_task.most_common(EXAMPLES):
        first = next(r for r in repairs if r["task_id"] == task_id)
        lines.append(
            f"  - {cite.task(task_id)}: {count} repair runs, e.g. "
            f"{cite(task_id, first['node_key'], first['attempt'])}"
        )
    by_hour = Counter(
        _iso(run.get("finished_at") or run.get("created_at"))[:13]
        for run in runs
        if _death_kind(run)
    )
    if by_hour:
        busiest = ", ".join(
            f"{hour}h {count}" for hour, count in by_hour.most_common(4)
        )
        lines.append(f"- busiest death hours (UTC): {busiest}")
    return lines


_MISSING_THING = re.compile(
    r"`([^`]{2,80})` (?:is )?(?:missing|not found)"
    r"|No such file or directory: '([^']{2,80})'"
    r"|([\w./-]{2,60}): (?:command )?not found",
    re.IGNORECASE,
)


def _turn_heads(data: dict) -> dict[tuple, str]:
    """The last recorded result head of each node attempt's session."""
    heads: dict[tuple, str] = {}
    for turn in data.get("turns", []):
        parts = _session_parts(turn.get("local_session_id"))
        if parts is None:
            continue
        attempt = int(parts[2]) if parts[2].isdigit() else parts[2]
        heads[(parts[0], parts[1], attempt)] = str(turn.get("result_head") or "")
    return heads


def _failures(data: dict, cite: Citer) -> list[str]:
    heads = _turn_heads(data)
    groups: dict[str, list[dict]] = defaultdict(list)
    for run in data.get("runs", []):
        if run.get("status") not in ("failed", "escalated", "uncertain"):
            continue
        if _death_kind(run):
            continue
        reason = _reason(run)
        reason = re.sub(r"^list_priced_cost: [^;]*; ", "", reason)
        key = _NOISE.sub("N", _one_line(reason, 90)) or "no reason"
        groups[key].append(run)
    lines = []
    for key, rows in sorted(groups.items(), key=lambda item: -len(item[1]))[:TOP]:
        models = Counter(str(r.get("model")) for r in rows)
        tasks = {r["task_id"] for r in rows}
        examples = ", ".join(
            cite(r["task_id"], r["node_key"], r["attempt"]) for r in rows[:3]
        )
        lines.append(
            f"- {len(rows)} runs on {len(tasks)} tasks ({dict(models)}): {key}"
            f"\n  - e.g. {examples}"
        )
        # What the model itself said: one reason can hide unrelated causes,
        # such as a broken guest tool behind a missing artifact.
        said = Counter(
            _NOISE.sub(
                "N",
                _one_line(
                    heads.get((r["task_id"], r["node_key"], r["attempt"]), ""), 110
                ),
            )
            for r in rows
        )
        for text, count in said.most_common(3):
            if text and (count > 1 or len(rows) <= 3):
                lines.append(f'  - {count} of these said: "{text}"')
    return lines


def _harness(data: dict, cite: Citer) -> list[str]:
    """Identical failing turn results across tasks: a broken harness or tool."""
    groups: dict[str, list[tuple]] = defaultdict(list)
    for turn in data.get("turns", []):
        text = str(turn.get("result_head") or "")
        if not re.search(
            r"(missing|not found|failed|cannot|unable|no such file|denied|blocked)",
            text[:300],
            re.I,
        ):
            continue
        parts = _session_parts(turn.get("local_session_id"))
        if parts is None:
            continue
        key = _NOISE.sub("N", _one_line(text, 100))
        groups[key].append((*parts, _f(turn.get("list_cost_usd"))))
    missing: dict[str, list[tuple]] = defaultdict(list)
    for turn in data.get("turns", []):
        match = _MISSING_THING.search(str(turn.get("result_head") or ""))
        parts = _session_parts(turn.get("local_session_id"))
        if match and parts is not None:
            thing = next(group for group in match.groups() if group)
            missing[thing].append((*parts, _f(turn.get("list_cost_usd"))))
    lines = []
    for thing, rows in sorted(missing.items(), key=lambda item: -len(item[1])):
        tasks = {row[0] for row in rows}
        if len(rows) < 3 or len(tasks) < 2:
            continue
        examples = ", ".join(cite(row[0], row[1], row[2]) for row in rows[:3])
        lines.append(
            f"- missing in the guest: `{thing}` named by {len(rows)} turns on "
            f"{len(tasks)} tasks, ${sum(r[3] for r in rows):.2f} list\n  - e.g. {examples}"
        )
    for key, rows in sorted(groups.items(), key=lambda item: -len(item[1])):
        tasks = {row[0] for row in rows}
        if len(rows) < 3 or len(tasks) < 2:
            continue
        cost = sum(row[3] for row in rows)
        examples = ", ".join(cite(row[0], row[1], row[2]) for row in rows[:3])
        lines.append(
            f"- {len(rows)} turns on {len(tasks)} tasks, ${cost:.2f} list: "
            f'"{key}"\n  - e.g. {examples}'
        )
        if len(lines) >= 6:
            break
    return lines


def _tools(data: dict, cite: Citer) -> list[str]:
    sessions: dict[str, set] = defaultdict(set)
    calls: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    repeated = []
    denials = []
    tool_names: Counter = Counter()
    all_sessions = set()
    for turn in data.get("turns", []):
        parts = _session_parts(turn.get("local_session_id"))
        if parts is None:
            continue
        key = turn.get("local_session_id")
        all_sessions.add(key)
        commands = Counter()
        for item in _activities(turn):
            command = str(item.get("command") or "")
            if item.get("type") == "tool_use":
                tool_names[str(item.get("name"))] += 1
            if command:
                commands[command[:200]] += 1
            for label, pattern in _SETUP:
                if command and pattern.search(command):
                    calls[label] += 1
                    if key not in sessions[label]:
                        sessions[label].add(key)
                        if len(examples[label]) < EXAMPLES:
                            examples[label].append(cite(*parts))
        for command, count in commands.items():
            if count >= 3:
                repeated.append((count, cite(*parts), _one_line(command, 100)))
        for denial in _json(turn.get("permission_denials"), []):
            if isinstance(denial, dict):
                command = (denial.get("tool_input") or {}).get("command", "")
                denials.append(
                    f"{cite(*parts)}: {denial.get('tool_name')} "
                    f"{_one_line(command, 100)}"
                )
    lines = [f"- factory sessions with turns in the window: {len(all_sessions)}"]
    for label, _pattern in _SETUP:
        if sessions[label]:
            lines.append(
                f"- {label}: {len(sessions[label])} sessions, {calls[label]} calls; "
                f"e.g. {', '.join(examples[label])}"
            )
    if repeated:
        lines.append(
            f"- identical command run 3+ times in one turn: {len(repeated)} cases"
        )
        for count, where, command in sorted(repeated, reverse=True)[:EXAMPLES]:
            lines.append(f"  - {count}x {where}: {command}")
    lines.append(
        f"- permission denials: {len(denials)}"
        + "".join(f"\n  - {denial}" for denial in denials[:EXAMPLES])
    )
    if tool_names:
        lines.append(
            "- non-shell tool calls: "
            + ", ".join(f"{name} {count}" for name, count in tool_names.most_common(8))
        )
    lines.append(
        "- note: the shim records each tool call's type and command only, no exit "
        "status, so failed calls are visible only through repetition or results"
    )
    return lines


def _reviews(data: dict, cite: Citer) -> list[str]:
    runs_by_id = {run.get("id"): run for run in data.get("runs", [])}
    verdicts = Counter()
    infra_blocked = 0
    examples = []
    for row in data.get("verdicts", []):
        run = runs_by_id.get(row.get("review_run_id"))
        if row.get("verdict") == "blocked" and run is not None and _death_kind(run):
            infra_blocked += 1
            continue
        verdicts[str(row.get("verdict"))] += 1
        if (
            row.get("verdict") != "approve"
            and run is not None
            and len(examples) < EXAMPLES
        ):
            examples.append(
                f"{cite(run['task_id'], run['node_key'], run['attempt'])}: "
                f"{row.get('verdict')}: {_one_line(row.get('summary'), 140)}"
            )
    lines = [
        "- first-pass review verdicts: "
        + (", ".join(f"{k} {v}" for k, v in verdicts.most_common()) or "none")
        + f"; plus {infra_blocked} recorded as blocked that were infra deaths"
    ]
    lines.extend(f"  - {example}" for example in examples)
    rounds = Counter(
        run["task_id"]
        for run in data.get("runs", [])
        if _CORRECTION_NODE.fullmatch(str(run.get("node_key") or ""))
    )
    if rounds:
        lines.append(
            "- correction rounds per task: "
            + ", ".join(f"{cite.task(t)} {n}" for t, n in rounds.most_common(6))
        )
    return lines


def _costs(data: dict, cite: Citer, task_cost: dict) -> list[str]:
    kinds: dict[str, list] = defaultdict(lambda: [0, 0.0])
    per_task: dict[str, Counter] = defaultdict(Counter)
    for run in data.get("runs", []):
        node = str(run.get("node_key") or "")
        kind = node.split("_")[0] + ("_funding" if "funding" in node else "")
        key = f"{kind}:{run.get('model')}"
        kinds[key][0] += 1
        kinds[key][1] += _f(run.get("list_cost_usd"))
        per_task[run["task_id"]][kind] += 1
    total = sum(_f(turn.get("list_cost_usd")) for turn in data.get("turns", []))
    lines = [f"- factory list spend on turns in the window: ${total:.2f}"]
    lines.append(
        "- by node kind and model (runs, list $): "
        + ", ".join(
            f"{k} {v[0]} ${v[1]:.2f}"
            for k, v in sorted(kinds.items(), key=lambda item: -item[1][1])[:10]
        )
    )
    top = sorted(per_task, key=lambda t: -task_cost.get(t, 0.0))[:8]
    for task_id in top:
        lines.append(
            f"- {cite.task(task_id)}: ${task_cost.get(task_id, 0.0):.2f} list, "
            f"runs {dict(per_task[task_id])}"
        )
    sizes = data.get("pr_sizes", {})
    outliers = []
    for task_id, pr in data.get("task_prs", {}).items():
        lines_changed = sizes.get(pr)
        if lines_changed:
            cost = task_cost.get(task_id, 0.0)
            outliers.append((cost / lines_changed, task_id, pr, cost, lines_changed))
    if outliers:
        lines.append("- cost per landed line (merged PRs), worst first:")
        for per_line, task_id, pr, cost, changed in sorted(outliers, reverse=True)[:5]:
            lines.append(
                f"  - {cite.task(task_id)} PR #{pr}: ${cost:.2f} for {changed} lines "
                f"(${per_line:.3f}/line)"
            )
    return lines


def _escalations(data: dict, cite: Citer) -> list[str]:
    states = Counter(str(row.get("state")) for row in data.get("receipts", []))
    lines = [
        "- receipts touched in the window by state: "
        + ", ".join(f"{k} {v}" for k, v in states.most_common())
    ]
    shown = 0
    for row in data.get("receipts", []):
        escalation = _json(row.get("escalation_json"), {})
        if not escalation or shown >= 8:
            continue
        if row.get("state") not in ("escalated", "uncertain", "admitted", "landing"):
            continue
        shown += 1
        task_id = row.get("task_id") or ""
        lines.append(
            f"- {cite.task(task_id) if task_id else '#' + str(row.get('issue_number'))} "
            f"[{row.get('state')}] {escalation.get('kind')}: "
            f"{_one_line(escalation.get('reason') or escalation.get('question'), 180)}"
        )
    return lines


def _issues(data: dict) -> list[str]:
    github = data.get("github") or {}
    if github.get("error"):
        return [
            f"- GitHub issue list unavailable ({github['error']}). Run "
            "`gh issue list` and `gh search issues` yourself before filing."
        ]
    lines = [
        "Issues and comments earlier retro runs filed (marker in body or comments):"
    ]
    retro = github.get("retro") or []
    if not retro:
        lines.append("- none yet")
    lines.extend(f"- #{i['number']} [{i['state']}] {i['title']}" for i in retro[:40])
    lines.append(
        "Open issues titled factory:, embervm: or agent sessions: (dedupe against these):"
    )
    lines.extend(
        f"- #{i['number']} {i['title']}"
        for i in (github.get("open") or [])[:ISSUE_LIST_LIMIT]
    )
    closed = github.get("recently_closed") or []
    if closed:
        lines.append("Closed in the last 14 days:")
        lines.extend(f"- #{i['number']} {i['title']}" for i in closed[:60])
    return lines


def build_retro_digest(data: dict, now: datetime | None = None) -> str:
    """Render the bounded evidence digest from plain loaded rows."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=data.get("window_hours", WINDOW_HOURS))
    issues = {
        row["task_id"]: row["issue_number"]
        for row in data.get("receipts", [])
        if row.get("task_id") and row.get("issue_number") is not None
    }
    cite = Citer(data.get("published", {}), issues)
    run_cost: dict = defaultdict(float)
    task_cost: dict = defaultdict(float)
    for turn in data.get("turns", []):
        parts = _session_parts(turn.get("local_session_id"))
        if parts is None:
            continue
        cost = _f(turn.get("list_cost_usd"))
        run_cost[
            (parts[0], parts[1], int(parts[2]) if parts[2].isdigit() else parts[2])
        ] += cost
    for run in data.get("runs", []):
        run["list_cost_usd"] = run_cost.get(
            (run["task_id"], run["node_key"], run["attempt"]), 0.0
        )
    task_cost.update(data.get("task_costs", {}))
    header = (
        "## Factory retro evidence digest\n"
        f"Window: {WINDOW_HOURS}h, {_iso(since)} to {_iso(now)} UTC. Costs are list "
        "price in USD (list_cost_usd). A cite is a public page link where the page "
        "exists, else `#<issue> <node>/<attempt> (task <id>)`; quote cites exactly. "
        f"Mark every issue body and comment you write with `{RETRO_MARKER}`.\n"
    )
    sections = [
        header,
        _section(
            "1. Planner decision refusals (conductor_rejected)",
            _refusals(data, cite, run_cost),
        ),
        _section(
            "2. Infrastructure deaths and planner repair runs",
            _deaths(data, cite, run_cost),
        ),
        _section(
            "3. Identical failing turn results across tasks (harness or tool breakage)",
            _harness(data, cite),
        ),
        _section("4. Other node failure reasons", _failures(data, cite)),
        _section("5. Tool calls: setup, repetition, denials", _tools(data, cite)),
        _section("6. Review verdicts and correction rounds", _reviews(data, cite)),
        _section("7. Cost", _costs(data, cite, task_cost)),
        _section("8. Escalations and stuck receipts", _escalations(data, cite)),
        _section("9. Existing issues (dedupe)", _issues(data)),
    ]
    out = ""
    for section in sections:
        if len(out) + len(section) > DIGEST_MAX_CHARS:
            out += (
                section[: max(0, DIGEST_MAX_CHARS - len(out) - 40)]
                + "\n[digest truncated]\n"
            )
            break
        out += section + "\n"
    return out


# --- Postgres loader --------------------------------------------------------

_RUNS = """
SELECT n.id, n.task_id, n.node_key, n.attempt, n.status, n.model, n.cost_usd,
       n.outcome_json, n.created_at, n.finished_at
FROM swarm.swarm_node_run n
WHERE n.created_at >= :since
ORDER BY n.id
"""
_RECEIPTS = """
SELECT id, issue_number, state, task_id, task_class, title, escalation_json, updated_at
FROM swarm.factory_receipt
WHERE task_id IN (SELECT DISTINCT task_id FROM swarm.swarm_node_run WHERE created_at >= :since)
   OR updated_at >= :since
"""
_REFUSALS = """
SELECT task_id, detail_json, created_at
FROM swarm.factory_audit
WHERE action = 'conductor_rejected' AND created_at >= :since
"""
_DELIVERIES = """
SELECT task_id, detail_json
FROM swarm.factory_audit
WHERE action IN ('delivery_ready', 'merged', 'finish_task') AND created_at >= :since
ORDER BY id
"""
_VERDICTS = """
SELECT task_id, review_run_id, verdict, sample_kind, left(summary, 400) AS summary
FROM swarm.factory_review_verdict
WHERE reviewed_at >= :since
"""
_TURNS = """
SELECT s.local_session_id, t.seq, t.list_cost_usd, t.terminal_reason, t.stop_reason,
       t.permission_denials, t.usage_json, left(t.result_text, 300) AS result_head
FROM agent_sessions.agent_turns t
JOIN agent_sessions.agent_sessions s ON s.id = t.session_id
WHERE t.created_at >= :since AND s.local_session_id LIKE 'factory:%'
"""
_TASK_COSTS = """
SELECT split_part(s.local_session_id, ':', 2) AS task_id,
       COALESCE(SUM(t.list_cost_usd), 0) AS cost
FROM agent_sessions.agent_sessions s
JOIN agent_sessions.agent_turns t ON t.session_id = s.id
WHERE s.local_session_id LIKE 'factory:%'
  AND split_part(s.local_session_id, ':', 2) IN (
      SELECT DISTINCT task_id FROM swarm.swarm_node_run WHERE created_at >= :since)
GROUP BY 1
"""
_PUBLISHED = "SELECT session_key, issue_number FROM public_api.factory_session_snapshot"
_PR_SIZES = """
SELECT number, additions + deletions AS changed
FROM observability.merged_prs WHERE number = ANY(:numbers)
"""


def _rows(session, sql: str, params: dict) -> list[dict]:
    from sqlmodel import text

    return [dict(row._mapping) for row in session.execute(text(sql), params).all()]


def _github_issues(now: datetime) -> dict:
    """Open area issues, recent closures and earlier retro filings."""
    import httpx

    token = os.environ.get("GITHUB_API_TOKEN")
    if not token:
        return {"error": "no GITHUB_API_TOKEN"}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    areas = ("factory:", "embervm:", "agent sessions:", "agents:")

    def search(client, query: str, limit: int) -> list[dict]:
        found = []
        page = 1
        while len(found) < limit:
            response = client.get(
                "https://api.github.com/search/issues",
                params={"q": query, "per_page": 100, "page": page, "sort": "updated"},
            )
            response.raise_for_status()
            items = response.json().get("items") or []
            found.extend(
                {
                    "number": item["number"],
                    "title": item.get("title") or "",
                    "state": item.get("state"),
                }
                for item in items
            )
            if len(items) < 100:
                break
            page += 1
        return found[:limit]

    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            base = f"repo:{REPO} is:issue"
            retro = search(client, f'{base} "factory-retro" in:body,comments', 60)
            open_items = [
                item
                for item in search(client, f"{base} is:open", 400)
                if item["title"].lower().startswith(areas)
            ]
            cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")
            closed = [
                item
                for item in search(client, f"{base} is:closed closed:>={cutoff}", 200)
                if item["title"].lower().startswith(areas)
            ]
    except Exception as exc:  # noqa: BLE001 - the digest degrades, the run continues
        logger.warning("factory retro GitHub issue read failed", exc_info=True)
        return {"error": type(exc).__name__}
    return {"retro": retro, "open": open_items, "recently_closed": closed}


def load_retro_data(session, now: datetime | None = None) -> dict:
    """Read the window's factory evidence as plain dicts (Postgres only)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)
    params = {"since": since}
    runs = _rows(session, _RUNS, params)
    receipts = _rows(session, _RECEIPTS, params)
    turns = _rows(session, _TURNS, params)
    task_prs: dict[str, int] = {}
    for row in _rows(session, _DELIVERIES, params):
        match = _PR_URL.search(str(row.get("detail_json") or ""))
        if match and row.get("task_id"):
            task_prs[row["task_id"]] = int(match.group(1))
    pr_sizes = {}
    if task_prs:
        pr_sizes = {
            int(row["number"]): int(row["changed"] or 0)
            for row in _rows(
                session, _PR_SIZES, {"numbers": sorted(set(task_prs.values()))}
            )
        }
    try:
        published = {
            row["session_key"]: row["issue_number"]
            for row in _rows(session, _PUBLISHED, {})
        }
    except Exception:  # noqa: BLE001 - links are optional
        logger.warning("factory retro could not read published sessions", exc_info=True)
        session.rollback()
        published = {}
    return {
        "window_hours": WINDOW_HOURS,
        "runs": runs,
        "receipts": receipts,
        "refusals": _rows(session, _REFUSALS, params),
        "verdicts": _rows(session, _VERDICTS, params),
        "turns": turns,
        "task_costs": {
            row["task_id"]: _f(row["cost"])
            for row in _rows(session, _TASK_COSTS, params)
        },
        "task_prs": task_prs,
        "pr_sizes": pr_sizes,
        "published": published,
        "github": _github_issues(now),
    }


def build_factory_retro_prompt(instructions: str) -> str:
    """The job's own instructions followed by the freshly built digest."""
    from core.db import get_engine
    from sqlmodel import Session

    with Session(get_engine()) as session:
        data = load_retro_data(session)
    return f"{instructions.strip()}\n\n{build_retro_digest(data)}"
