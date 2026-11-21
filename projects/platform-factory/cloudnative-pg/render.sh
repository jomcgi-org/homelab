#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

wget -qO "$work/chart.tgz" https://github.com/cloudnative-pg/charts/releases/download/cloudnative-pg-v0.29.0/cloudnative-pg-0.29.0.tgz
echo "668e065ff53508d58238788fd35b355a925060843629a951df0e6a9362e6d32f  $work/chart.tgz" | sha256sum -c - >&2
helm template cnpg "$work/chart.tgz" --namespace cnpg-system \
	--kube-version 1.37.2 --include-crds --skip-tests \
	-f projects/platform/cloudnative-pg/deploy/values.yaml \
	-f projects/platform-factory/cloudnative-pg/values.yaml >"$work/render.yaml"
echo "8a3db818e3c6448ec045e7ab0c86bfb95121a9c5e53568738f556d9608b433b2  $work/render.yaml" | sha256sum -c - >&2
cat "$work/render.yaml"
