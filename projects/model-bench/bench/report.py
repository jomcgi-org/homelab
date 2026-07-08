from __future__ import annotations


def render_leaderboard(
    *,
    per_class: dict,
    anchors: dict,
    frontier: dict,
    retired: list,
    agentic: dict | None = None,
    agentic_anchor_ids: set | None = None,
) -> str:
    """Render a markdown leaderboard report.

    Args:
        per_class: model_id -> task_class -> dict with keys pass1, cost, tier, qualifies.
        anchors: model_id -> task_class -> dict with keys pass1, cost.
        frontier: task_class -> list of non-dominated model ids.
        retired: list of dicts with keys id, reason, date, pass1, cost.
        agentic: model_id -> dict with keys n, pass_rate, mean_tokens, mean_turns,
                 cost, tool_ok_rate. The agentic tool-calling leaderboard: the
                 primary contract of this benchmark. Optional/back-compatible.
        agentic_anchor_ids: set of model_ids in `agentic` that are anchors. They are
                 pulled out of the cost-ranked candidate tables and shown in the frontier
                 CEILING section (pass + wall-time only), since they run via Claude Code
                 (own harness, free under Max) and their cost=0 would otherwise dominate.

    Returns:
        Markdown string with sections: Agentic, Frontier ceiling, Budget tier, Anchors,
        Pareto frontier, Retired. Section headers are fixed and always emitted even when
        inputs are empty.
    """
    lines: list[str] = []
    lines.append("# model-bench leaderboard")
    lines.append("")

    # Agentic leaderboard under the gate model: a model must clear the easy+standard
    # FLOOR to qualify; the qualified are ranked by hard-task pass then cost, with the
    # perf/efficiency columns as the value axis. The disqualified are listed with the
    # floor task(s) they missed.
    _anchor_ids = agentic_anchor_ids or set()
    rows = [
        {"model": mid, **stats}
        for mid, stats in (agentic or {}).items()
        if mid not in _anchor_ids
    ]
    qualified = [r for r in rows if r.get("qualified")]
    disqualified = [r for r in rows if not r.get("qualified")]

    def _cps(r) -> str:
        cps = r.get("cost_per_solve")
        return f"{cps:.4f}" if cps is not None else "n/a"

    lines.append("## Agentic leaderboard: qualified")
    lines.append("")
    lines.append(
        "Cleared the easy+standard floor. Ranked by hard-task pass, then cost."
    )
    lines.append("")
    if qualified:
        # Best first: most hard tasks solved, then cheapest, then fastest.
        qualified.sort(
            key=lambda r: (
                -r.get("hard_pass", 0),
                r.get("cost", 0.0),
                r.get("mean_latency_ms", 0.0),
            )
        )
        lines.append(
            "| Model | hard | mean tokens | mean turns | wall-time (s) "
            "| cost ($) | $/solve | tool-use ok |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in qualified:
            lines.append(
                f"| {r['model']} | {r.get('hard_pass', 0)}/{r.get('hard_n', 0)} "
                f"| {r.get('mean_tokens', 0):.0f} | {r.get('mean_turns', 0):.1f} "
                f"| {r.get('mean_latency_ms', 0) / 1000:.1f} "
                f"| {r.get('cost', 0.0):.4f} | {_cps(r)} | {r.get('tool_ok_rate', 0.0):.2f} |"
            )
    else:
        lines.append("No qualified models yet.")
    lines.append("")

    lines.append("## Agentic leaderboard: disqualified")
    lines.append("")
    lines.append("Failed one or more floor (easy/standard) tasks, so not yet viable.")
    lines.append("")
    if disqualified:
        disqualified.sort(key=lambda r: -r.get("floor_pass", 0))
        lines.append("| Model | floor | failed floor tasks | tool-use ok |")
        lines.append("| --- | --- | --- | --- |")
        for r in disqualified:
            failed = ", ".join(r.get("floor_failed", [])) or "(none run)"
            lines.append(
                f"| {r['model']} | {r.get('floor_pass', 0)}/{r.get('floor_n', 0)} "
                f"| {failed} | {r.get('tool_ok_rate', 0.0):.2f} |"
            )
    else:
        lines.append("No disqualified models.")
    lines.append("")

    # Frontier ceiling: anchors (Claude via Claude Code) run the same agentic tasks under
    # their own harness. This calibrates task difficulty ("can the frontier even do it")
    # without pretending to be a cost-ranked candidate. Pass + wall-time only; no cost,
    # no turns/tokens (a different harness, not comparable to the OpenRouter rows).
    lines.append("## Frontier ceiling (agentic)")
    lines.append("")
    lines.append(
        "Claude anchors run via Claude Code (Max subscription, free), graded by the same "
        "verifier. A capability ceiling, not a ranked competitor: no cost or token/turn "
        "columns, since the harness differs from the candidate rows."
    )
    lines.append("")
    ceiling_rows = [
        {"model": mid, **stats}
        for mid, stats in (agentic or {}).items()
        if mid in _anchor_ids
    ]
    if ceiling_rows:
        ceiling_rows.sort(
            key=lambda r: (-r.get("hard_pass", 0), r.get("mean_latency_ms", 0.0))
        )
        lines.append("| Model | hard | pass rate | wall-time (s) | tasks |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in ceiling_rows:
            lines.append(
                f"| {r['model']} | {r.get('hard_pass', 0)}/{r.get('hard_n', 0)} "
                f"| {r.get('pass_rate', 0.0):.2f} "
                f"| {r.get('mean_latency_ms', 0) / 1000:.1f} | {r.get('n', 0)} |"
            )
    else:
        lines.append("No anchor ceiling results yet.")
    lines.append("")

    # Budget tier: qualifying candidates sorted by cost ascending
    lines.append("## Budget tier")
    lines.append("")
    qualifying_rows: list[dict] = []
    for model_id, class_data in per_class.items():
        for cls, scores in class_data.items():
            if scores.get("qualifies"):
                qualifying_rows.append(
                    {
                        "model": model_id,
                        "class": cls,
                        "pass1": scores.get("pass1", 0.0),
                        "cost": scores.get("cost", 0.0),
                        "tier": scores.get("tier", ""),
                    }
                )
    qualifying_rows.sort(key=lambda r: r["cost"])

    if qualifying_rows:
        lines.append("| Model | Class | pass@1 | cost ($) | tier |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in qualifying_rows:
            lines.append(
                f"| {row['model']} | {row['class']} "
                f"| {row['pass1']:.2f} | {row['cost']:.4f} | {row['tier']} |"
            )
    else:
        lines.append("No qualifying budget candidates yet.")
    lines.append("")

    # All results: every candidate x class, including non-qualifiers, so rejects
    # (models that fail the bar or cost more than it) stay visible instead of
    # vanishing from the only candidate table. This tool is used to reject models,
    # so the rejects have to be shown.
    lines.append("## All results")
    lines.append("")
    all_rows: list[dict] = []
    for model_id, class_data in per_class.items():
        for cls, scores in class_data.items():
            all_rows.append(
                {
                    "model": model_id,
                    "class": cls,
                    "pass1": scores.get("pass1", 0.0),
                    "cost": scores.get("cost", 0.0),
                    "tier": scores.get("tier", ""),
                    "qualifies": "yes" if scores.get("qualifies") else "no",
                }
            )
    all_rows.sort(key=lambda r: (r["class"], r["cost"]))
    if all_rows:
        lines.append("| Model | Class | pass@1 | cost ($) | tier | qualifies |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in all_rows:
            lines.append(
                f"| {row['model']} | {row['class']} | {row['pass1']:.2f} "
                f"| {row['cost']:.4f} | {row['tier']} | {row['qualifies']} |"
            )
    else:
        lines.append("No results yet.")
    lines.append("")

    # Anchors: baseline pass1/cost per class
    lines.append("## Anchors")
    lines.append("")
    if anchors:
        lines.append("| Model | Class | pass@1 | cost ($) |")
        lines.append("| --- | --- | --- | --- |")
        for model_id, class_data in anchors.items():
            for cls, scores in class_data.items():
                p1 = scores.get("pass1", 0.0)
                cost = scores.get("cost", 0.0)
                lines.append(f"| {model_id} | {cls} | {p1:.2f} | {cost:.4f} |")
    else:
        lines.append("No anchors defined.")
    lines.append("")

    # Pareto frontier: non-dominated models per class
    lines.append("## Pareto frontier")
    lines.append("")
    if frontier:
        for cls in sorted(frontier.keys()):
            models = frontier[cls]
            model_list = ", ".join(models) if models else "none"
            lines.append(f"**{cls}:** {model_list}")
    else:
        lines.append("No frontier data yet.")
    lines.append("")

    # Retired tombstone: preserves institutional memory of why experiments failed
    lines.append("## Retired")
    lines.append("")
    if retired:
        lines.append("| Model | final pass@1 | cost ($) | reason | date |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in retired:
            model_id = entry.get("id", "")
            p1 = entry.get("pass1", 0.0)
            cost = entry.get("cost", 0.0)
            reason = entry.get("reason", "")
            date = entry.get("date", "")
            lines.append(f"| {model_id} | {p1:.2f} | {cost:.4f} | {reason} | {date} |")
    else:
        lines.append("No retired models.")
    lines.append("")

    return "\n".join(lines)
