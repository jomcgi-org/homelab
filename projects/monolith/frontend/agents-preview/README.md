# Agents sheet evidence

This fixture composes the production root/public layouts, root error stylesheet,
agents route and neighboring profile-fixture components. It server-renders the
agents route, then hydrates the same component tree. The five phase panels are
the static presentation in every mode; no content depends on scrolling or JS.

From `projects/monolith/frontend`, with frozen workspace dependencies installed:

```sh
AGENTS_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config agents-preview/vite.config.js --outDir /tmp/agents-built
python3 agents-preview/browser_check.py --root /tmp/agents-built --output /tmp/agents-evidence --expected-sha "$(git rev-parse HEAD)"
```

Use fresh output paths and Playwright 1.63.0 with Chromium. Exact-head captures
require a clean worktree. Development captures use `local-unpublished` for both
the build variable and expected SHA and never count as final evidence.

The existing read-only fixture-preview workflow discovers this registered suite
through `bazel/tools/previews/suite.py`. It runs the DOM/timeline tests, builds
the SSR fixture and executes the checker. It retains 48 PNGs and `checks.json`
under `head/agents/` in the exact-SHA evidence artifact, with a PNG gallery in
the comparison preview. A base without this harness is explicitly unavailable,
never represented as a matched before capture. Checks cover 390/1440, light/dark, opening/resume states, resized
viewports, reduced motion, script-disabled SSR, a maintenance banner, keyboard
scheme changes, persistence and component navigation. The receipt records body
and document geometry, internal diagram overflow and computed paint.

Fixture limits: invented profile stats, a history adapter instead of the Kit
router, no backend/server loads or real-host acceptance, and local Schibsted
with system mono fallback instead of Google Fonts. Only the agents route has
server HTML; other direct fixture routes mount client-side. The error stylesheet
is eagerly included, so Kit's preload timing is not reproduced. Engineering
has no direct `/ember/agents` link at this task's base; its fixture link exercises
the component transition. Legacy Ember's existing story link is exercised.

Generated builds and evidence stay outside tracked sources. Merge, rollout and
real-host checks remain outstanding on #7152.
