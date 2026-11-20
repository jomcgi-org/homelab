#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
wget -qO "$archive" https://github.com/cloudnative-pg/charts/releases/download/cloudnative-pg-v0.29.0/cloudnative-pg-0.29.0.tgz
echo "668e065ff53508d58238788fd35b355a925060843629a951df0e6a9362e6d32f  $archive" | sha256sum -c -
helm template cnpg "$archive" --namespace cnpg-system --kube-version 1.37.2 --include-crds --skip-tests -f projects/platform/cloudnative-pg/deploy/values.yaml -f projects/platform-factory/cloudnative-pg/values.yaml | diff - projects/platform-factory/cloudnative-pg/resources/upstream/cloudnative-pg-0.29.0-install.yaml
