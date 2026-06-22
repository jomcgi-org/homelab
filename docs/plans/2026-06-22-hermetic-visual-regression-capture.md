# Hermetic Bazel-native Visual Regression (Capture Migration) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move the public-page visual-regression capture + diff off the runtime `apt`/`npm`/`npx playwright install` CI shell step onto hermetic, input-cached Bazel actions that run chromium inside our own `ci/visual-chromium` apko image on RBE (ADR tooling/010).

**Architecture:** A `capture` Bazel action (inputs: the `:build_public` app bundle, committed fixtures + `targets.json`, and the rules_playwright-vendored chromium; executed in the `ci/visual-chromium` exec image via `exec_properties.container-image`) emits a PNG tree + `manifest.json`. A `diff` Bazel action (inputs: those PNGs + committed `baseline/*.png`) emits `report.json` + diff PNGs. The Bazel graph ends at the diff result; a thin `buildbuddy.yaml` step consumes the cached `bazel-bin` outputs and runs the existing `report-to-pr.sh` (PR comment) or, on the `.reseed-baselines` sentinel, `update-baselines.sh` (commit baselines back). The decisive property is caching by input: a PR that does not touch the frontend bundle / fixtures / targets / chromium is a cache hit and runs no browser.

**Tech Stack:** Bazel (bzlmod) + `aspect_rules_js` (`js_run_binary`) + `rules_playwright` + `rules_apko`, Playwright 1.55.0 (chromium rev 1187), Wolfi/apko exec image, BuildBuddy RBE CI.

---

## Background the executor MUST read first

