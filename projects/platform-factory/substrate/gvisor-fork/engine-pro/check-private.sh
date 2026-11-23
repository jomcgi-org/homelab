#!/usr/bin/env bash
# Prove that a pushed image is private. Remove the version when it is not.
#
#   GH_TOKEN=... check-private.sh [--no-delete] ghcr.io/OWNER/PATH/NAME:TAG
#
# Two steps, in this order:
#
#   1. WITH the workflow's login: `crane manifest` of the same reference must
#      resolve. If it does not, the script fails: the push did not land, or the
#      name is wrong, and an anonymous refusal would prove nothing. (A package
#      that does not exist is refused the same way as a private one.)
#   2. With NO credentials (an empty Docker config, an empty environment): the
#      same call must be refused: UNAUTHORIZED, DENIED, NAME_UNKNOWN,
#      MANIFEST_UNKNOWN or an HTTP 401, 403 or 404. Only after step 1 does this
#      refusal mean "private". If the manifest comes back, or the answer is
#      unclear three times, the image is not proven private.
#
# A reference that is not proven private fails the job. Then the script tries to
# delete the version with `gh api` (GH_TOKEN), unless --no-delete is given (the
# canary: it holds no proprietary byte, and a public package is fixed by the
# package settings, not by a delete). The delete is best effort: GitHub needs
# admin rights on the package, not packages: write, and it refuses to delete a
# version of a public package with more than 5000 downloads. When it fails the
# script says so loudly: delete the version by hand.
set -euo pipefail

delete=1
if [[ ${1:-} == --no-delete ]]; then
	delete=0
	shift
fi
ref=${1:?usage: check-private.sh [--no-delete] ghcr.io/OWNER/PATH/NAME:TAG}
if [[ ! "$ref" =~ ^ghcr\.io/([a-z0-9-]+)/([a-z0-9/._-]+):([A-Za-z0-9._-]+)$ ]]; then
	echo "check-private: $ref is not ghcr.io/OWNER/PATH:TAG" >&2
	exit 64
fi
owner=${BASH_REMATCH[1]}
package=${BASH_REMATCH[2]}
tag=${BASH_REMATCH[3]}

empty=$(mktemp -d)
trap 'rm -rf "$empty"' EXIT
pause() { sleep "${CHECK_PRIVATE_SLEEP:-5}"; }

# Step 1: the image exists, seen with the login of the workflow.
resolved=0
for attempt in 1 2 3; do
	status=0
	answer=$(crane manifest "$ref" 2>&1 >/dev/null) || status=$?
	if [[ $status -eq 0 ]]; then
		resolved=1
		break
	fi
	echo "check-private: try $attempt: the authenticated pull of $ref failed: ${answer:0:200}" >&2
	pause
done
if [[ $resolved -ne 1 ]]; then
	echo "::error::check-private: $ref does not resolve with the workflow's login. The push did not land or the name is wrong. An anonymous refusal would prove nothing, so the image is NOT proven private. Look in the package list of the organisation." >&2
	exit 1
fi

# Step 2: the same reference, with no credentials at all.
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
	echo "check-private: try $attempt: no clear answer to the anonymous pull: ${answer:0:200}" >&2
	pause
done

if [[ $verdict == refused ]]; then
	echo "check-private: $ref resolves with the login and an anonymous pull is refused. The package is private."
	exit 0
fi

echo "::error::check-private: an anonymous pull of $ref is NOT refused (${verdict}). The package may be public." >&2
if [[ $delete -eq 0 ]]; then
	echo "::error::check-private: nothing proprietary was pushed. Set the package private in the package settings, then run again." >&2
	exit 1
fi
echo "check-private: deleting the version." >&2
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
		echo "::error::check-private: could not delete version ${id}. DELETE IT BY HAND (package settings of ${package}); GitHub needs package admin rights, and refuses to delete a public version with over 5000 downloads." >&2
	fi
done
exit 1
