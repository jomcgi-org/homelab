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
#   - the push-all BUILD list, monolith routes, the posts manifest, the ADR 036
#     orchestrator context bundle (orchestrator_bundle.md), and the guest
#     env-readmes (ADR agents/044:
#     environment.md per guest image, derived from that guest's
#     apko.lock.json + env-notes.md).
#
# Deliberately NOT here: the repo-docs and public docs manifests. They are
# build outputs of genrules (#6446), never committed, so nothing regenerates
# or checks them in; validate-generate-scripts.sh checks their inputs instead.
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

# Guest env-readmes run serially BEFORE the parallel batch, so a failing one
# exits before the rest start. (They were ordered ahead of the doc-manifest
# generators, which read environment.md; those are genrules now, #6446.)
for sandbox_lang in python go rust elixir ocaml javascript; do
	python3 ./projects/firecracker/tools/env_readme/gen_env_readme.py \
		--lock "projects/firecracker/sandbox/${sandbox_lang}/apko.lock.json" \
		--title "${sandbox_lang} sandbox guest environment" \
		--notes "projects/firecracker/sandbox/${sandbox_lang}/env-notes.md" \
		--out "projects/firecracker/sandbox/${sandbox_lang}/environment.md" || exit 1
done

run ./bazel/images/generate-push-all.sh
run ./projects/monolith/generate-routes.sh
run python3 ./projects/monolith/knowledge/tools/gen_posts_manifest.py
run python3 ./projects/monolith/knowledge/tools/gen_orchestrator_bundle.py

fails=0
for p in "${pids[@]}"; do
	wait "$p" || fails=1
done
exit "$fails"
