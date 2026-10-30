#!/usr/bin/env bash
# Callers resolve the base; this runner owns only guard order and arguments.
set -euo pipefail
if [[ $# -ne 2 || -z "$1" || -z "$2" ]]; then
	echo "usage: ratchets.sh base head" >&2
	exit 2
fi
python3 ./bazel/tools/ci/verification_genrule_guard.py .
python3 ./bazel/tools/ci/central_build_ratchet.py "$1" "$2"
python3 ./bazel/tools/ci/chart_version_guard.py "$1" "$2"
python3 ./bazel/tools/ci/source_ratchet.py "$1" "$2"
