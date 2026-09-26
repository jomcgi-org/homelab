@../AGENTS.md

# Claude Code specifics

Everything above applies. This file only adds what is particular to Claude
Code; anything every agent needs belongs in `AGENTS.md`.

- **Hooks** in `.claude/settings.json` are fast-fail duplicates of the CI and
  git gates, plus a few advisories. A rule that must hold for every author
  lives in CI or a git hook, never only here (`bazel/ARCHITECTURE.md`,
  Hooks).
- **Agents** (`.claude/agents/`): `reviewer` reviews a finished PR diff once,
  at the end, and has no `Write` or `Edit` tool; `stpa-analyst` refreshes a
  system's safety model. Search with `Explore` rather than a general-purpose
  agent.
- **Local memory** (`~/.claude/projects/<path-slug>/memory/`) is private to one
  machine and unreviewed. Treat an entry as a lead and grep the artifact before
  acting on it. Anything another agent or a future session needs goes to the
  KG with `report_knowledge`.
- The BuildBuddy MCP needs `BUILDBUDDY_API_KEY` in the environment before the
  session starts.
