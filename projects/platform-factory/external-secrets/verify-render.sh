#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
wget -qO "$archive" https://github.com/external-secrets/external-secrets/releases/download/helm-chart-2.15.0/external-secrets-2.15.0.tgz
echo "ed94d41cc67e392ea45e370345eb491b0fdc21387f1aaaefcf8e2e6c85089e1d  $archive" | sha256sum -c -
helm template external-secrets "$archive" --namespace external-secrets --kube-version 1.37.2 --include-crds --skip-tests -f projects/platform-factory/external-secrets/values.yaml | diff - projects/platform-factory/external-secrets/resources/upstream/external-secrets-2.15.0-install.yaml
