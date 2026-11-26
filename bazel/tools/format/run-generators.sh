#!/usr/bin/env bash
# Single source of truth for the repo's doc/config generators.
#
# Every pure-script doc/config generator is listed here ONCE and run
# by BOTH format paths, so the two can never drift (a generator in one list but
# not the other is the classic "green locally, red in CI" trap):
#   - CI + `bazel run //bazel/tools/format:format` run it via the :run_generators
#     target in the format multirun; the Format check action auto-commits any
#     drift like a formatting fix.
#   - local ci regen runs it when generator inputs change.
#
# Generators covered (each self-locates via BUILD_WORKSPACE_DIRECTORY, so this
# wrapper only cd's to the workspace and invokes them):
#   - the push-all BUILD list and the jomcgi-site posts manifest.
#
# Deliberately NOT here: sync-helm-deps and atlas-checksum generation. They need
# helm/atlas CLIs that are not in the CI format runner and have their own gates;
# ci regen syncs Helm dependencies when chart inputs change; pre-commit runs
# update-atlas-sum.sh for migrations. Keep this list to pure python/grep
# generators that are safe in CI.
#
# Failure handling: generators run in parallel (they write disjoint files); a
# non-zero exit from any one fails this script so CI catches a broken generator.
# ci regen also propagates generator failures.
set -uo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

pids=()
run() {
	"$@" &
	pids+=($!)
}

run ./bazel/images/generate-push-all.sh
run python3 ./projects/jomcgi-site/posts/gen_posts_manifest.py

fails=0
for p in "${pids[@]}"; do
	wait "$p" || fails=1
done
exit "$fails"
