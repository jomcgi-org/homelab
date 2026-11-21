#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/resources"
version=$(sed -n 's/^    newTag: //p' kustomization.yaml)
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9.-]+)?$ ]]
old=$(sed -n 's/^  - \(upstream\/cfgate-.*-install.yaml\)$/\1/p' kustomization.yaml)
[[ -f "$old" ]]
file="upstream/cfgate-v${version}-install.yaml"
url="https://github.com/cfgate/cfgate/releases/download/v${version}/install.yaml"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$url" >"$tmp"
grep -Fqx "        image: ghcr.io/cfgate/cfgate:${version}" "$tmp"
digest=$(sha256sum "$tmp" | cut -d' ' -f1)
if [[ "$old" == "$file" ]]; then
	# A digest-only update must preserve the previously reviewed release asset.
	cmp "$tmp" "$old"
fi
cp "$tmp" "$file"
sed -i -e "s|^#   https://github.com/cfgate/.*|#   ${url}|" -e "s/^#   sha256 .*/#   sha256 ${digest}/" -e "s|^  - ${old}$|  - ${file}|" kustomization.yaml
if [[ "$old" != "$file" ]]; then rm "$old"; fi
