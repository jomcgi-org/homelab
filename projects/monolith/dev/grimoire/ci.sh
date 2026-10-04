#!/usr/bin/env bash
# Run the browser scenario after the runner has Python/uv, pnpm and PG16.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
evidence_dir="${GRIMOIRE_EVIDENCE_DIR:-/tmp/grimoire-ci-evidence}"
mkdir -p "$evidence_dir"
ci_environment="$(mktemp -d /tmp/grimoire-ci-python-XXXXXX)"
trap 'rm -rf "$ci_environment"' EXIT

uv venv --python 3.13 "$ci_environment/venv"
uv pip install --python "$ci_environment/venv/bin/python" --no-deps -r bazel/requirements/runtime.txt
uv pip install --python "$ci_environment/venv/bin/python" playwright==1.63.0
"$ci_environment/venv/bin/playwright" install chromium
pnpm install --frozen-lockfile --ignore-scripts
python3 projects/monolith/knowledge/tools/gen_docs_manifest.py
pnpm --dir projects/monolith/frontend exec node --test test/grimoire-dev-startup.test.mjs \
	2>&1 | tee "$evidence_dir/startup.log"
# Both launches own a fresh database and Vite cache. A warm second browser
# session cannot hide a dependency-discovery regression.
for attempt in 1 2; do
	"$ci_environment/venv/bin/python" projects/monolith/dev/grimoire/run.py \
		--rehearse --output "$evidence_dir/cold-start-$attempt"
done 2>&1 | tee "$evidence_dir/run.log"
