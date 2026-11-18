#!/usr/bin/env bash
# Run the browser scenario after the runner has Python/uv, pnpm and PG16.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
evidence_dir="${GRIMOIRE_EVIDENCE_DIR:-/tmp/grimoire-ci-evidence}"
mkdir -p "$evidence_dir"
# An opted-in run must never leave an earlier run's stability summary behind:
# clear it before setup, so an early failure cannot show old passes.
if [[ "${GRIMOIRE_CHECK_STABILITY:-0}" == 1 ]]; then
	rm -rf "$evidence_dir/stability" "$evidence_dir/stability.log"
fi
ci_environment="$(mktemp -d /tmp/grimoire-ci-python-XXXXXX)"
trap 'rm -rf "$ci_environment"' EXIT

uv venv --python 3.13 "$ci_environment/venv"
uv pip install --python "$ci_environment/venv/bin/python" --no-deps -r bazel/requirements/runtime.txt
uv pip install --python "$ci_environment/venv/bin/python" playwright==1.63.0
"$ci_environment/venv/bin/playwright" install chromium
pnpm install --frozen-lockfile --ignore-scripts

# The friends UI is static files. Build the React bundle once from this exact
# checkout; every launch below serves it through grimoire/web.py, so there is
# no dev server and no dependency cache to warm.
export GRIMOIRE_WEB_DIR="$ci_environment/grimoire-web"
GRIMOIRE_BASE=/grimoire/ pnpm --dir projects/monolith/web/grimoire exec vite build \
	--outDir "$GRIMOIRE_WEB_DIR" --emptyOutDir \
	2>&1 | tee "$evidence_dir/web-build.log"

# Every stage runs even after an earlier one fails, so one failed check never
# hides the evidence of the others. The wrapper still exits nonzero.
failed=0
# Both launches own a fresh database. Each start must serve the shell and
# every asset it names before the browser opens (see run.py).
(
	status=0
	for attempt in 1 2; do
		"$ci_environment/venv/bin/python" projects/monolith/dev/grimoire/run.py \
			--rehearse --output "$evidence_dir/cold-start-$attempt" || status=1
	done
	exit "$status"
) 2>&1 | tee "$evidence_dir/run.log" || failed=1
grep -c '^App shell ready' "$evidence_dir/run.log" >"$evidence_dir/startup.log" || failed=1
# The full matrix includes all existing checks and the additional journeys,
# using a fresh database for each desktop/phone session and link scenario.
"$ci_environment/venv/bin/python" projects/monolith/dev/grimoire/run.py \
	--rehearse --all --output "$evidence_dir/all" \
	2>&1 | tee "$evidence_dir/all.log" || failed=1

# On demand only (GRIMOIRE_CHECK_STABILITY=1, HOME-369): twenty phone sessions
# in this same pinned environment, each with all 53 checks and a fresh database.
# pr-checks does not set it; twenty sessions do not fit in one job.
if [[ "${GRIMOIRE_CHECK_STABILITY:-0}" == 1 ]]; then
	"$ci_environment/venv/bin/python" projects/monolith/dev/grimoire/stability.py \
		--output "$evidence_dir/stability" \
		2>&1 | tee "$evidence_dir/stability.log" || failed=1
	cat "$evidence_dir/stability/report.md" |
		tee -a "$evidence_dir/stability.log" || failed=1
fi

# Public sheet evidence uses invented responses only. Reuse the same pinned
# browser environment and artifact upload, without changing workflow permissions
# or any production publish step. Build from this exact checkout before capture.
# The public Grimoire pages stay in Svelte, so this build needs its manifest.
python3 projects/monolith/knowledge/tools/gen_docs_manifest.py
pnpm --dir projects/monolith/frontend exec vite build \
	--config grimoire-preview/vite.config.js \
	--outDir "$ci_environment/public-sheet-build" \
	2>&1 | tee "$evidence_dir/public-sheet-build.log" || failed=1
"$ci_environment/venv/bin/python" projects/monolith/frontend/grimoire-preview/browser_check.py \
	--artifact "$ci_environment/public-sheet-build" \
	--output "$evidence_dir/public-sheet" \
	--expected-sha "$(git rev-parse HEAD)" \
	2>&1 | tee "$evidence_dir/public-sheet.log" || failed=1
exit "$failed"
