#!/usr/bin/env bash
# Push a CONTENT-FREE canary image to the package of the Pro image.
#
#   canary.sh ghcr.io/OWNER/PATH/NAME:TAG
#
# One empty layer on an empty base, and the label PRIVATE-CANARY. It holds no
# byte of the Pro engine, the rules or the build directory: it never reads them.
# Pushing it creates the package (a new version takes the visibility of the
# package), so check-private.sh can test the package before any proprietary byte
# goes there. The Pro image is pushed only when the canary is refused to an
# anonymous pull. Needs crane and a prior login.
set -euo pipefail

ref=${1:?usage: canary.sh ghcr.io/OWNER/PATH/NAME:TAG}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A tar with no member: 10 KiB of zero bytes.
tar --create --file "$work/empty.tar" --files-from /dev/null
crane append --new_layer "$work/empty.tar" --new_tag "$ref" >/dev/null
crane mutate "$ref" --tag "$ref" \
	--label "PRIVATE-CANARY=content-free canary of the private Pro engine package; it holds no engine and no rule" \
	--label "org.opencontainers.image.description=PRIVATE-CANARY: no content" >/dev/null
echo "canary pushed: $ref"
