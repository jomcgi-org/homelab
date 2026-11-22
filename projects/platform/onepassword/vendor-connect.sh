#!/usr/bin/env bash
# Vendor the 1Password Connect chart into charts/connect/ and apply
# connect.patch.
#
# Upstream renders connect-api and connect-sync from one shared
# connect.version, so values cannot give each image its own digest
# (HOME-287). The patch adds connect.api.version and connect.sync.version,
# which default to connect.version. Rerun after changing VERSION or
# SHA256; review the diff of charts/connect/. Needs helm, patch and
# sha256sum.
set -euo pipefail

VERSION="2.0.5"
SHA256="499c5e1cfb63038549a1ca49376fd44f3d55d48c9236b0b782952b858f4819c6"
REPO="https://1password.github.io/connect-helm-charts/"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

helm pull connect --repo "${REPO}" --version "${VERSION}" --destination "${tmp}"
echo "${SHA256}  ${tmp}/connect-${VERSION}.tgz" | sha256sum --check --quiet

rm -rf "${HERE}/charts/connect"
tar -xzf "${tmp}/connect-${VERSION}.tgz" -C "${HERE}/charts"
patch --quiet --strip=1 --directory="${HERE}/charts" <"${HERE}/connect.patch"
# The repo allows only the document names in docs/templates/bundles.yaml.
rm -f "${HERE}/charts/connect/CHANGELOG.md"
