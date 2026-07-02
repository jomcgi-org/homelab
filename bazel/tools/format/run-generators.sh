#!/usr/bin/env bash
# Single source of truth for the repo's doc/config generators.
#
# Every generator that produces a COMMITTED artifact is listed here ONCE and run
# by BOTH format paths, so the two can never drift (a generator in one list but
# not the other is the classic "green locally, red in CI" trap):
#   - CI + `bazel run //bazel/tools/format:format` run it via the :run_generators
#     target in the format multirun; the Format check action auto-commits any
#     drift like a formatting fix.
#   - local pre-commit runs it via bazel/tools/format/fast-format.sh.
#
# Generators covered (each self-locates via BUILD_WORKSPACE_DIRECTORY, so this
# wrapper only cd's to the workspace and invokes them):
#   - home-cluster kustomization, the push-all BUILD list, monolith routes,
#     and the two doc-index manifests
#     (repo_docs_manifest.ndjson + the public docs-manifest.json).
#
# Deliberately NOT here: sync-helm-deps and atlas-checksum generation. They need
# helm/atlas CLIs that are not in the CI format runner and have their own gates;
# fast-format.sh runs them locally. Keep this list to pure python/grep generators
# that are safe in CI.
#
# Failure handling: generators run in parallel (they write disjoint files); a
# non-zero exit from any one fails this script so CI catches a broken generator.
# fast-format.sh wraps the call so a local generator hiccup never blocks a commit.
set -uo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

pids=()
run() {
	"$@" &
	pids+=($!)
}

run ./bazel/images/generate-home-cluster.sh
run ./bazel/images/generate-push-all.sh
run ./projects/monolith/generate-routes.sh
run python3 ./projects/monolith/knowledge/tools/gen_repo_docs_manifest.py
run python3 ./projects/monolith/knowledge/tools/gen_docs_manifest.py

fails=0
for p in "${pids[@]}"; do
	wait "$p" || fails=1
done
exit "$fails"
