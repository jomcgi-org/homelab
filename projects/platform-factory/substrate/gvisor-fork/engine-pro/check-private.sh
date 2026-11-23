#!/usr/bin/env bash
# Prove that a pushed image is private, and remove it when it is not.
#
#   GH_TOKEN=... check-private.sh ghcr.io/OWNER/PATH/NAME:TAG
#
# Asks the registry for the manifest with NO credentials (an empty Docker config
# and an empty environment). Only a refusal passes: UNAUTHORIZED, DENIED,
# NAME_UNKNOWN, MANIFEST_UNKNOWN or an HTTP 401, 403 or 404. If the registry
# answers, or if the answer is neither a manifest nor a clear refusal (after
# three tries), the image is not proven private: the script deletes the version
# with `gh api` (GH_TOKEN needs packages: write on the package) and exits 1.
# When the deletion fails too, it says so loudly: delete the version by hand.
set -euo pipefail

ref=${1:?usage: check-private.sh ghcr.io/OWNER/PATH/NAME:TAG}
if [[ ! "$ref" =~ ^ghcr\.io/([a-z0-9-]+)/([a-z0-9/._-]+):([A-Za-z0-9._-]+)$ ]]; then
	echo "check-private: $ref is not ghcr.io/OWNER/PATH:TAG" >&2
	exit 64
fi
owner=${BASH_REMATCH[1]}
package=${BASH_REMATCH[2]}
tag=${BASH_REMATCH[3]}

empty=$(mktemp -d)
trap 'rm -rf "$empty"' EXIT

verdict=unknown
for attempt in 1 2 3; do
	status=0
	answer=$(env -i PATH="$PATH" HOME="$empty" DOCKER_CONFIG="$empty" crane manifest "$ref" 2>&1) || status=$?
	if [[ $status -eq 0 ]]; then
		verdict=exposed
		break
	elif grep -qE 'UNAUTHORIZED|DENIED|NAME_UNKNOWN|MANIFEST_UNKNOWN|status code 40[134]' <<<"$answer"; then
		verdict=refused
		break
	fi
	echo "check-private: try $attempt gave no clear answer: ${answer:0:200}" >&2
	sleep "${CHECK_PRIVATE_SLEEP:-5}"
done

if [[ $verdict == refused ]]; then
	echo "check-private: an anonymous pull of $ref is refused. The package is private."
	exit 0
fi

echo "::error::check-private: an anonymous pull of $ref is NOT refused (${verdict}). Deleting the version." >&2
encoded=${package//\//%2F}
status=0
versions=$(gh api --paginate "/orgs/${owner}/packages/container/${encoded}/versions?per_page=100" \
	--jq ".[] | select((.metadata.container.tags // []) | index(\"${tag}\")) | .id") || status=$?
if [[ $status -ne 0 || -z "$versions" ]]; then
	echo "::error::check-private: could not find the version of ${tag}. DELETE ${owner}/${package} BY HAND and set it private." >&2
	exit 1
fi
for id in $versions; do
	if gh api --method DELETE "/orgs/${owner}/packages/container/${encoded}/versions/${id}" >/dev/null; then
		echo "check-private: deleted version ${id} of ${package}" >&2
	else
		echo "::error::check-private: could not delete version ${id}. DELETE IT BY HAND (package settings of ${package})." >&2
	fi
done
exit 1