- **The de-risk spike is done and green** on branch `feat/visreg-bazel-spike` (draft PR #2789). It proved the substrate end to end: `rules_playwright` vendors chromium; a per-target `exec_properties.container-image` override is honored by BuildBuddy RBE; chromium launches + renders inside `ghcr.io/jomcgi/homelab/ci/visual-chromium` (PUBLIC, dual-arch, glibc/Wolfi). Read that branch's diff before starting — it is the working reference for every Bazel/apko mechanic below.
- **ADR:** `docs/decisions/tooling/010-hermetic-visual-regression.md` (Accepted). This plan is its implementation.
- **The existing tool** (`projects/monolith/frontend/visual/`): `capture.mjs` boots `serve.mjs` (which spawns the `:build_public` adapter-node app with `API_BASE` → `mock-server.mjs` serving committed `fixtures/api/*.json`), intercepts `tiles.openfreemap.org` / `**/img/**` / `flagcdn.com` / chat SSE, freezes the clock, and screenshots 16 pages × 2 viewports (`targets.json`) → `out/<id>-<vp>.png` + `out/manifest.json`. `diff.mjs` pixelmatches `out/` vs `baseline/` → `out/report.json` + `out/diff/`. `report-to-pr.sh` posts the inline PR comment; `update-baselines.sh` reseeds on the `.reseed-baselines` sentinel.
- **Critical render detail:** map pages (`hikes`, `ships`, `stars`, `trips-*` with `"map": true`) render **WebGL** via `chromium.launch({ args: ["--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader"] })`. This is **full chromium, not headless-shell**, and swiftshader/ANGLE `dlopen`s GL libraries. This is the migration's #1 risk (Task 3).

## Cross-cutting rules (apply to EVERY task)

- **No local test loop.** Never run `bazel test`/`bazel build` as the inner loop (Mac runners absent; darwin/arm64 = "No registered executors"). Implement → commit → push → read CI. Read CI failures via `mcp__buildbuddy__get_invocation` (use the `commitSha` selector) → `get_log`; quote the actual error before hypothesizing.
- **Read CI status via the commit-status API**, NOT `statusCheckRollup`: `gh api repos/jomcgi/homelab/commits/<sha>/status --jq '.state, [.statuses[]|"\(.state) \(.context)"]'`. The required checks (`Format check`, `Test`, `Push images`, `Visual regression`, `Manifest diff`) post as commit statuses; they show as NULL in `statusCheckRollup` even when run. Only `Format check`/`Test`/`Push images` gate merge.
- **Rebase before every push.** `chart-version-bot` (and `ci-format-bot`) land commits on the branch on every push under `projects/**`; `git pull --rebase origin feat/visreg-hermetic-capture` before pushing, or the push is rejected. These bot commits are the only merge conflict.
- **Never commit to main.** All work on `feat/visreg-hermetic-capture` (worktree `/tmp/claude-worktrees/visreg-hermetic-capture`). Rebase-merge only.
- **Commit messages:** Conventional Commits, no em-dashes.
- **apko locks:** generate with the standalone `apko` binary (`apko lock apko.yaml`), NOT `bazel run @rules_apko//apko` (wrong-arch Exec-format on darwin). The local `update-apko-locks` pre-commit hook is wrong-arch too → commit apko.yaml changes with `SKIP=update-apko-locks`. The `apko_image` `_lock_test` only verifies `config.checksum == sha256(apko.yaml)`, so any apko version's lock passes as long as it was regenerated after the last apko.yaml edit.
- **pnpm-lock is pnpm-then-prettier.** After `pnpm install --lockfile-only`, run `prettier --write pnpm-lock.yaml` or the diff is a ~15k-line phantom (quote-style) churn. Verify the real diff is only the intended importer + package entries; preserve the root `pnpm.overrides`.
- **`//bazel/images:push_all` is auto-generated** by `bazel/images/generate-push-all.sh` (pure grep/awk, NO bazel): `BUILD_WORKSPACE_DIRECTORY=$PWD bash bazel/images/generate-push-all.sh`.
- **Indexed-markdown manifests.** This plan doc and any `docs/**` edit require regenerating `projects/monolith/knowledge/repo_docs_manifest.ndjson` (`python3 projects/monolith/knowledge/tools/gen_repo_docs_manifest.py`) AND the docs-site manifest (`python3 projects/monolith/knowledge/tools/gen_docs_manifest.py`), each with `BUILD_WORKSPACE_DIRECTORY=$PWD`. `git add` the new doc BEFORE generating (the generators use `git ls-files` = tracked only). Reliable guard: re-run the generator and confirm `git diff --stat` on the manifest is empty.

---

## Task 0: Commit this plan

**Files:**

- Create: `docs/plans/2026-06-22-hermetic-visual-regression-capture.md` (this file)
- Modify: `projects/monolith/knowledge/repo_docs_manifest.ndjson`, the docs-site manifest

**Step 1:** `git add docs/plans/2026-06-22-hermetic-visual-regression-capture.md` (BEFORE generating; the manifest generators see tracked files only).

**Step 2:** Regenerate both manifests:

```bash
cd /tmp/claude-worktrees/visreg-hermetic-capture
BUILD_WORKSPACE_DIRECTORY=$PWD python3 projects/monolith/knowledge/tools/gen_repo_docs_manifest.py
BUILD_WORKSPACE_DIRECTORY=$PWD python3 projects/monolith/knowledge/tools/gen_docs_manifest.py
```

**Step 3:** Re-run both generators and confirm `git diff --stat` shows no further manifest change (fresh regen = no-op). If a manifest changes on the second run, you generated too early.

**Step 4:** Commit: `docs(plan): hermetic visual regression capture migration (tooling/010)`. Push (rebase first). Confirm `Format check` passes (it validates manifest freshness via `validate-generate-scripts.sh`).

---

## Task 1: Bring the `ci/visual-chromium` apko image onto this branch

The image is already built and PUBLIC in GHCR; this task only re-lands its _definition_ on this branch so the capture target can reference it. Copy the four pieces from `feat/visreg-bazel-spike`.

**Files:**

- Create: `projects/monolith/frontend/visual/apko.yaml`, `projects/monolith/frontend/visual/apko.lock.json`, `projects/monolith/frontend/visual/BUILD`
- Modify: `MODULE.bazel` (add `apko.translate_lock(name="visual_chromium_lock", lock="//projects/monolith/frontend/visual:apko.lock.json")` + add `"visual_chromium_lock"` to the `use_repo(apko, ...)` list), `bazel/images/BUILD` (regenerated)

**Step 1:** Copy the files verbatim from the spike branch:

```bash
git show feat/visreg-bazel-spike:projects/monolith/frontend/visual/apko.yaml > projects/monolith/frontend/visual/apko.yaml
git show feat/visreg-bazel-spike:projects/monolith/frontend/visual/apko.lock.json > projects/monolith/frontend/visual/apko.lock.json
```

The `visual/BUILD` from the spike contains only the `apko_image(name="visual_chromium", repository="ghcr.io/jomcgi/homelab/ci/visual-chromium", ...)` target — copy that target (later tasks add the capture/diff targets to the same BUILD). The apko.yaml at this point is the spike's final 161-pkg set (chromium libs + `findutils/grep/sed/gawk` userland + `libudev`/`glib`); Task 3 extends it for WebGL.

**Step 2:** Add the `translate_lock` + `use_repo` entry to `MODULE.bazel` (see the spike's diff for exact placement next to `monolith_frontend_lock`).

**Step 3:** Regenerate push_all: `BUILD_WORKSPACE_DIRECTORY=$PWD bash bazel/images/generate-push-all.sh`. Confirm it adds `//projects/monolith/frontend/visual:visual_chromium.push`.

**Step 4:** Commit (`SKIP=update-apko-locks`): `feat(visual): land ci/visual-chromium apko image definition`. Push (rebase). Verify `Test` (the `_lock_test` passes) and `Push images` green; confirm GHCR still has the public package (no rebuild needed, but a republish is harmless).

---

## Task 2: Make `visual/` a pnpm workspace member, align Playwright to 1.55.0

**Files:**

- Modify: `pnpm-workspace.yaml` (add `projects/monolith/frontend/visual`), `projects/monolith/frontend/visual/package.json` (`@playwright/test` `1.49.1` → `1.55.0`; keep `pixelmatch`, `pngjs`), `pnpm-lock.yaml`

**Step 1:** Add `projects/monolith/frontend/visual` to `pnpm-workspace.yaml` `packages:`.

**Step 2:** Edit `package.json`: pin `"@playwright/test": "1.55.0"` (must equal the `playwright_version` the spike pinned, so the npm driver matches the vendored chromium rev 1187).

**Step 3:** Regenerate the lock and prettier it:

```bash
pnpm install --lockfile-only && prettier --write pnpm-lock.yaml
```

**Step 4:** Verify `git diff pnpm-lock.yaml` is a small, sane change: a new `projects/monolith/frontend/visual` importer + `@playwright/test@1.55.0` (+ `pixelmatch`/`pngjs`) entries, root `pnpm.overrides` intact, no unrelated resolution churn. If the diff is ~15k lines you forgot the `prettier --write`.

**Step 5:** Commit: `build(visual): add visual tool to pnpm workspace, pin playwright 1.55.0`. Push (rebase). Verify `Format check`/`Test` green.

---

## Task 3: Extend the apko image for WebGL (HIGHEST RISK — verify maps render)

The spike's image satisfies `headless_shell` for a static page. The real capture uses **full chromium + swiftshader WebGL**, which needs GL libraries the binary `dlopen`s (not in its `DT_NEEDED`). This task is iterative and gated on a real map render; budget several CI rounds.

**Files:**

- Modify: `projects/monolith/frontend/visual/apko.yaml`, `projects/monolith/frontend/visual/apko.lock.json`

**Step 1: Enumerate full-chromium direct deps.** Download the full chromium build (NOT headless-shell) for rev 1187 and parse its ELF `DT_NEEDED`:

```bash
curl -sL "https://cdn.playwright.dev/dbazure/download/playwright/builds/chromium/1187/chromium-linux.zip" -o /tmp/cr.zip
unzip -oq /tmp/cr.zip -d /tmp/cr
# Parse DT_NEEDED of /tmp/cr/chrome-linux/chrome with the Python ELF parser from the spike session.
```

Map each `.so` to a Wolfi package via the APKINDEX `so:` provides (download `https://packages.wolfi.dev/os/x86_64/APKINDEX.tar.gz`, `awk` for `so:<lib>`). Add any not already present.

**Step 2: Add the swiftshader/ANGLE GL stack.** These are `dlopen`ed (won't appear in `DT_NEEDED`): start with `mesa` (libGL), `mesa-egl`/`libegl`, `mesa-gles`, `libglvnd` as available in Wolfi (resolve exact names from the APKINDEX). swiftshader itself is bundled inside chromium, but it links the system GL/EGL loader.

**Step 3:** Regenerate the lock (`apko lock apko.yaml`), verify the `config.checksum` matches, commit (`SKIP=update-apko-locks`): `fix(visual): add WebGL/GL libs to ci/visual-chromium for swiftshader`. Push (rebase). Note the rebuilt image digest from `Push images` (`gh api user/packages/container/homelab%2Fci%2Fvisual-chromium/versions --jq '.[0].name'`).

**Step 4: VERIFY a map actually renders** before building out the full capture. Temporarily point the spike's `spike_test` (or a minimal one-page capture target — see Task 4) at a map page (e.g. `stars`) running in the new image digest, and confirm the produced PNG is a non-trivial render (not a blank GL canvas). Iterate Steps 1-3 until a map page renders. **Do not proceed to Task 5's full baseline reseed until a map page is confirmed rendering.**

> If swiftshader WebGL proves intractable in apko within a reasonable number of rounds, STOP and surface it: fallback options are (a) bake a heavier mesa stack, or (b) reconsider the exec image base. Do not silently ship maps as blank.

---

## Task 4: Bazel-ify the capture action

**Files:**

- Modify: `projects/monolith/frontend/visual/BUILD` (add `npm_link_all_packages`, a `js_library` for the mjs+fixtures+targets, and a `js_run_binary` capture target), `projects/monolith/frontend/visual/serve.mjs` / `capture.mjs` (launch-arg + path adjustments)

**Step 1:** Add to `visual/BUILD`:

- `npm_link_all_packages(name = "node_modules")`
- a `js_library` bundling `capture.mjs`, `serve.mjs`, `mock-server.mjs`, `targets.json`, `fixtures/**`, `package.json`, with deps `:node_modules/@playwright/test`.
- a `js_run_binary` `capture` target: `tool` = a `js_binary` wrapping `capture.mjs`; `srcs`/`data` = the js_library + `//projects/monolith/frontend:build_public` + `@playwright//:chromium`; `out_dirs = ["out"]`; `env = { "PLAYWRIGHT_BROWSERS_PATH": "$(rootpath @playwright//:chromium)/../", "APP_ENTRY": "$(location //projects/monolith/frontend:build_public)/index.js" }`; `exec_properties = { "container-image": "docker://ghcr.io/jomcgi/homelab/ci/visual-chromium@sha256:<digest from Task 3>" }`. Mirror the spike's `spike_test` wiring (no `chdir`; `$(rootpath)` is exec-root relative).

**Step 2:** Adjust `capture.mjs` launch args: merge the container flags with the WebGL flags →
`args: ["--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader","--no-sandbox","--disable-dev-shm-usage"]` (NOT `--disable-gpu` — it would kill swiftshader). Confirm `serve.mjs` resolves `APP_ENTRY` from the env the BUILD sets and that the app boots under the exec image (node from the hermetic toolchain).

**Step 3:** Confirm `exec_properties` is honored on `js_run_binary` the same way it was on `js_test` (the spike). If a `js_run_binary` action does not pick up the container override on RBE, fall back to a `genrule`/`ctx.actions.run` whose `exec_properties` are set, or a `js_test`-shaped capture that writes undeclared outputs — investigate and pick the form that both runs in the image AND yields retrievable PNGs.

**Step 4:** Commit: `feat(visual): bazel-native capture action in ci/visual-chromium`. Push (rebase). The capture won't be diffed yet; verify it BUILDS (`bazel build //projects/monolith/frontend/visual:capture` succeeds in CI — i.e. `Test`/`Push images` that build `//...` stay green) and, via the BuildBuddy log, that the action ran the browser and emitted `out/`.

---

## Task 5: Bazel-ify the diff action + reseed baselines

**Files:**

- Modify: `projects/monolith/frontend/visual/BUILD` (add `diff` target), `projects/monolith/frontend/visual/diff.mjs` (path/output adjustments if needed), create `projects/monolith/frontend/visual/.reseed-baselines` (sentinel, temporary)

**Step 1:** Add a `diff` Bazel target (a `js_run_binary` running `diff.mjs`): inputs = the `capture` target's `out` PNGs + `baseline/**` + `manifest.json`; `out` = `report.json` + `diff/**`. `diff.mjs` reads paths relative to its inputs rather than a fixed `HERE/out` — adjust to read capture outputs from the declared input dir and baselines from `baseline/`.

**Step 2:** Because chromium 1187 + apko fonts differ from the old CI container, ALL baselines change. Commit an empty `projects/monolith/frontend/visual/.reseed-baselines` sentinel so the (still-current) reseed flow regenerates them. (Until Task 6 cuts the CI step over, the reseed runs through the existing `update-baselines.sh`; after Task 6, through the bazel outputs.) Coordinate ordering with Task 6 — recommended: do Task 6 first so the reseed runs against the bazel `capture` output, then drop the sentinel.

**Step 3:** Commit: `feat(visual): bazel-native diff action`. Push (rebase).

> **Reseed footgun (from memory):** NEVER enable auto-merge on the reseed PR. Auto-merge fires when the fast checks go green while the visual action is still capturing, so the baseline commit-back is skipped and the sentinel leaks to main. Wait for the `visual-baseline-bot` "update visual baselines" commit to land on the branch (`git ls-tree <branch> .../.reseed-baselines` removed), THEN merge.

---

## Task 6: Cut over `buildbuddy.yaml` "Visual regression" step

**Files:**

- Modify: `buildbuddy.yaml` (the `Visual regression` step), `projects/monolith/frontend/visual/update-baselines.sh` + `report-to-pr.sh` (read from `bazel-bin` instead of `out/`)

**Step 1:** Replace the step body: drop the `apt-get`/`npm install`/`npx playwright install`/`node capture.mjs`/`node diff.mjs` block. New body:

```bash
bazel build //projects/monolith/frontend/visual:diff --config=ci --remote_download_outputs=all
```

Then resolve the diff outputs from the `bazel-bin` convenience symlink (NOT `bazel info bazel-bin --config=ci`, which fails with "@@buildbuddy_toolchain not resolved"). Keep the `visual-baseline-bot|argocd-image-updater` author skip-guard and the `gh pr list --head "$BRANCH"` PR-number resolution.

**Step 2:** Point `report-to-pr.sh` / `update-baselines.sh` at the `bazel-bin/.../visual/{capture_out,diff}` paths instead of `projects/monolith/frontend/visual/out`. These stay thin workflow steps (they need only `gh`/`jq`/`git`); keep their minimal `apt` for `gh`/`jq` for now (a future `ci/` image can remove it — out of scope).

**Step 3:** The step keeps `--remote_download_outputs=all` (node reads the bazel tree outputs outside bazel) and `--config=ci` (the mixed-arch workflow pool shares `~/.cache`; `build:ci --disk_cache=` disables it, avoiding wrong-arch binaries).

**Step 4:** Commit: `ci(visual): run capture+diff as hermetic bazel actions`. Push (rebase). Now run the reseed (Task 5 Step 2 sentinel) so baselines regenerate against the bazel capture. Verify the `Visual regression` step posts a correct PR comment and that a no-frontend-change commit produces a `diff` cache hit (no browser run — confirm in the BuildBuddy log).

---

## Task 7: Confirm caching + finalize

**Step 1:** Push a trivial non-frontend commit (e.g. a comment in an unrelated file) and confirm via the BuildBuddy log that `//projects/monolith/frontend/visual:diff` is a **cache hit** and no chromium runs — the ADR's decisive property.

**Step 2:** Confirm a real frontend change (touch a public `.svelte`) re-runs the capture and the PR comment shows the changed page(s).

**Step 3:** Ensure NO `visual-spike/` directory exists on this branch (it was the throwaway; it lives only on `feat/visreg-bazel-spike` and must not be carried over).

**Step 4:** Update `docs/decisions/tooling/010-hermetic-visual-regression.md` status note (Open Questions now resolved: ruleset = rules_playwright + caveats; exec = RBE via apko exec image; package set = the committed apko.yaml). Regenerate the doc manifests (Task 0 procedure). Commit.

---

## End of plan: review + merge

- **One comprehensive code review** against the full PR diff (per repo policy: one review per merged PR, not per task). Use the code-reviewer agent over the squashed diff.
- All test execution was deferred to CI on the pushed branch (no local bazel test loop).
- Merge: rebase-merge only. **Do not auto-merge** while a `.reseed-baselines` sentinel is unconsumed (wait for `visual-baseline-bot`); once baselines are static committed content, normal merge is fine.
- After merge: verify the `Visual regression` step on `main` and that the next unrelated PR gets a `diff` cache hit.
