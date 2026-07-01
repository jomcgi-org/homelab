from __future__ import annotations


def render_leaderboard(
    *,
    per_class: dict,
    anchors: dict,
    frontier: dict,
    retired: list,
    agentic: dict | None = None,
) -> str:
    """Render a markdown leaderboard report.

    Args:
        per_class: model_id -> task_class -> dict with keys pass1, cost, tier, qualifies.
        anchors: model_id -> task_class -> dict with keys pass1, cost.
        frontier: task_class -> list of non-dominated model ids.
        retired: list of dicts with keys id, reason, date, pass1, cost.
        agentic: model_id -> dict with keys n, pass_rate, med_tokens, med_turns,
                 cost, tool_ok_rate. The agentic tool-calling leaderboard: the
                 primary contract of this benchmark. Optional/back-compatible.

    Returns:
        Markdown string with sections: Agentic, Budget tier, Anchors, Pareto frontier,
        Retired. Section headers are fixed and always emitted even when inputs are empty.
    """
    lines: list[str] = []
    lines.append("# model-bench leaderboard")
    lines.append("")

    # Agentic leaderboard: pass-rate + efficiency (tokens/turns) + $ over real-repo
    # tool-calling tasks. This is the headline contract; single-shot tables follow.
    lines.append("## Agentic (tool-calling) leaderboard")
    lines.append("")
    if agentic:
        rows = [{"model": mid, **stats} for mid, stats in agentic.items()]
        # Best first: highest pass-rate, then cheapest, then fewest tokens.
        rows.sort(
            key=lambda r: (
                -r.get("pass_rate", 0.0),
                r.get("cost", 0.0),
                r.get("med_tokens", 0.0),
            )
        )
        lines.append(
            "| Model | n | pass-rate | median tokens | median turns "
            "| cost ($) | tool-use ok |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            lines.append(
                f"| {r['model']} | {r.get('n', 0)} | {r.get('pass_rate', 0.0):.2f} "
                f"| {r.get('med_tokens', 0):.0f} | {r.get('med_turns', 0):.1f} "
                f"| {r.get('cost', 0.0):.4f} | {r.get('tool_ok_rate', 0.0):.2f} |"
            )
    else:
        lines.append("No agentic results yet.")
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
